"""
Source: Gmail LJ-jobs inbox — job-alert emails.

Rolls the lj-jobs-gmail-pipeline into LoveWork as a first-class source. Polls the Gmail
LJ-jobs label, classifies each email, parses supported job alerts into individual listings,
and feeds them through the registry + matcher like any other source.

This source does NO web crawling (jobs come straight from email), so the `crawler`
argument is accepted for interface uniformity but unused. Scoring is delegated entirely
to the LLM matcher — this module only EXTRACTS listings (the regex `score_role` from the
old pipeline is replaced by a cheap noise pre-filter + the matcher).

Cron-safe (assumes scheduled runs):
  - **Read-only by default** (decoupling plan A7b, 2026-10-21): scans the label's
    recent mail (`newer_than:` the window) and dedups against the local archive —
    an archived message is a processed message — so a scheduled run never
    reprocesses the same email *and never writes to the mailbox*. The old
    consume-by-marking-read regime (`is:unread` + `modify --remove-labels
    UNREAD`) survives behind LOVEWORK_GMAIL_MARK_READ=1.
  - Registry hash dedup prevents duplicate job records across emails/runs.
  - Gracefully no-ops (returns []) if Gmail is unavailable (no google_api.py / OAuth token).

Gmail access reuses the same google_api.py script as history.scan_gmail and lj-jobs-poll.
Classification + LinkedIn parsing are ported from lj-jobs-poll.py (LJ-work-2026 root).
TotalJobs, CWJobs, TalentSource, Rec-London, JobServe, Lensa, and Johnson Jobs
alerts use source-specific parsers below.
"""

import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, List, Optional

from job_registry import JobRegistry
from lead_identity import is_noise_company, is_noise_location, is_noise_title, is_non_org_name
from matcher import JobMatcher
from wiki_store import WikiEntry, match_fields

# Reuse the LJ-jobs label from history (single source of truth) and the shared Gmail
# accessor (resolves the right Python for google_api.py).
from history import GMAIL_LJ_JOBS_LABEL
from gmail_accessor import run_gapi

logger = logging.getLogger(__name__)

# Tunables (env-overridable for tests / manual runs).
MAX_EMAILS = int(os.getenv("LOVEWORK_GMAIL_MAX_EMAILS", "40"))
# Read-only by default (A7b, 2026-10-21): dedup is the local archive, not the
# mailbox's read state, so the `gmail.modify` scope is never needed and other
# people's inboxes (vj's, kj's) are never written to. LOVEWORK_GMAIL_MARK_READ=1
# restores the consume-by-marking-read regime unchanged.
MARK_READ = os.getenv("LOVEWORK_GMAIL_MARK_READ", "0") == "1"
# How far back a read-only run looks. The archive dedups what it has seen;
# the window bounds what it asks Gmail for.
WINDOW_DAYS = int(os.getenv("LOVEWORK_GMAIL_WINDOW_DAYS", "30"))
# Capture search-results URLs from alert footers and save them as related-harvest seeds. Set
# LOVEWORK_LI_CAPTURE_GMAIL_SEEDS=0 to disable.
CAPTURE_GMAIL_SEEDS = os.getenv("LOVEWORK_LI_CAPTURE_GMAIL_SEEDS", "1") != "0"
# Archive each processed email verbatim under <state>/sources/gmail/<label>/ so
# parsers can be replayed without Gmail access (see replay_archived_jobs). Set
# LOVEWORK_GMAIL_ARCHIVE=0 to disable.
CAPTURE_GMAIL_RAW = os.getenv("LOVEWORK_GMAIL_ARCHIVE", "1") != "0"


# Gmail access is provided by the shared gmail_accessor.run_gapi (resolves the right
# Python for google_api.py once, for both this source and history.scan_gmail).


# ── Classification (ported from lj-jobs-poll.py) ───────────────────────────

def classify_email(msg: dict) -> str:
    """Categorise a Gmail message by sender/subject."""
    sender = msg.get("from", "").lower()
    subject = msg.get("subject", "").lower()

    if "user@example.invalid" in sender and "application was sent" in subject:
        return "linkedin_app_sent"
    if "user@example.invalid" in sender:
        return "linkedin_alert"
    if "user@example.invalid" in sender:
        return "linkedin_listing"
    if any(d in sender for d in ("workable.com", "workablemail.com")):
        return "ats_workable"
    if "ashbyhq.com" in sender:
        return "ats_ashby"
    if "lever.co" in sender:
        return "ats_lever"
    if "greenhouse.io" in sender:
        return "ats_greenhouse"
    if "linkedin.com" in sender:
        return "linkedin_other"
    if "totaljobs.com" in sender:
        return "totaljobs_alert"
    if "cwjobs.co.uk" in sender:
        return "cwjobs_alert"
    if "talentsource" in sender or "talentsourcejobs" in sender:
        return "talentsource_alert"
    if "recruitment-london.com" in sender or "rec-london" in sender:
        return "rec_london_alert"
    if "user@example.invalid" in sender:
        return "jobserve_admin"
    if "user@example.invalid" in sender:
        return "jobserve_alert"
    if "jobserve.com" in sender and "job alert" in subject:
        return "jobserve_alert"
    if "user@example.invalid" in sender or "user@example.invalid" in sender:
        return "lensa_alert"
    if "user@example.invalid" in sender:
        return "johnsonjobs_alert"
    if "cv-library.co.uk" in sender and "new" in subject:
        return "cv_library_alert"
    if "jackandjill.ai" in sender:
        return _classify_jackandjill(subject)
    return "other"


# Jack & Jill write from jack@/user@example.invalid. Their mail is either the
# lead drop (the thing we answer; see jnj_letter.py) or account noise that must
# never be parsed as leads. Subjects observed across the 2026-02..09 corpus.
_JACKANDJILL_NOISE_SUBJECTS = (
    "verification code", "new device signed in", "introduction could earn",
    "pay you for introducing", "paid too little", "watch jill", "more powerful",
    "can i ask you something", "friends of jack", "relaunch", "no action needed",
)


def _classify_jackandjill(subject: str) -> str:
    """Split their mail into the lead drop and everything else."""
    if any(noise in subject for noise in _JACKANDJILL_NOISE_SUBJECTS):
        return "jackandjill_other"
    return "jackandjill_drop"


# Their jobs live at app.jackandjill.ai/jobs/<uuid>/post and nowhere else; the
# uuid is the lead's identity (it is NOT the public feed's <referencenumber> —
# the drop and the feed are different populations; see docs/24).
_JACKANDJILL_JOB_RE = re.compile(
    r"https://app\.jackandjill\.ai/jobs/(?P<uuid>[0-9a-fA-F-]{36})/post[^\s)\"'<>]*"
)
_JACKANDJILL_LINK_RE = re.compile(
    r"\[(?P<text>[^\]]{3,200})\]\((?P<url>https://app\.jackandjill\.ai/jobs/"
    r"[0-9a-fA-F-]{36}/post[^)]*)\)"
)


