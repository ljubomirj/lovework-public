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
  - Scans only `is:unread` LJ-jobs emails and marks each one read after extraction, so a
    scheduled run never reprocesses the same email.
  - Registry hash dedup prevents duplicate job records across emails/runs.
  - Gracefully no-ops (returns []) if Gmail is unavailable (no google_api.py / OAuth token).

Gmail access reuses the same google_api.py script as history.scan_gmail and lj-jobs-poll.
Classification + LinkedIn parsing are ported from lj-jobs-poll.py (LJ-work-2026 root).
TotalJobs, CWJobs, TalentSource, Rec-London, JobServe, Lensa, and Johnson Jobs
alerts use source-specific parsers below.
"""

import logging
import os
import re
from pathlib import Path
from typing import List, Optional

from job_registry import JobRegistry
from lead_identity import is_noise_location, is_noise_title
from matcher import JobMatcher
from wiki_store import WikiEntry, match_fields

# Reuse the LJ-jobs label from history (single source of truth) and the shared Gmail
# accessor (resolves the right Python for google_api.py).
from history import GMAIL_LJ_JOBS_LABEL
from gmail_accessor import run_gapi

logger = logging.getLogger(__name__)

# Tunables (env-overridable for tests / manual runs).
MAX_EMAILS = int(os.getenv("LOVEWORK_GMAIL_MAX_EMAILS", "40"))
# Mark processed emails read so cron doesn't reprocess them. Set
# LOVEWORK_GMAIL_MARK_READ=0 to leave mail untouched (e.g. dry debugging).
MARK_READ = os.getenv("LOVEWORK_GMAIL_MARK_READ", "1") != "0"
# Capture search-results URLs from alert footers and save them as related-harvest seeds. Set
# LOVEWORK_LI_CAPTURE_GMAIL_SEEDS=0 to disable.
CAPTURE_GMAIL_SEEDS = os.getenv("LOVEWORK_LI_CAPTURE_GMAIL_SEEDS", "1") != "0"


# Gmail access is provided by the shared gmail_accessor.run_gapi (resolves the right
# Python for google_api.py once, for both this source and history.scan_gmail).


# ── Classification (ported from lj-jobs-poll.py) ───────────────────────────

def classify_email(msg: dict) -> str:
    """Categorise a Gmail message by sender/subject."""
    sender = msg.get("from", "").lower()
    subject = msg.get("subject", "").lower()

    if "jobs-noreply@linkedin.com" in sender and "application was sent" in subject:
        return "linkedin_app_sent"
    if "jobalerts-noreply@linkedin.com" in sender:
        return "linkedin_alert"
    if "jobs-listings@linkedin.com" in sender:
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
    if "candidate.admin@apps.jobserve.com" in sender:
        return "jobserve_admin"
    if "jobsbyemail@apps.jobserve.com" in sender:
        return "jobserve_alert"
    if "jobserve.com" in sender and "job alert" in subject:
        return "jobserve_alert"
    if "aggregated@lensa.com" in sender or "jobalert@lensa.com" in sender:
        return "lensa_alert"
    if "alerts@johnsonjobs.com" in sender:
        return "johnsonjobs_alert"
    if "cv-library.co.uk" in sender and "new" in subject:
        return "cv_library_alert"
    return "other"


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


def parse_linkedin_alerts(body: str) -> List[dict]:
    """Extract individual job listings from a LinkedIn alert email body.

    Returns a list of dicts with keys title/company/location/url. The
    LinkedIn alert footer's "see all jobs" / "view this search" link is
    captured separately via extract_linkedin_search_url().
    """
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
    "hi ljubomir", "new jobs for you", "matching jobs for your alert",
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

OBVIOUS_NONFIT = [
    r"\bquant\b", r"\bquantitative\b", r"\btrader\b", r"\bportfolio\b",
    r"\bdata\s*analyst\b",
    r"\bsoftware\s*engineer\b(?!.*\b(ml|ai|machine|learning|research|llm|agent)\b)",
    r"\bfinanc\w*\b(?!.*\b(ml|ai|research|llm|agent|quant)\b)",
    r"\baccount\w*\b", r"\bmarket\w*\b", r"\boperat\w*\b",
]


def is_obvious_nonfit(title: str, company: str = "") -> bool:
    text = f"{title} {company}".lower()
    return any(re.search(p, text) for p in OBVIOUS_NONFIT)


# ── Source ─────────────────────────────────────────────────────────────────

class GmailJobsSource:
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
    ):
        self.matcher = matcher
        self.registry = registry
        self.max_emails = max_emails
        self.mark_read = mark_read
        self.label = label
        self.credential_home = credential_home
        self.name = source_name
        self.sources_dir = sources_dir
        self.capture_seeds = capture_seeds

    def _gapi(self, *args):
        """Call Gmail without changing legacy test and LJ call signatures."""
        if self.credential_home is None:
            return run_gapi(*args)
        return run_gapi(*args, credential_home=self.credential_home)

    def run(self) -> List[WikiEntry]:
        entries: List[WikiEntry] = []

        messages = self._gapi(
            "gmail", "search", f"label:{self.label} is:unread", "--max", str(self.max_emails)
        )
        if not messages:
            logger.info(f"[{self.name}] No unread {self.label} emails (or Gmail unavailable).")
            return entries

        logger.info(f"[{self.name}] {len(messages)} unread {self.label} email(s) to process")
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
                raw_jobs = parser_fn(body)
                if self.capture_seeds and seed_extractor_fn is not None:
                    try:
                        search_url = seed_extractor_fn(body)
                        if search_url:
                            self._save_seed(category, search_url)
                    except Exception as e:
                        logger.debug(f"[{self.name}] seed capture failed: {e}")

        produced: List[WikiEntry] = []
        # P3 — market gates at the boundary: Lensa is a US-market noise
        # source. When disabled, consume the email (mark read) but produce
        # no leads — the listings are not worth scoring for a UK principal.
        if category == "lensa_alert" and not _lensa_enabled():
            if self.mark_read and msg_id:
                self._gapi("gmail", "modify", msg_id, "--remove-labels", "UNREAD")
            logger.info(f"[{self.name}] Lensa disabled (US-market noise); consumed {category} {msg_id} without leads")
            return []
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
            if is_obvious_nonfit(job["title"], job.get("company", "")):
                continue
            entry = self._make_entry(
                org_name=job["company"], title=job["title"],
                url=job.get("url", ""), location=job.get("location", ""),
            )
            if entry is not None:
                produced.append(entry)

        # Never consume a recognised lead email whose parser found nothing.
        # A provider changing its template should leave a visible unread item
        # for inspection rather than silently dropping all of its leads.
        parsed_successfully = (not is_lead) or bool(raw_jobs)
        if is_lead and not raw_jobs:
            logger.warning(
                f"[{self.name}] {category} email {msg_id} yielded no listings; "
                "leaving it unread"
            )
        if self.mark_read and msg_id and parsed_successfully:
            self._gapi("gmail", "modify", msg_id, "--remove-labels", "UNREAD")
        return produced

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

    def _make_entry(self, org_name: str, title: str, url: str, location: str) -> Optional[WikiEntry]:
        record = None
        if self.registry is not None:
            try:
                record = self.registry.upsert(
                    org=org_name, title=title, url=url or "",
                    careers_url="", source=self.name,
                )
            except Exception as e:
                logger.debug(f"[{self.name}] Registry upsert failed: {e}")

        if self.matcher is None:
            return None  # nothing to do without a matcher

        # LinkedIn alerts carry no role description — pass location as the sparse context
        # AND as the location field (so the work-auth hard-kill + the LLM see geography).
        match = self.matcher.match(
            title, location or "", org_name, job_url=url or "", location=location or "",
        )
        entry = WikiEntry(
            org_name=org_name, title=title,
            url=url or None, location=location or None,
            score=match.score, decision=match.decision, reasoning=match.reasoning,
            source=self.name,
            advert_excerpt=location or "",
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