def _markdown_links(body: str) -> str:
    """Rewrite HTML anchors as markdown links so one pattern reads both shapes.

    Their batch mail is HTML (`<li><strong><a href=…>Title at Company</a>`), the
    single-lead mail is plain text with markdown links. Both carry the same
    two facts per role — a title and a rationale sentence — so normalise first
    and parse once.
    """
    text = re.sub(
        r'<a\s[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<text>.*?)</a>',
        lambda m: f"[{re.sub(r'<[^>]+>', '', m.group('text')).strip()}]({html.unescape(m.group('url'))})",
        body,
        flags=re.S | re.I,
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|li|h[1-6]|div|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def parse_jackandjill_drops(body: str) -> list[dict]:
    """Extract the roles Jack sent, each with his own rationale as description.

    The rationale is real scoring evidence — it is the recruiter's own read of
    the role against this principal's brief ("UK-remote, GPU workload
    scheduling…") — so it is passed through as the advert text rather than
    scoring on the title alone. Location is deliberately left empty: their
    prose mentions places ("Paris meetups", "US-Remote") that must not become a
    work-auth hard-kill by accident.
    """
    text = _markdown_links(body)
    jobs: list[dict] = []
    seen: set = set()
    for match in _JACKANDJILL_LINK_RE.finditer(text):
        url = html.unescape(match.group("url")).strip()
        uuid_match = _JACKANDJILL_JOB_RE.search(url)
        if not uuid_match:
            continue
        uuid = uuid_match.group("uuid")
        if uuid in seen:
            continue
        seen.add(uuid)

        label = re.sub(r"\s+", " ", match.group("text")).strip(" *")
        company, title = _split_jackandjill_label(label)
        if not company:
            # Jack writes some drops as a bold lead-in with a hyperlinked
            # "[Link]" after it, so the anchor text carries neither title nor
            # company: `* **Staff Software Engineer (Agentic Engineering) at
            # Coder** ([Link](…))`. Fall back to that lead-in, which holds
            # "<title> at <company>" in both shapes.
            lead_in = _role_label_before_link(text, match.start())
            lead_company, lead_title = _split_jackandjill_label(lead_in)
            if lead_company:
                company, title = lead_company, lead_title
        if not title or title.lower().strip(" .:") in _GENERIC_ANCHOR_TITLES:
            continue

        # Everything until the next linked role is Jack's paragraph about this one.
        tail = text[match.end():]
        stop = _JACKANDJILL_LINK_RE.search(tail)
        rationale = re.sub(r"\s+", " ", tail[: stop.start() if stop else 800]).strip(" :*|-")
        rationale = rationale.split("If you have any issues")[0].strip()

        jobs.append({
            "title": title,
            "company": company,
            "url": url.split("?")[0],
            "uuid": uuid,
            "location": "",
            "description": rationale,
        })
    return jobs


def _split_jackandjill_label(label: str) -> tuple:
    """`Member of Engineering (Compute) at poolside` → ("poolside", title)."""
    text = str(label or "").strip()
    match = re.match(r"^(?P<title>.+?)\s+at\s+(?P<company>.+)$", text)
    if not match:
        return "", text
    return match.group("company").strip(), match.group("title").strip()


# UI chrome, not roles: some mails hyperlink a role uuid under a button's text,
# and "[View on dashboard]" must not become a lead titled "View on dashboard".
_GENERIC_ANCHOR_TITLES = frozenset({
    "link", "click here", "view on dashboard", "go to dashboard", "dashboard",
    "view role", "view job", "view listing", "apply", "apply now", "see role",
    "see job", "details", "view details", "learn more", "read more",
})


def _role_label_before_link(text: str, start: int) -> str:
    """The role label a generic anchor hides, read from the bold lead-in.

    Jack's newer drops put the role in bold and hyperlink the word "Link":
    `* **Staff Software Engineer (Agentic Engineering) at Coder** ([Link](…))`.
    Only the same line is searched — the lead-in never wraps onto the next one.
    """
    line_start = text.rfind("\n", 0, start) + 1
    bold = re.findall(r"\*\*(.+?)\*\*", text[line_start:start])
    return re.sub(r"\s+", " ", bold[-1]).strip() if bold else ""


# ── LinkedIn alert parser (ported from lj-jobs-poll.py) ────────────────────

FOOTER_LINES = {
    "this email was intended for", "see all jobs on linkedin", "see all jobs",
    "job search smarter", "learn why we included", "you are receiving",
    "results from the new", "land your next role", "jobs that match your profile",
    "based on your title and location", "search for more related jobs",
}


# P3 — market gates at the boundary. The Lensa aggregator serves mostly US
# job boards; for UK-based principals its listings are noise, not leads.
# Off by default; set LOVEWORK_GMAIL_LENSA_ENABLED=1 to surface them again
# (they still pass through the work-auth kill for explicit US-only roles).
# Read at dispatch time (not import) so tests and operators can toggle it.
def _lensa_enabled() -> bool:
    return os.getenv("LOVEWORK_GMAIL_LENSA_ENABLED", "0") == "1"


def is_boilerplate(title: str) -> bool:
    """True if a parsed title is obviously footer/boilerplate text."""
    t = title.lower().strip()
    if len(t) < 5:
        return True
    for prefix in FOOTER_LINES:
        if t.startswith(prefix):
            return True
    if re.search(r'https?://', t):
        return True
    if t.startswith("top job picks") or t.startswith("new recommended"):
        return True
    return False


def _is_alert_confirmation(body: str) -> bool:
    """True for a bare "job alert has been created" notice (no adverts).

    The header alone means nothing: 3 of the archived KJ emails open with
    "Your job alert has been created: Cheminformatics in United Kingdom." and
    then carry 10 adverts between them. Only a confirmation with no advert
    block at all is a non-listing — the permissive positional parser invents
    one from its prose otherwise (title←"Your job alert has been created…",
    company←"You'll receive notific…").
    """
    if not re.search(r"your job alert\b[^\n]{0,80}?\bhas been created", body, re.IGNORECASE):
        return False
    return "View job:" not in body


def _parse_linkedin_digest(body: str) -> List[dict]:
    """Parse the LinkedIn digest alert template (2026-09 KJ-jobs):

        Your job alert for <keywords> in <region>
        A new job matches your preferences.        (or "New jobs match…")
        <TITLE>
        <COMPANY>
        <LOCATION>
        This company is actively hiring            (optional)
        View job: <URL>
        ------------------------------------------------------------

    Advert chunks are dash-rule separated. The legacy sequential parser
    mis-aligned this shape (title←boilerplate, company←title, location←company)
    and dropped the "View job:" URL entirely — every KJ lead went out linkless.
    """
    jobs: List[dict] = []
    boiler_prefixes = (
        "a new job matches", "new jobs match", "new job matches",
        "your job alert", "you’ll receive notifications",
        "you'll receive notifications", "view job:", "see all jobs",
    )
    # Boilerplate that trails the advert's location line, above the link.
    trailing_boiler = ("this company is actively hiring", "apply with resume", "apply now")
    for block in re.split(r"-{15,}", body):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        url_idx = next(
            (i for i, ln in enumerate(lines) if ln.lower().startswith("view job:")),
            None,
        )
        if url_idx is None:
            continue  # footer/upsell chunks carry no advert
        m = re.match(r"View job:\s*(\S+)", lines[url_idx])
        url = m.group(1) if m else ""
        clean = []
        for ln in lines[:url_idx]:
            low = ln.lower()
            if low.startswith(boiler_prefixes) or low.startswith("http"):
                continue
            clean.append(ln)
        while clean and clean[-1].lower().startswith(trailing_boiler):
            clean.pop()
        if len(clean) < 2:
            continue
        # Every advert closes with title / company / location, so read the
        # triple from the END. Header boilerplate we have not seen before
        # ("Your saved job at X is still available") then shifts nothing.
        triple = clean[-3:]
        if is_boilerplate(triple[0]):
            continue
        jobs.append({
            "title": re.sub(r"\s+", " ", triple[0]).strip(),
            "company": triple[1],
            "location": triple[2] if len(triple) >= 3 else "",
            "url": url,
        })
    return jobs


def parse_linkedin_alerts(body: str) -> List[dict]:
    """Extract individual job listings from a LinkedIn alert email body.

    Returns a list of dicts with keys title/company/location/url. The
    LinkedIn alert footer's "see all jobs" / "view this search" link is
    captured separately via extract_linkedin_search_url().

    Bodies containing "View job:" lines use the digest template and are
    parsed by _parse_linkedin_digest, which also captures the per-advert URL.
    """
    if _is_alert_confirmation(body):
        return []
    if "View job:" in body:
        return _parse_linkedin_digest(body)
    jobs: List[dict] = []

    # Drop everything from the footer onward.
    body_clean = re.split(
        r'(See all jobs on LinkedIn|Land your next role|Job search smarter)',
        body, maxsplit=1,
    )[0]

    # Listings are separated by runs of dashes.
    for block in re.split(r'-{40,}', body_clean):
        lines = [ln.strip() for ln in block.split('\n') if ln.strip()]
        if not lines:
            continue

        title = company = location = url = ""
        for line in lines:
            if any(line.lower().startswith(p) for p in (
                "your job alert for", "new jobs match", "new job",
                "top job picks", "new recommended",
            )):
                continue
            if "apply with resume" in line.lower() or "school alum" in line.lower():
                continue
            if line.startswith("http"):
                if not url and ("linkedin.com/comm/jobs/view/" in line or "linkedin.com/jobs/view/" in line):
                    url = line
                continue
            if not title and len(line) > 3 and not line.startswith("http"):
                title = line
            elif title and not company and line != title and len(line) > 2:
                company = line
            elif title and company and not location and line != title and line != company and len(line) > 2:
                location = line

        if title and company and not is_boilerplate(title):
            title = re.sub(r'\s+', ' ', title).strip()
            jobs.append({
                "title": title, "company": company,
                "location": location, "url": url,
            })

    return jobs


# The LinkedIn alert footer's "view all jobs" / "see this search" URL is
# the search-results page that, when opened, also shows LinkedIn's
# "related jobs" recommendations — the source of the additional leads
# LoveWork wants to harvest. We extract it from the email body so the
# linkedin_related source can follow it.

# ── Shared helper functions ──────────────────────────────────────────────


def _extract_jobs_from_blocks(text, url_pattern, skip_keywords):
    """Extract jobs by finding URL matches and looking backwards for context."""
    jobs = []
    for m in re.finditer(url_pattern, text, re.IGNORECASE):
        url = m.group(1).rstrip(').,;')
        pos = m.start()
        before = text[max(0, pos - 600):pos]
        lines_before = [l.strip() for l in before.split('\n') if l.strip()][-8:]
        title = company = location = ""
        for line in lines_before:
            low = line.lower()
            if len(line) < 3:
                continue
            if any(kw in low for kw in skip_keywords):
                continue
            if not title:
                title = line
            elif title and not company and line != title:
                company = line
            elif title and company and not location and line != title and line != company:
                location = line
        if title and not is_boilerplate(title):
            title = re.sub(r'\s+', ' ', title).strip()
            jobs.append({'title': title, 'company': company or "",
                        'location': location or "", 'url': url})
    return jobs


def _extract_jobs_plaintext(text, source_name, skip_keywords):
    """Fallback parser for emails without clickable URLs."""
    jobs = []
    body = re.split(
        r'(You are receiving this email|To unsubscribe|This email was sent|'
        rf'{source_name}|Follow us on)',
        text, maxsplit=1, flags=re.IGNORECASE,
    )[0]
    blocks = re.split(r'(?:-{3,}|={3,}|\n\n\n+)', body)
    for block in blocks:
        lines = [l.strip() for l in block.split('\n') if l.strip() and len(l.strip()) > 2]
        if not lines:
            continue
        title = company = location = ""
        for line in lines:
            low = line.lower()
            if any(kw in low for kw in skip_keywords):
                continue
            if not title:
                title = line
            elif title and not company and line != title:
                company = line
            elif title and company and not location and line != title and line != company:
                location = line
        if title and not is_boilerplate(title):
            title = re.sub(r'\s+', ' ', title).strip()
            jobs.append({'title': title, 'company': company or "",
                        'location': location or "", 'url': ""})
    return jobs


# ── Totaljobs alert parser ────────────────────────────────────────────────

def parse_totaljobs_alerts(body: str) -> list[dict]:
    """Extract individual job listings from a Totaljobs alert email body.

    Totaljobs emails are HTML with each job in a bordered block:
      - Job title as a clickable link
      - Company name, location, salary
      - Short description snippet

    Handles both the HTML link format and plain-text fallback.
    Returns a list of dicts with keys title/company/location/url.
    """
    import html as html_mod

    jobs: list[dict] = []

    text = re.sub(r'<br\s*/?>', '\n', body, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text)
    text = text.replace('\xa0', ' ')

    url_pattern = r'(https?://(?:www\.)?totaljobs\.com/job/[^"\s)>]+)'
    urls = list(re.finditer(url_pattern, text, re.IGNORECASE))

    if not urls:
        return _parse_totaljobs_plaintext(text)

    for match in urls:
        url = match.group(1).rstrip(').,;')
        pos = match.start()
        before = text[max(0, pos - 600):pos]
        lines_before = [l.strip() for l in before.split('\n') if l.strip()][-8:]

        title = company = location = ""
        for line in lines_before:
            low = line.lower()
            if len(line) < 3:
                continue
            skip_kw = ("apply", "save", "email", "http", "job alert", "new jobs",
                       "totaljobs", "refine", "similar", "recommended", "search")
            if not title and not any(kw in low for kw in skip_kw):
                title = line
            elif title and not company and line != title:
                company = line
            elif title and company and not location and line != title and line != company:
                location = line

        if title and not is_boilerplate(title):
            title = re.sub(r'\s+', ' ', title).strip()
            jobs.append({'title': title, 'company': company or "",
                        'location': location or "", 'url': url})

    return jobs


def _parse_totaljobs_plaintext(text: str) -> list[dict]:
    """Fallback parser for plain-text Totaljobs alerts without links."""
    jobs: list[dict] = []
    body = re.split(
        r'(You are receiving this email|To unsubscribe|This email was sent|'
        r'Totaljobs|Follow us on)',
        text, maxsplit=1, flags=re.IGNORECASE,
    )[0]
    blocks = re.split(r'(?:-{3,}|={3,}|\n\n\n+)', body)
    for block in blocks:
        lines = [l.strip() for l in block.split('\n') if l.strip() and len(l.strip()) > 2]
        if not lines:
            continue
        title = company = location = ""
        for line in lines:
            low = line.lower()
            if any(kw in low for kw in
                ("apply", "save", "email", "http", "job alert",
                 "totaljobs", "refine", "similar", "recommended", "search",
                 "sign in", "create alert", "register", "cookie", "privacy")):
                continue
            if not title:
                title = line
            elif title and not company and line != title:
                company = line
            elif title and company and not location and line != title and line != company:
                location = line
        if title and not is_boilerplate(title):
            title = re.sub(r'\s+', ' ', title).strip()
            jobs.append({'title': title, 'company': company or "",
                        'location': location or "", 'url': ""})
    return jobs


SEARCH_URL_RE = re.compile(
    r'(https?://(?:www\.)?linkedin\.com/(?:comm/)?jobs/(?:search|collections/[^"\s>]+)[^"\s)>]*)',
    re.IGNORECASE,
)


def extract_linkedin_search_url(body: str) -> Optional[str]:
    """Return the first plausible LinkedIn search-results URL from the body.

    LinkedIn alert emails link to a search-results page (the
    "See all jobs on LinkedIn" footer). The URL contains the user's
    search keywords + filters; opening it in LinkedIn shows the
    matched jobs PLUS related jobs (the harvest target for
    linkedin_related).
    """
    if not body:
        return None
    m = SEARCH_URL_RE.search(body)
    if not m:
        return None
    return m.group(1).rstrip(").,;")


def extract_totaljobs_search_url(body: str) -> Optional[str]:
    """Return the first Totaljobs search URL from the email footer."""
    if not body:
        return None
    m = re.search(r'(https?://(?:www\.)?totaljobs\.com/(?:jobs|search)[^"\s)>]*)', body, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).rstrip(").,;")


# ── CWJobs alert parser ───────────────────────────────────────────────────

CWJOBS_URL_RE = r'(https?://(?:www\.)?cwjobs\.co\.uk/job/[^"\s)>]+)'
CWJOBS_SKIP = ("apply", "save", "email", "http", "job alert", "new jobs",
               "cwjobs", "refine", "similar", "recommended", "search")


def parse_cwjobs_alerts(body: str) -> list[dict]:
    """CWJobs emails use a similar format to Totaljobs (same parent company)."""
    import html as html_mod
    text = re.sub(r'<br\s*/?>', '\n', body, flags=re.IGNORECASE)
    text = re.sub(r'</(?:p|div|tr|li)>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text)
    text = text.replace('\xa0', ' ')
    jobs = _extract_jobs_from_blocks(text, CWJOBS_URL_RE, CWJOBS_SKIP)
    if jobs:
        return jobs
    return _extract_jobs_plaintext(text, "cwjobs", CWJOBS_SKIP)


def extract_cwjobs_search_url(body: str) -> Optional[str]:
    """Return the first CWJobs search URL from the email footer."""
    if not body:
        return None
    m = re.search(r'(https?://(?:www\.)?cwjobs\.co\.uk/(?:jobs|search)[^"\s)>]*)', body, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).rstrip(").,;")


# ── TalentSource alert parser ─────────────────────────────────────────────

TALENTSOURCE_HOST_RE = r'(?:www\.)?talentsource(?:jobs)?\.[a-z.]+'
TALENTSOURCE_URL_RE = rf'(https?://{TALENTSOURCE_HOST_RE}/(?:job|vacancy)/[^"\s)>]+)'
TALENTSOURCE_SKIP = ("apply", "save", "email", "http", "job alert", "new jobs",
                     "talentsource", "refine", "similar", "recommended", "search")


def parse_talentsource_alerts(body: str) -> list[dict]:
    """Extract job listings from a TalentSource alert email."""
    import html as html_mod
    text = re.sub(r'<br\s*/?>', '\n', body, flags=re.IGNORECASE)
    text = re.sub(r'</(?:p|div|tr|li)>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text)
    text = text.replace('\xa0', ' ')
    jobs = _extract_jobs_from_blocks(text, TALENTSOURCE_URL_RE, TALENTSOURCE_SKIP)
    if jobs:
        return jobs
    return _extract_jobs_plaintext(text, "talentsource", TALENTSOURCE_SKIP)


def extract_talentsource_search_url(body: str) -> Optional[str]:
    """Return the first TalentSource search URL from the email footer."""
    if not body:
        return None
    m = re.search(rf'(https?://{TALENTSOURCE_HOST_RE}/(?:search|jobs)(?:/[^"\s)>]*)?)', body, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).rstrip(").,;")


# ── Rec-London alert parser ────────────────────────────────────────────────

RECLONDON_URL_RE = (
    r'(https?://(?:www\.)?recruitment-london\.com/'
    r'(?:job|jobs|vacancy|vacancies)/[^"\s)>]+)'
)
RECLONDON_SKIP = ("apply", "save", "email", "http", "job alert", "new jobs",
                  "rec-london", "recruitment", "refine", "similar", "recommended",
                  "search", "unsubscribe", "privacy")


def parse_rec_london_alerts(body: str) -> list[dict]:
    """Extract job listings from a Rec-London job alert email."""
    import html as html_mod
    text = re.sub(r'<br\s*/?>', '\n', body, flags=re.IGNORECASE)
    text = re.sub(r'</(?:p|div|tr|li)>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text)
    text = text.replace('\xa0', ' ')
    jobs = _extract_jobs_from_blocks(text, RECLONDON_URL_RE, RECLONDON_SKIP)
    if jobs:
        return jobs
    return _extract_jobs_plaintext(text, "recruitment-london", RECLONDON_SKIP)


def extract_rec_london_search_url(body: str) -> Optional[str]:
    """Return the first Rec-London search URL from the email footer."""
    if not body:
        return None
    m = re.search(r'(https?://(?:www\.)?recruitment-london\.com/(?:search|jobs)/[^"\s)>]*)', body, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).rstrip(").,;")


# ── Lensa Aggregated alert parser ─────────────────────────────────────────

LENSA_URL_RE = (
    r'(https?://(?:www\.)?lensa\.com/'
    r'(?!unsubscribe|privacy|terms|login|signup|faq|about|contact)'
    r'[^"\s)>]+)'
)
LENSA_TRACKING_RE = re.compile(
    r'^\]\((https://email\.lensa\.com/f/[^)\n]+)\)', re.IGNORECASE | re.MULTILINE
)
LENSA_SKIP = (
    "apply now", "save", "email", "http", "lensa", "jobs you might",
    "recommended", "search jobs", "search for", "unsubscribe", "privacy", "terms",
    "sign in", "create account", "view all", "learn more",
)


def parse_lensa_alerts(body: str) -> list[dict]:
    """Extract job cards from a Lensa Aggregated alert email."""
    import html as html_mod

    # Gmail's text representation of this provider is Markdown. Each card
    # ends with a main link on a line beginning `](`; logo links inside the
    # card must not be mistaken for job links.
    markdown_jobs: list[dict] = []
    cursor = 0
    for match in LENSA_TRACKING_RE.finditer(body):
        segment = body[cursor:match.start()]
        cursor = match.end()
        segment = re.sub(r'!\[[^]]*\]\([^)]*\)', '', segment)
        segment = html_mod.unescape(segment)
        lines = []
        for raw_line in segment.splitlines():
            line = re.sub(r'\s+', ' ', raw_line).strip(' []|•\t')
            line = line.replace('․', '.').replace('·', '.')
            line = line.rstrip('.').strip()
            if line:
                lines.append(line)
        separators = [
            index for index, line in enumerate(lines) if line.startswith('---')
        ]
        # The message header has its own horizontal rule; the card rule is
        # the last one in the segment (usually rendered as ---|---).
        card_separators = [
            index for index in separators if '|' in lines[index]
        ]
        separator = (card_separators[-1] if card_separators else separators[-1]) if separators else None
        if separator is None:
            continue
        before = [line for line in lines[:separator] if line.lower() not in {
            'jobs posted on 100+ job boards', 'jobs posted on 52+ job boards',
        }]
        after = lines[separator + 1:]
        if not before or not after:
            continue
        title = next(
            (line for line in after if not line.startswith('$') and not re.fullmatch(
                r'(?:new[• ]+)?(?:full[- ]time|part[- ]time|intern)(?:•.*)?',
                line, re.IGNORECASE,
            )),
            '',
        )
        if not title or is_boilerplate(title):
            continue
        company = before[-1]
        location = ''
        for line in after:
            if line == title or line.startswith('$'):
                continue
            if 'remote' in line.lower() and '•' in line:
                location = 'Remote'
                break
            if re.search(r'\b[A-Z][a-z]+(?:[ -][A-Z][a-z]+)*,\s*[A-Z]{2}\b', line):
                location = line.strip('| ')
                break
        markdown_jobs.append({
            'title': title,
            'company': company,
            'location': location,
            'url': match.group(1).rstrip(').,;'),
        })
    if markdown_jobs:
        return markdown_jobs

    def replace_anchor(match: re.Match) -> str:
        url = html_mod.unescape(match.group(1)).strip()
        title = re.sub(r'<[^>]+>', ' ', match.group(2))
        title = re.sub(r'\s+', ' ', html_mod.unescape(title)).strip()
        return f"\n{title}\n{url}\n"

    body = re.sub(
        r'<a\b[^>]*?href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
        replace_anchor, body, flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r'<br\s*/?>', '\n', body, flags=re.IGNORECASE)
    text = re.sub(r'</(?:p|div|tr|li|td|h[1-6])>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text).replace('\xa0', ' ')
    jobs: list[dict] = []
    cursor = 0
    for match in re.finditer(LENSA_URL_RE, text, re.IGNORECASE):
        segment = text[cursor:match.start()]
        cursor = match.end()
        lines = [re.sub(r'\s+', ' ', line).strip(' -|•\t')
                 for line in segment.splitlines() if line.strip()]
        lines = [line for line in lines if line and not any(
            keyword in line.lower() for keyword in LENSA_SKIP + ("view job",)
        )]
        if len(lines) == 2:
            title, location = lines
            company = title
        elif len(lines) >= 3:
            title, company, location = lines[-3:]
        else:
            continue
        if not is_boilerplate(title):
            jobs.append({
                "title": title,
                "company": company,
                "location": location,
                "url": match.group(1).rstrip(").,;"),
            })
    if jobs:
        return jobs
    return _extract_jobs_plaintext(text, "lensa", LENSA_SKIP)


# ── JobServe alert parser ─────────────────────────────────────────────────

JOBSERVE_URL_RE = re.compile(
    r'https?://(?:[a-z0-9-]+\.)?jobserve\.com/[^\s"\'<>]+',
    re.IGNORECASE,
)
JOBSERVE_SKIP = (
    "apply", "email", "http", "job alert", "jobs by email", "jobserve",
    "new jobs", "recommended", "saved search", "search results", "unsubscribe",
    "privacy", "sign in", "view all", "this email was sent",
)


def _is_jobserve_job_url(url: str) -> bool:
    """Distinguish listing links from JobServe account/search/footer links."""
    low = url.lower().rstrip(").,;")
    if any(part in low for part in (
        "unsubscribe", "optout", "jobsearch", "savedsearch", "candidate",
        "account", "login", "contact", "privacy", "cookie", "jobalert",
    )):
        return False
    if any(part in low for part in (
        "/job-in-", "/trx-adv/", "joblanding", "joblisting", "jobid=", "jid=",
    )):
        return True

    # JobServe also uses short, case-sensitive permalinks such as /f5zzb.
    path = re.sub(r'^https?://(?:[a-z0-9-]+\.)?jobserve\.com/', '', low)
    return bool(re.fullmatch(r'[a-z0-9]{5,12}/?(?:\?.*)?', path))


def _clean_email_html(
    body: str,
    job_url_predicate=_is_jobserve_job_url,
) -> tuple[str, dict[str, str]]:
    """Convert an email body to text while retaining selected anchor titles."""
    import html as html_mod

    anchor_titles: dict[str, str] = {}

    def replace_anchor(match: re.Match) -> str:
        url = html_mod.unescape(match.group(1)).strip()
        title = re.sub(r'<[^>]+>', ' ', match.group(2))
        title = re.sub(r'\s+', ' ', html_mod.unescape(title)).strip()
        if title and job_url_predicate(url):
            anchor_titles[url.rstrip(").,;")] = title
        return f"\n{title}\n{url}\n"

    text = re.sub(
        r'<a\b[^>]*?href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
        replace_anchor,
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</(?:p|div|tr|li|td|h[1-6])>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html_mod.unescape(text).replace('\xa0', ' ')
    return text, anchor_titles


def parse_jobserve_alerts(body: str) -> list[dict]:
    """Extract listings from a JobServe Jobs-by-Email digest.

    JobServe uses several link shapes: regional ``job-in-*`` pages,
    ``trx-adv`` tracking links, legacy landing/listing URLs, and short
    permalinks. Anchor text is preferred as the title; plaintext messages use
    the three lines immediately preceding the link as title/company/location.
    """
    text, anchor_titles = _clean_email_html(body)
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    for match in JOBSERVE_URL_RE.finditer(text):
        url = match.group(0).rstrip(").,;")
        if url in seen_urls or not _is_jobserve_job_url(url):
            continue
        seen_urls.add(url)

        before = text[max(0, match.start() - 900):match.start()]
        card_lines = []
        for line in before.splitlines()[-14:]:
            line = re.sub(r'\s+', ' ', line).strip(" -|•\t")
            low = line.lower()
            if len(line) < 3 or any(keyword in low for keyword in JOBSERVE_SKIP):
                continue
            card_lines.append(line)

        title = anchor_titles.get(url, "")
        company = location = ""
        if title:
            # The anchor title is normally the final line before its URL.
            context = card_lines[:-1] if card_lines and card_lines[-1] == title else card_lines
            if len(context) >= 2:
                company, location = context[-2:]
            elif context:
                company = context[-1]
        elif len(card_lines) >= 3:
            title, company, location = card_lines[-3:]
        elif card_lines:
            title = card_lines[-1]

        if title and not is_boilerplate(title):
            jobs.append({
                "title": title,
                "company": company or "JobServe listing",
                "location": location,
                "url": url,
            })

    return jobs


# ── Johnson Jobs alert parser ─────────────────────────────────────────────

JOHNSONJOBS_URL_RE = re.compile(
    r'https?://(?:www\.)?johnsonjobs\.com/[^\s"\'<>]+',
    re.IGNORECASE,
)
JOHNSONJOBS_SKIP = (
    "apply", "email", "http", "johnson jobs", "new job post", "job alert",
    "unsubscribe", "privacy", "terms", "sign in", "view all", "manage alerts",
)


def _is_johnsonjobs_job_url(url: str) -> bool:
    """Keep job/redirect links but reject account and footer links."""
    low = url.lower().rstrip(").,;")
    return not any(part in low for part in (
        "unsubscribe", "optout", "privacy", "terms", "login", "signin",
        "account", "preferences", "manage-alert", "contact",
    ))


def parse_johnsonjobs_alerts(body: str) -> list[dict]:
    """Extract Johnson Jobs email listings without trusting their provenance.

    Johnson Jobs links are retained as discovery URLs: they are commonly
    redirect/tracking links, and primary-page enrichment must establish whether
    the advertised employer and role are real before a lead can be trusted.
    """
    text, anchor_titles = _clean_email_html(body, _is_johnsonjobs_job_url)
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    for match in JOHNSONJOBS_URL_RE.finditer(text):
        url = match.group(0).rstrip(").,;")
        if url in seen_urls or not _is_johnsonjobs_job_url(url):
            continue
        seen_urls.add(url)

        before = text[max(0, match.start() - 900):match.start()]
        card_lines = []
        for line in before.splitlines()[-14:]:
            line = re.sub(r'\s+', ' ', line).strip(" -|•\t")
            low = line.lower()
            if len(line) < 3 or any(keyword in low for keyword in JOHNSONJOBS_SKIP):
                continue
            card_lines.append(line)

        title = anchor_titles.get(url, "")
        company = location = ""
        if title:
            context = card_lines[:-1] if card_lines and card_lines[-1] == title else card_lines
            if len(context) >= 2:
                company, location = context[-2:]
            elif context:
                company = context[-1]
        elif len(card_lines) >= 3:
            title, company, location = card_lines[-3:]
        elif card_lines:
            title = card_lines[-1]

        if title and not is_boilerplate(title):
            jobs.append({
                "title": title,
                "company": company or "Johnson Jobs listing",
                "location": location,
                "url": url,
            })

    return jobs


# ── CV-Library alert parser ────────────────────────────────────────────────

CV_LIBRARY_SKIP = {
    "apply", "view more", "discover all matching", "search jobs", "edit alerts",
    "unsubscribe", "preferences", "cv-library logo", "run search",
    "hi user", "new jobs for you", "matching jobs for your alert",
    "email footer", "all matching jobs", "cv-library email footer",
    "run search", "today run search",
}


def parse_cv_library_alerts(body: str) -> list[dict]:
    """Extract listings from a CV-Library job alert email.

    CV-Library sends plain-text multipart emails with the format:

        ****...****
        Job Title (
        https://clicks.cv-library.co.uk/f/a/TRACKING_ID
        )
        ****...****Location,
        £salary
        Apply  (
        https://clicks.cv-library.co.uk/f/a/APPLY_ID
        )Job Title Location: ... Description ...

    Each job appears twice (compact + full).  We parse the first occurrence
    and deduplicate by tracking URL.  Company names are not directly
    embedded in CV-Library alert emails.
    """
    # Split on separator lines of 50+ consecutive `*` or `=` chars
    blocks = re.split(r'\n[*=]{50,}\n', body)

    jobs: list[dict] = []
    seen_urls: set[str] = set()
    # Minimum title length to filter out "jobs", "apply" etc.
    MIN_TITLE_LEN = 12

    for block in blocks:
        lines = block.strip().split('\n')
        # Clean whitespace
        clean = [re.sub(r'\s+', ' ', l).strip() for l in lines]

        for i in range(len(clean) - 2):
            # Look for "Title (" then next line is a URL, then ")"
            if (clean[i].endswith('(')
                    and not clean[i].startswith('http')
                    and clean[i + 1].startswith('http')
                    and clean[i + 2] == ')'):
                candidate_title = clean[i].rstrip(' (').strip()
                candidate_url = clean[i + 1].rstrip(').,;')

                # Must be a CV-Library clicks tracking URL
                if 'clicks.cv-library.co.uk' not in candidate_url:
                    continue
                # Skip short boilerplate titles
                if len(candidate_title) < MIN_TITLE_LEN:
                    continue
                low_title = candidate_title.lower()
                if any(kw in low_title for kw in CV_LIBRARY_SKIP):
                    continue

                title = candidate_title
                url = candidate_url

                # Extract location from the line immediately after the closing ")"
                # Format: "***...***City of London, London," or "***...***London,"
                location = ""
                after_close = i + 3  # line right after ")"
                if after_close < len(clean):
                    loc_candidate = re.sub(r'^\**', '', clean[after_close]).strip().rstrip(',')
                    if loc_candidate and len(loc_candidate) < 80:
                        location = loc_candidate

                if url not in seen_urls:
                    seen_urls.add(url)
                    jobs.append({
                        "title": title,
                        "company": "CV-Library listing",
                        "location": location,
                        "url": url,
                    })
                break

    return jobs


# ── Cheap noise pre-filter (ported KILL_SIGNAL from lj-jobs-poll.py) ───────
# Skips obvious non-fits BEFORE the (expensive) LLM match. The matcher still scores
# everything that passes — this only avoids wasting LLM calls on clear noise.

# A title carrying any of these is never an "obvious" non-fit, however ordinary
# the rest of it looks. `agent\w*` matters: without it the plain `agent` word
# missed "Agentic", so "Staff Software Engineer (Agentic Engineering) at Coder"
# — a Jack & Jill drop, and exactly this principal's brief — was filtered before
# the matcher ever saw it.
_AI_SIGNAL = r"(?:ml|ai|machine|learning|research|llm|agent\w*|genai|nlp|inference)"

OBVIOUS_NONFIT = [
    r"\bquant\b", r"\bquantitative\b", r"\btrader\b", r"\bportfolio\b",
    r"\bdata\s*analyst\b",
    rf"\bsoftware\s*engineer\b(?!.*\b{_AI_SIGNAL}\b)",
    rf"\bfinanc\w*\b(?!.*\b{_AI_SIGNAL}\b|\bquant\b)",
    r"\baccount\w*\b", r"\bmarket\w*\b", r"\boperat\w*\b",
]


def is_obvious_nonfit(title: str, company: str = "") -> bool:
    text = f"{title} {company}".lower()
    return any(re.search(p, text) for p in OBVIOUS_NONFIT)


# ── Source ─────────────────────────────────────────────────────────────────

class GmailJobsSource:
    #: This source's tier, declared here and nowhere else
    #: (docs/28 §WP3b box 1). Primary = the hiring party's own
    #: publication; secondary = someone relaying it.
    primary = False
    #: Set when the run enumerated its *whole* scope (no truncation,
    #: caps or failures). Only then may absence become
    #: `not_listed_in_scope` rather than `disappeared`.
    complete_scope = False
    """Ingest supported job alerts from one principal-approved Gmail label."""

    def __init__(
        self,
        crawler=None,  # unused — jobs come from email, not the web
        matcher: Optional[JobMatcher] = None,
        registry: Optional[JobRegistry] = None,
        max_emails: int = MAX_EMAILS,
        mark_read: bool = MARK_READ,
        *,
        label: str = GMAIL_LJ_JOBS_LABEL,
        credential_home: Optional[Path] = None,
        source_name: str = "gmail_jobs",
        sources_dir: Optional[Path] = None,
        capture_seeds: bool = CAPTURE_GMAIL_SEEDS,
        capture_raw: bool = CAPTURE_GMAIL_RAW,
        query: Optional[str] = None,
        force_ids: Optional[Iterable[str]] = None,
    ):
        self.matcher = matcher
        self.registry = registry
        self.max_emails = max_emails
        self.label = label
        # A query replaces the label as the search scope (the Jack & Jill drop
        # arrives unlabelled). Query mode never marks mail read — a search like
        # `from:jackandjill.ai` can match personal correspondence, and the
        # principal's unread marker is his, not ours.
        self.query = query
        self.mark_read = mark_read and not query
        self.credential_home = credential_home
        self.name = source_name
        self.sources_dir = sources_dir
        self.capture_seeds = capture_seeds
        self.capture_raw = capture_raw
        # (org, title) already produced in the current run; see _already_seen.
        self._seen_leads: set = set()
        # Message ids to read even though they are archived: the escape hatch for
        # a parser or gate fix, since an archived mail is otherwise never read
        # again (and query mode cannot mark mail read to signal progress).
        self.force_ids = {str(i) for i in (force_ids or ())}

    def _gapi(self, *args):
        """Call Gmail without changing legacy test and LJ call signatures."""
        if self.credential_home is None:
            return run_gapi(*args)
        return run_gapi(*args, credential_home=self.credential_home)

    def _search_query(self) -> str:
        """What to ask Gmail for.

        Query mode reads whatever it matches. Label mode depends on how
        progress is marked: read-only (the default since A7b) asks for the
        label's recent window and lets the archive dedup; the legacy
        consume regime asks for `is:unread` and marks each read.
        """
        if self.query:
            return self.query
        if not self.mark_read:
            return f"label:{self.label} newer_than:{WINDOW_DAYS}d"
        return f"label:{self.label} is:unread"

    def run(self) -> List[WikiEntry]:
        entries: List[WikiEntry] = []
        self._seen_leads.clear()  # dedupe is per run

        messages = self._gapi(
            "gmail", "search", self._search_query(), "--max", str(self.max_emails)
        )
        scope = self.query or f"label:{self.label}"
        if messages is None:
            logger.warning(
                f"[{self.name}] Gmail UNAVAILABLE for {scope} — this run "
                "contributes 0 leads. This is a credential/transport failure, not an "
                "empty mailbox; check the stored token."
            )
            return entries

        if (self.query or not self.mark_read) and self.sources_dir is not None:
            # Already archived = already processed — the rule for every mode
            # that cannot consume mail (query mode, and label mode since A7b
            # made read-only the default). Only when the record carries a
            # category this version still knows: see `_archived_as_processed`
            # for why existence alone is not enough, `_retry_marker` for mail
            # a parser found nothing in, and `force_ids` for mail a caller
            # deliberately wants re-read.
            already = [m for m in messages
                       if str(m.get("id") or "") not in self.force_ids
                       and self._archived_as_processed(str(m.get("id") or ""))]
            if already:
                messages = [m for m in messages if m not in already]
                logger.info(
                    f"[{self.name}] {len(already)} of {len(already) + len(messages)} "
                    f"{scope} email(s) already archived; skipped"
                )

        if self.query or not self.mark_read:
            # A consuming run fetches a body for every recognised mail because
            # marking it read ends the matter. A non-consuming run cannot, so
            # mail with no parser would be re-fetched on every single run
            # forever. Their account codes and marketing are recognisable from
            # the headers alone — drop them before the body fetch.
            parsable = [
                m for m in messages
                if (self._PARSERS.get(classify_email(m)) or (None, None, False))[0] is not None
            ]
            if len(parsable) != len(messages):
                logger.info(
                    f"[{self.name}] {len(messages) - len(parsable)} {scope} email(s) "
                    "carry no parser (account/marketing); not fetched"
                )
            messages = parsable

        if not messages:
            logger.info(f"[{self.name}] No new mail for {scope}.")
            return entries

        logger.info(f"[{self.name}] {len(messages)} new {scope} email(s) to process")
        for msg in messages:
            try:
                entries.extend(self._process_email(msg))
            except Exception as e:
                logger.error(f"[{self.name}] Failed on email {msg.get('id')}: {e}")

        return entries

    # ── Dispatch map: category -> (parser_fn, seed_extractor_fn, is_lead) ──
    _PARSERS = {
        "linkedin_alert":     (parse_linkedin_alerts,     extract_linkedin_search_url,     True),
        "linkedin_listing":   (parse_linkedin_alerts,     None,                            True),
        "linkedin_other":     (parse_linkedin_alerts,     None,                            True),
        "linkedin_app_sent":  (None,                      None,                            False),
        "totaljobs_alert":    (parse_totaljobs_alerts,    extract_totaljobs_search_url,    True),
        "cwjobs_alert":       (parse_cwjobs_alerts,       extract_cwjobs_search_url,       True),
        "talentsource_alert": (parse_talentsource_alerts, extract_talentsource_search_url, True),
        "rec_london_alert":   (parse_rec_london_alerts,   extract_rec_london_search_url,   True),
        "jobserve_alert":     (parse_jobserve_alerts,     None,                            True),
        "jobserve_admin":     (None,                      None,                            False),
        "lensa_alert":        (parse_lensa_alerts,         None,                            True),
        "johnsonjobs_alert":  (parse_johnsonjobs_alerts,   None,                            True),
        "cv_library_alert":   (parse_cv_library_alerts,    None,                            True),
        # The drop: hand-picked leads against this principal's brief. Their
        # other mail (codes, device sign-in, partner marketing) is archived and
        # deliberately never parsed.
        "jackandjill_drop":   (parse_jackandjill_drops,    None,                            True),
        "jackandjill_other":  (None,                       None,                            False),
    }

    def _process_email(self, msg: dict) -> List[WikiEntry]:
        msg_id = msg.get("id")
        category = classify_email(msg)
        parser_info = self._PARSERS.get(category)
        if parser_info is None:
            # Unknown category — skip (don't mark read, in case it's misclassified).
            return []

        parser_fn, seed_extractor_fn, is_lead = parser_info
        raw_jobs: List[dict] = []

        if not is_lead:
            # E.g. linkedin_app_sent — prior-contact signals, not leads.
            pass
        elif parser_fn is not None:
            body_resp = self._gapi("gmail", "get", msg_id)
            body = (body_resp or {}).get("body", "") if isinstance(body_resp, dict) else ""
            if body:
                # Archive before parsing: a parser that fails (or silently
                # mis-aligns, as the 2026-09-10 digest case did) must still be
                # replayable offline against the exact text it saw.
                self._archive_raw(msg, category, body)
                if _is_alert_confirmation(body):
                    # Not a digest: nothing to extract, and leaving it unread
                    # would replay it every run. Consume it silently — this is
                    # the one recognised email that is safe to consume with
                    # zero extracted listings.
                    if self.mark_read and msg_id:
                        self._gapi("gmail", "modify", msg_id, "--remove-labels", "UNREAD")
                    logger.info(
                        f"[{self.name}] consumed alert confirmation {msg_id} without leads"
                    )
                    return []
                raw_jobs = parser_fn(body)
                if self.capture_seeds and seed_extractor_fn is not None:
                    try:
                        search_url = seed_extractor_fn(body)
                        if search_url:
                            self._save_seed(category, search_url)
                    except Exception as e:
                        logger.debug(f"[{self.name}] seed capture failed: {e}")

        # P3 — market gates at the boundary: Lensa is a US-market noise
        # source. When disabled, consume the email (mark read) but produce
        # no leads — the listings are not worth scoring for a UK principal.
        if category == "lensa_alert" and not _lensa_enabled():
            if self.mark_read and msg_id:
                self._gapi("gmail", "modify", msg_id, "--remove-labels", "UNREAD")
            logger.info(
                f"[{self.name}] Lensa disabled (US-market noise); consumed "
                f"{category} {msg_id} without leads"
            )
            return []

        produced = self._entries_from_jobs(raw_jobs, category)

        # Never consume a recognised lead email whose parser found nothing.
        # A provider changing its template should leave a visible unread item
        # for inspection rather than silently dropping all of its leads.
        parsed_successfully = (not is_lead) or bool(raw_jobs)
        if is_lead and not raw_jobs:
            logger.warning(
                f"[{self.name}] {category} email {msg_id} yielded no listings; "
                + ("leaving it unread" if self.mark_read else
                   "marked for re-read (parser found nothing)")
            )
            self._set_retry(msg_id, True)
        elif msg_id:
            self._set_retry(msg_id, False)
        if self.mark_read and msg_id and parsed_successfully:
            self._gapi("gmail", "modify", msg_id, "--remove-labels", "UNREAD")
        return produced

    def _set_retry(self, msg_id: str, wanted: bool) -> None:
        """Write or clear the retry marker; never fails the run."""
        if self.sources_dir is None or not msg_id:
            return
        marker = self._retry_marker(msg_id)
        try:
            if wanted:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("parser found nothing; re-read until it does\n")
            elif marker.exists():
                marker.unlink()
        except OSError as e:
            logger.debug(f"[{self.name}] retry marker {msg_id}: {e}")

    def _already_seen(self, org_name: str, title: str) -> bool:
        """True when this (org, title) was already produced in the current run.

        Alert boards re-send the same advert across several emails; without
        this the lead is scored once per copy and listed repeatedly in the
        report (the 2026-09-10 KJ replay: 72 entries, 51 distinct leads).
        """
        key = (org_name.strip().lower(), title.strip().lower())
        if key in self._seen_leads:
            logger.info(f"[{self.name}] duplicate advert skipped: {org_name} — {title[:60]}")
            return True
        self._seen_leads.add(key)
        return False

    def _entries_from_jobs(self, raw_jobs: List[dict], category: str) -> List[WikiEntry]:
        """Turn parsed listings into scored entries: noise gates → registry → matcher.

        Shared by the live path (`_process_email`) and the archive replay
        (`run_from_archive`) so a replayed email is processed exactly like a
        freshly fetched one. One (org, title) is produced once per run: boards
        re-send the same advert across alerts (2026-09-10 KJ corpus: one advert
        arrived 7 times), and each copy used to cost a matcher call and print a
        duplicate report block.
        """
        produced: List[WikiEntry] = []
        for job in raw_jobs:
            # P2 — identity integrity: the title must be the role title and
            # the location a place. Drop subjects, salary strings, dates,
            # and mis-split prose at the source instead of scoring them.
            if is_noise_title(job["title"]):
                logger.info(
                    f"[{self.name}] dropped noise title from {category}: "
                    f"{job['title'][:80]!r}"
                )
                continue
            if is_noise_location(job.get("location", "")):
                logger.info(
                    f"[{self.name}] dropped noise location from {category}: "
                    f"{job['location'][:80]!r}"
                )
                continue
            # P2 — the *company* field needs the same gate as the title: a
            # mail parser that mis-splits a digest line makes the org a
            # sentence ("You're a great fit for this role if you have the
            # following skills:"), which then mints a wiki page named after
            # it (the 2026-10-07 audit's not-an-org class).
            #
            # `company == title` is the parsers' "no company in this card"
            # convention (a Lensa card with no company line): the company is
            # *unknown*, not junk, so the org gate must not drop the listing.
            # The page-minting gate routes such a name to `_Unattributed`.
            company = job.get("company", "")
            if company.strip() and company.strip() != job["title"].strip() \
                    and is_non_org_name(company):
                logger.info(
                    f"[{self.name}] dropped non-org company from {category}: "
                    f"{job.get('company', '')[:80]!r}"
                )
                continue
            # The company column can hold the salary band, the contract type
            # or a UI string when a card's boundary falls differently
            # ("Competitive", "400 - 450 per day" — 2026-10-07 audit). A page
            # named after one is a page nobody can find, so gate it here as
            # well as at the writer.
            if is_noise_company(job.get("company", "")):
                logger.info(
                    f"[{self.name}] dropped noise company from {category}: "
                    f"{job.get('company', '')[:80]!r}"
                )
                continue
            if is_obvious_nonfit(job["title"], job.get("company", "")):
                continue
            if self._already_seen(job["company"], job["title"]):
                continue
            entry = self._make_entry(
                org_name=job["company"], title=job["title"],
                url=job.get("url", ""), location=job.get("location", ""),
                description=job.get("description", ""),
            )
            if entry is not None:
                produced.append(entry)
        return produced

    def _archive_path(self, msg_id: str) -> Path:
        """Path of the archived record for one message."""
        return self.sources_dir / "gmail" / _archive_slug(self.label) / f"{msg_id}.json"

    def _archived_as_processed(self, msg_id: str) -> bool:
        """Whether the archive accounts for this message *as parsed*.

        Existence alone is not proof: a record whose category this version does
        not recognise was written by an older classifier, so its mail was
        archived but never handed to a parser (the Jack & Jill drops of
        2026-09-05 and 09-12 sat under the literal category "other" that way,
        and the drop ingest could never see them again).
        """
        path = self._archive_path(msg_id)
        if not path.exists():
            return False
        if self._retry_marker(msg_id).exists():
            # A lead email whose parser found nothing: archived (replayable)
            # but not *processed* — re-read it each run until a parser fix
            # lands, which is what "leave it unread" used to mean.
            return False
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return str(record.get("category") or "") in self._PARSERS

    def _retry_marker(self, msg_id: str) -> Path:
        """Beside the archive record: present = "a parser found nothing here"."""
        path = self._archive_path(msg_id)
        return path.with_name(path.name + ".retry")

    def _archive_raw(self, msg: dict, category: str, body: str) -> None:
        """Persist the email verbatim so parsers replay without Gmail.

        One JSON record per message under `<sources_dir>/gmail/<label>/`. The
        archive is the processed corpus: exactly the text the parsers saw,
        including emails whose parser extracted nothing (those are precisely
        the ones worth replaying after a template change).

        Never fails a crawl — archival problems are logged, not raised. The
        directory ignores itself via `.gitignore` because these are the
        principal's private messages and state/ is otherwise partially tracked.
        """
        if not self.capture_raw or self.sources_dir is None:
            return
        msg_id = str(msg.get("id") or "")
        if not msg_id:
            return
        try:
            label_dir = self.sources_dir / "gmail" / _archive_slug(self.label)
            label_dir.mkdir(parents=True, exist_ok=True)
            ignore = label_dir / ".gitignore"
            if not ignore.exists():
                ignore.write_text("*\n")
            record = {
                "id": msg_id,
                "label": self.label,
                "source": self.name,
                "category": category,
                "from": msg.get("from", ""),
                "subject": msg.get("subject", ""),
                "date": msg.get("date", ""),
                "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "body": body,
            }
            (self._archive_path(msg_id)).write_text(
                json.dumps(record, ensure_ascii=False, indent=1)
            )
        except Exception as e:
            logger.warning(f"[{self.name}] raw-email archive failed for {msg_id}: {e}")

    def backfill_archive(self, query: Optional[str] = None) -> int:
        """Archive the label's existing mail (read and unread) without parsing.

        The run path archives only what it processes (`is:unread`), so already
        consumed mail is otherwise never captured. This one-shot fill puts the
        whole label on disk so a template change can be replayed against the
        full corpus. It fetches bodies only — never marks read, scores, or
        writes seeds — and skips messages already archived, so re-running is
        free.

        Returns the number of emails archived.
        """
        if not self.capture_raw or self.sources_dir is None:
            return 0
        messages = self._gapi(
            "gmail", "search", query or f"label:{self.label}", "--max", str(self.max_emails)
        )
        if messages is None:
            logger.warning(
                f"[{self.name}] archive backfill: Gmail unavailable for {self.label} "
                "(credential/transport failure, not an empty mailbox)"
            )
            return 0
        archived = 0
        for msg in messages or []:
            msg_id = str(msg.get("id") or "")
            if not msg_id:
                continue
            try:
                if self._archive_path(msg_id).exists():
                    continue
                resp = self._gapi("gmail", "get", msg_id)
                body = (resp or {}).get("body", "") if isinstance(resp, dict) else ""
                if body:
                    self._archive_raw(msg, classify_email(msg), body)
                    archived += 1
            except Exception as e:
                logger.warning(f"[{self.name}] archive backfill failed for {msg_id}: {e}")
        logger.info(f"[{self.name}] archive backfill: {archived} new email(s) of {len(messages or [])}")
        return archived

    def run_from_archive(self, label: Optional[str] = None) -> List[WikiEntry]:
        """Re-ingest the archived corpus through the registry and the matcher.

        The offline equivalent of `run()`: reads the emails saved by
        `_archive_raw`, re-parses them with the current parsers, and scores the
        leads exactly as a live crawl does (same market/noise gates, registry
        upserts, matcher). It makes no Gmail call and marks nothing read, so it
        can put a parser fix into the principal's registry, org pages and report
        without waiting for the next crawl — or replay a corpus whose mail has
        already been consumed.
        """
        entries: List[WikiEntry] = []
        if self.sources_dir is None:
            logger.warning(f"[{self.name}] archive replay needs sources_dir; nothing to replay")
            return entries
        self._seen_leads.clear()  # dedupe is per replay

        archived = list(iter_archived_emails(self.sources_dir, label or self.label))
        for record in archived:
            category, parser_info = _resolve_archived_category(record)
            if parser_info[0] is None:
                continue  # non-lead category, archived for audit only
            body = record.get("body") or ""
            if not body or _is_alert_confirmation(body):
                continue
            if category == "lensa_alert" and not _lensa_enabled():
                continue  # same market gate as the live path
            entries.extend(self._entries_from_jobs(parser_info[0](body), category))

        logger.info(
            f"[{self.name}] archive replay: {len(archived)} archived email(s) → "
            f"{len(entries)} entr(ies)"
        )
        return entries

    def _save_seed(self, category: str, search_url: str):
        """Save a seed URL for related-ads harvesting."""
        if category.startswith("linkedin"):
            from .linkedin_related import append_seed
            seeds_path = (
                self.sources_dir / "linkedin_seeds.md" if self.sources_dir is not None else None
            )
            append_seed(search_url, seeds_path=seeds_path)
        else:
            import config
            seed_dir = self.sources_dir or (config.PROFILES_DIR / "lj")
            seed_file = seed_dir / f"{category}_seeds.md"
            try:
                seed_file.parent.mkdir(parents=True, exist_ok=True)
                with open(seed_file, "a") as f:
                    f.write(f"{search_url}\n")
                logger.info(f"[{self.name}] Saved {category} seed: {search_url[:80]}...")
            except Exception as e:
                logger.debug(f"[{self.name}] Failed to save seed: {e}")

    def _make_entry(
        self, org_name: str, title: str, url: str, location: str,
        description: str = "",
    ) -> Optional[WikiEntry]:
        record = None
        if self.registry is not None:
            try:
                record = self.registry.upsert(
                    org=org_name, title=title, url=url or "",
                    careers_url="", source=self.name,
                )
            except Exception as e:
                # A swallowed upsert is data loss, and the exception type is
                # what separates contract drift from a transient failure
                # (LEARNINGS 2026-09-20).
                logger.warning(f"[{self.name}] Registry upsert failed "
                               f"({type(e).__name__}): {e}")

        if self.matcher is None:
            return None  # nothing to do without a matcher

        # Parsers that can supply advert text use it (the Jack & Jill drop
        # carries the recruiter's own rationale). Everything else passes
        # location as the sparse context AND as the location field, so the
        # work-auth hard-kill and the LLM still see geography.
        context = (description or "").strip() or location or ""
        match = self.matcher.match(
            title, context, org_name, job_url=url or "", location=location or "",
        )
        entry = WikiEntry(
            org_name=org_name, title=title,
            url=url or None, location=location or None,
            score=match.score, decision=match.decision, reasoning=match.reasoning,
            source=self.name,
            advert_excerpt=context,
            **match_fields(match),
        )
        if record is not None:
            entry.lifecycle_status = record.status
            entry.first_seen = record.first_seen
        return entry


class GmailLjJobsSource(GmailJobsSource):
    """Legacy LJ source kept for existing callers and registry provenance."""

    name = "gmail_lj_jobs"

    def __init__(self, *args, **kwargs):
        if kwargs.get("label") is None:
            kwargs["label"] = GMAIL_LJ_JOBS_LABEL
        kwargs.setdefault("source_name", self.name)
        super().__init__(*args, **kwargs)


# ── Raw-email archive (replay without Gmail) ────────────────────────────────
#
# GmailJobsSource._archive_raw writes one record per processed email to
# <state>/sources/gmail/<label>/<msg_id>.json. These helpers read it back, so a
# parser change can be validated against the real corpus with no Gmail access:
#
#     from pathlib import Path
#     from sources.gmail_lj_jobs import replay_archived_jobs
#     jobs = replay_archived_jobs(Path("state/kj/sources"), "KJ-jobs")


def _archive_slug(label: str) -> str:
    """Filesystem-safe label directory name (`KJ-jobs` → `KJ-jobs`)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(label)).strip("_") or "label"


def iter_archived_emails(sources_dir, label: str) -> Iterator[dict]:
    """Yield archived email records for `label`, filename order.

    Each record carries the verbatim `body` the parsers saw, plus
    id/label/category/from/subject/archived_at.
    """
    label_dir = Path(sources_dir) / "gmail" / _archive_slug(label)
    for path in sorted(label_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            logger.warning(f"[gmail-archive] unreadable {path}: {e}")
            continue
        yield _with_current_category(record)


def _with_current_category(record: dict) -> dict:
    """Upgrade an archived record whose stored category predates its parser.

    The archive records the category as of the day the mail was processed, so a
    message backfilled before a parser existed keeps `"other"` for ever — and
    every consumer that dispatches on the stored category (`replay_archived_jobs`,
    `run_from_archive`, the per-source report) then skips it. That is exactly how
    the 60 backfilled Jack & Jill drops stayed invisible: 123 leads of the
    highest-value email channel, classified `other` one day before the parser
    landed. Classifying at read time fixes all three consumers at once, without
    rewriting the principal's private archive.
    """
    stored = str(record.get("category") or "")
    if GmailJobsSource._PARSERS.get(stored):
        return record
    fresh = classify_email({
        "from": record.get("from", ""),
        "subject": record.get("subject", ""),
    })
    if fresh != stored and GmailJobsSource._PARSERS.get(fresh):
        logger.info(
            f"[gmail-archive] {record.get('id', '?')}: reclassified {stored!r} -> {fresh!r}"
        )
        return {**record, "category": fresh, "category_reclassified_from": stored}
    return record


def _resolve_archived_category(record: dict) -> tuple:
    """How an archived record should be processed now: (category, parser_info).

    The recorded category wins when this version still knows it. Otherwise the
    mail was shelved before its parser existed, so the record is re-classified
    from the archived headers (`from`/`subject` are kept verbatim) — without
    that, those emails are unparseable forever.
    """
    category = str(record.get("category") or "")
    parser_info = GmailJobsSource._PARSERS.get(category)
    if parser_info and parser_info[0] is not None:
        return category, parser_info
    category = classify_email(record)
    parser_info = GmailJobsSource._PARSERS.get(category)
    if parser_info and parser_info[0] is not None:
        return category, parser_info
    return category, (None, None, False)


def replay_archived_jobs(sources_dir, label: str) -> List[dict]:
    """Re-parse every archived email with the current parsers.

    Returns the listing dicts a crawl would parse, each tagged with the archive
    `id` and `category` it came from.
    """
    jobs: List[dict] = []
    for record in iter_archived_emails(sources_dir, label):
        category, parser_info = _resolve_archived_category(record)
        if parser_info[0] is None:
            continue
        for job in parser_info[0](record.get("body", "")):
            jobs.append({
                **job,
                "id": record.get("id", ""),
                "category": category,
            })
    return jobs
