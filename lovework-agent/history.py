"""
History scanner — finds prior applications and correspondence for an org.

Two sources:
1. applications/ directory (local filesystem, e.g. applications/20251220-Poetiq_ai-AI_Scientist/)
2. Gmail (via existing lj-jobs-poll.py infrastructure)

For each org, returns a PriorContact object with:
- applications: list of {date, role, path, status_guess}
- gmail_events: list of {date, subject, from, snippet, kind}
- summary: human-readable summary for the matcher
"""

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import config
from outcome_link import read_case_identity, read_case_outcome_markers

logger = logging.getLogger(__name__)

# ── Locations ────────────────────────────────────────────────────────────
APPLICATIONS_DIR = config.APPLICATIONS_DIR
GMAIL_LJ_JOBS_LABEL = os.getenv("LOVEWORK_GMAIL_LABEL", "LJ-jobs")
PREPARATION_MARKERS = (
    "LoveWork status: PREPARED — not submitted",
    "LoveWork status: CUT — scaffold only, not yet prepared",
    "LoveWork ATA status: PREPARED — interview not started",
)
#: The CUT marker joined the list 2026-10-18, the day markers became honest:
#: a LoveWork-cut scaffold is a case directory, not a manual application, and
#: must not surface as prior contact either — the marker's *word* is not the
#: protection's question ("is this directory LoveWork's?"), the prefix is.


@dataclass
class PriorApplication:
    """A prior application found in applications/."""

    date: str  # YYYY-MM-DD extracted from dir name
    company: str
    role: str
    path: Path
    txt_path: Optional[Path] = None
    has_rejection: bool = False
    rejection_date: Optional[str] = None
    # Exact advert identity, read from the case dossier that
    # application_packs.prepare_go_cases writes beside the application
    # (application_packs.py: `Advert identity: <hash>`, 2026-09-17). Empty for
    # legacy case directories that predate the dossier, in which case the
    # outcome row must be joined by a weaker rung (see outcome_link.py).
    advert_hash: str = ""
    title: str = ""  # the advertised position, not the directory slug
    url: str = ""  # the primary advert URL, when the dossier recorded one
    decision: str = ""  # LoveWork's initial decision on that advert
    initial_score: Optional[float] = None
    outcome_markers: List[tuple] = field(default_factory=list)  # (kind, date)


@dataclass
class GmailEvent:
    """A Gmail event related to this org."""

    date: str
    subject: str
    from_: str
    snippet: str
    kind: str  # application_sent, rejection, interview, offer, other


@dataclass
class PriorContact:
    """All prior contact with an org."""

    org: str
    applications: List[PriorApplication] = field(default_factory=list)
    gmail_events: List[GmailEvent] = field(default_factory=list)

    @property
    def has_application(self) -> bool:
        return len(self.applications) > 0

    @property
    def has_rejection(self) -> bool:
        return any(a.has_rejection for a in self.applications) or any(
            e.kind == "rejection" for e in self.gmail_events
        )

    @property
    def last_contact_date(self) -> Optional[str]:
        dates = []
        for a in self.applications:
            dates.append(a.date)
            if a.rejection_date:
                dates.append(a.rejection_date)
        for e in self.gmail_events:
            dates.append(e.date)
        return max(dates) if dates else None

    def summary(self) -> str:
        """Human-readable summary for the matcher."""
        if not self.applications and not self.gmail_events:
            return "No prior contact found."

        parts = []
        for a in self.applications:
            s = f"Applied {a.date} for '{a.role}'"
            if a.rejection_date:
                s += f", rejection received {a.rejection_date}"
            parts.append(s)
        for e in self.gmail_events:
            parts.append(f"Email {e.date}: {e.kind} — {e.subject[:100]}")
        return "; ".join(parts)


# ── applications/ scanner ────────────────────────────────────────────────

def _org_aliases(org: str) -> List[str]:
    """Generate search aliases for an org name.

    e.g. "FAR.AI" -> ["far.ai", "far ai", "farai"]
         "OpenAI Residency" -> ["openai", "open ai"]

    **…and the reverse direction, which was missing** (2026-10-18). A *concatenated*
    name — exactly the shape an applicant-tracking slug has
    (`apply.workable.com/huggingface/…`) — expanded to only itself, while the spaced
    form expanded to its separator variants. So `huggingface` could not match
    `Hugging Face`, and four HuggingFace GOs filed under two HF *Space* names stayed
    on the active list after a rejection. The compact form (alphanumerics only) is now
    an alias of every name, so an ATS slug and an org name meet in the middle.
    """
    aliases = {org.lower().strip()}
    # Strip punctuation (spaces -> spaces, not removed)
    aliases.add(re.sub(r"[^\w\s]", " ", org.lower()).strip())
    # Strip common suffixes
    for suffix in (" ai", "ai", " inc", " ltd", " labs", " lab", " research"):
        if org.lower().endswith(suffix):
            aliases.add(org.lower()[: -len(suffix)].strip())
    # Add stripped version (no dots/commas)
    clean = org.lower().replace(".", "").replace(",", "").strip()
    aliases.add(clean)
    # Also add variants with underscores and hyphens (directory names
    # use underscores between words, e.g. "Longshot_Systems")
    for base in list(aliases):
        aliases.add(base.replace(" ", "_"))
        aliases.add(base.replace(" ", "-"))
    # The compact form — separators removed entirely — so a slug matches a name and a
    # name matches a slug. This is the direction that was missing: `huggingface` and
    # `Hugging Face` are the same employer, and only one of them was reachable.
    for base in list(aliases):
        compact = re.sub(r"[^a-z0-9]", "", base.lower())
        if compact:
            aliases.add(compact)
    # **An empty alias matches every case** (2026-10-20). Stripping the "ai"
    # suffix off a name that *is* "AI" left `""` in the table, and `"" in
    # anything` is true — so asking the history scanner about "AI" returned
    # every directory in `applications/`, which the matcher turns into a blanket
    # reapply kill. The table never carries an empty string now; the guards at
    # the call sites are gone with it.
    return [alias for alias in aliases if alias.strip()]


@dataclass(frozen=True)
class CaseDirName:
    """The three facts a case directory's name carries.

    `Hugging_Face` -> date `2026-09-02`, company `Hugging_Face`, role
    `Senior_Machine_Learning_Engineer_Voice_Agents`. The company ends at the
    **first** dash after the date and the role is everything after it, because
    that is how these directories have been written since before LoveWork.
    """

    date: str            # ISO `YYYY-MM-DD` as the prefix spells it
    company: str         # the directory's spelling, underscores kept
    role: str            # the remainder; "" for a directory with no role part


def parse_case_dirname(directory_name: str) -> Optional[CaseDirName]:
    """Split a `YYYYMMDD-Company-Role` name — **one parser for the project**.

    Every reader of `applications/` asks this rather than carrying its own
    regex: the history scanner, the manual-corpus index (`manual_projects`) and
    anything that needs to know which organisation a case is about. The prefix
    is validated by shape, not by the calendar — a `20261340-` directory parses
    here and is rejected by whoever needs a real date.
    """
    match = re.match(r"(\d{4})(\d{2})(\d{2})[-_](.+)", directory_name or "")
    if not match:
        return None
    yyyy, mm, dd, rest = match.groups()
    # The company ends at the first **dash**, and only a dash: `_` separates
    # words inside a name (`Hugging_Face`) and is often the *only* thing in the
    # name (`20250723-Tufa_Labs`, no role part), so cutting at it would read
    # `Tufa` as the employer. A directory with no dash therefore carries its
    # whole remainder as the company, exactly as before.
    company, _, role = rest.partition("-")
    return CaseDirName(date=f"{yyyy}-{mm}-{dd}", company=company or rest, role=role)


def case_dir_matches_org(
    directory_name: str, org: str, *, parsed: Optional[CaseDirName] = None
) -> bool:
    """Is this case directory about `org`? — the project's one directory test.

    Two ways to agree, both needed: an alias appears anywhere in the directory
    name (the loose test the scanner was written for), or the **company segment
    folded to alphanumerics** equals a folded alias — so an applicant-tracking
    slug and a spaced name meet in the middle (the HuggingFace defect of
    2026-10-18, see `_org_aliases`).
    """
    parsed = parsed if parsed is not None else parse_case_dirname(directory_name)
    if parsed is None:
        return False
    aliases = _org_aliases(org)
    if any(alias in directory_name.lower() for alias in aliases):
        return True
    company_compact = re.sub(r"[^a-z0-9]", "", parsed.company.lower())
    return bool(company_compact and company_compact in {
        re.sub(r"[^a-z0-9]", "", alias) for alias in aliases})


def org_matches_text(text: str, org: str) -> bool:
    """Does this free text name `org`? The same alias table, on a written field.

    For the case when the principal's own words carry the employer's name that
    the directory slug does not: `20260614-EQL_Tech-...` is filed with
    `company: EQL Global`, and nothing in the slug says Global. One alias
    table, applied to a different surface — not a second matcher.
    """
    folded = (text or "").lower()
    if not folded:
        return False
    compact = re.sub(r"[^a-z0-9]", "", folded)
    for alias in _org_aliases(org):
        if alias in folded or re.sub(r"[^a-z0-9]", "", alias) in compact:
            return True
    return False


def scan_applications(org: str, applications_dir: Optional[Path] = None) -> List[PriorApplication]:
    """Scan applications/ for prior applications to this org.

    Args:
        org: organization name to search for
        applications_dir: override the configured APPLICATIONS_DIR (used in tests)
    """
    apps_dir = applications_dir or config.APPLICATIONS_DIR
    if not apps_dir.is_dir():
        return []

    found: List[PriorApplication] = []

    for app_dir in sorted(apps_dir.iterdir()):
        if not app_dir.is_dir():
            continue
        dir_name = app_dir.name
        parsed = parse_case_dirname(dir_name)
        if parsed is None:
            continue
        # **Match on the parsed company, folded** as well as on the whole name —
        # the rule and its history live in `case_dir_matches_org`.
        if not case_dir_matches_org(dir_name, org, parsed=parsed):
            continue
        date = parsed.date
        company = parsed.company
        role = parsed.role

        # The case file: `<dir>.txt` when the principal wrote it, `<dir>.md` when
        # LoveWork cut the pack (LJ, 2026-10-15), and either way the file that
        # matches the directory's own name.
        from application_packs import dossier_path

        txt_path = dossier_path(app_dir)
        has_rejection = False
        rejection_date = None

        if txt_path:
            try:
                content = txt_path.read_text(encoding="utf-8", errors="ignore")
                # LoveWork preparation dossiers live beside real applications,
                # but neither a GO review pack nor an unstarted agent-to-agent
                # interview is evidence that the principal applied. Explicit
                # markers protect outcome and reapply logic until submission
                # or interview start is confirmed.
                if any(marker in content for marker in PREPARATION_MARKERS):
                    continue
                # Look for rejection markers
                rej_match = re.search(
                    r"rejection\s*(?:received|email)?\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})",
                    content, re.I
                )
                if rej_match:
                    has_rejection = True
                    rejection_date = rej_match.group(1)
                # Also check for "regret" / "not moving forward" / "unfortunately"
                elif re.search(r"(regret|not moving forward|not to move forward|not moving|unfortunately|won't be proceeding|decided not to)", content, re.I):
                    has_rejection = True
            except Exception as e:
                logger.debug(f"Could not read {txt_path}: {e}")

        identity = read_case_identity(app_dir)
        found.append(PriorApplication(
            date=date,
            company=company,
            role=role.replace("_", " "),
            path=app_dir,
            txt_path=txt_path,
            has_rejection=has_rejection,
            rejection_date=rejection_date,
            advert_hash=identity.advert_hash if identity else "",
            title=identity.title if identity else "",
            url=identity.url if identity else "",
            decision=identity.decision if identity else "",
            initial_score=identity.initial_score if identity else None,
            outcome_markers=read_case_outcome_markers(app_dir),
        ))

    return found


# ── Gmail scanner ───────────────────────────────────────────────────────

def scan_gmail(
    org: str,
    max_results: int = 10,
    label: str = GMAIL_LJ_JOBS_LABEL,
    credential_home: Optional[Path] = None,
) -> List[GmailEvent]:
    """Scan one approved Gmail label for emails related to this org.

    Uses the shared gmail_accessor (google_api.py via the correct Python interpreter).
    Returns empty list if Gmail is not available or the query fails.
    """
    from gmail_accessor import run_gapi

    # Build a Gmail search query: from:*org* OR subject:*org* within the approved label.
    aliases = _org_aliases(org)
    org_query = " OR ".join(f'"{a}"' for a in aliases[:3])  # Limit to 3 aliases
    query = f'label:{label} ({org_query})'

    gapi_kwargs = {"credential_home": credential_home} if credential_home is not None else {}
    messages = run_gapi("gmail", "search", query, "--max", str(max_results), **gapi_kwargs)
    if not messages:
        return []

    events = []
    for msg in messages:
        subject = msg.get("subject", "")
        from_ = msg.get("from", "")
        # Parse date: Gmail returns e.g. "Fri, 12 Jun 2026 14:30:00 +0100"
        date = ""
        raw_date = msg.get("date", "")
        if raw_date:
            # Strip trailing timezone name e.g. " (UTC)", " (BST)"
            clean = re.sub(r"\s*\([^)]*\)\s*$", "", raw_date).strip()
            for fmt in (
                "%a, %d %b %Y %H:%M:%S %z",
                "%d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y",
                "%Y-%m-%d",
            ):
                try:
                    date = datetime.strptime(clean, fmt).strftime("%Y-%m-%d")
                    break
                except (ValueError, IndexError):
                    pass
            if not date:
                m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw_date)
                date = m.group(0) if m else raw_date[:10]

        # Classify the event
        kind = _classify_email(subject, from_)
        events.append(GmailEvent(
            date=date, subject=subject, from_=from_, snippet=msg.get("snippet", ""), kind=kind,
        ))

    return events


#: A job-alert newsletter is **not** an outcome, whatever its subject names.
#: Measured 2026-11-09 (`docs/52` §1.2): 44 "JJ Alerts" digests and six other
#: boards' alerts were classified `interview` because their subject names a
#: company called **Trading Interview** — the word matched, the event did not
#: exist. All 50 rows in lj's `interview`/`offer` kinds were this shape.
_ALERT_SENDER = re.compile(
    r"(alerts?@|jobalerts|jobleads|jobsbyemail|cwjobs|totaljobs|talentsource|"
    r"lensa|jobserve)", re.I)
_ALERT_SUBJECT = re.compile(
    r"^(jobs at |new jobs|new job post|job alert|your job alert|jobs you|"
    r"weekly job|\d+ new jobs|hand-picked roles)", re.I)


def _is_job_alert(subject: str, from_: str) -> bool:
    """True for a board's digest — a weekly "Jobs at X, Y and Z" mail."""
    return bool(_ALERT_SUBJECT.search(subject or "") or _ALERT_SENDER.search(from_ or ""))


def _classify_email(subject: str, from_: str) -> str:
    """Classify a Gmail event by its subject/sender."""
    s = subject.lower()
    f = from_.lower()
    if _is_job_alert(subject, from_):
        return "job_alert"
    if "application was sent" in s:
        return "application_sent"
    if any(kw in s for kw in ("rejection", "regret", "not moving forward", "won't be proceeding",
                               "unfortunately", "decided not to", "position has been filled")):
        return "rejection"
    if any(kw in s for kw in ("interview", "phone screen", "next steps", "schedule")):
        return "interview"
    if any(kw in s for kw in ("offer", "congratulations")):
        return "offer"
    return "other"


# ── Combined scanner ────────────────────────────────────────────────────

def scan_history(
    org: str,
    use_gmail: bool = True,
    applications_dir: Optional[Path] = None,
    gmail_label: str = GMAIL_LJ_JOBS_LABEL,
    gmail_credential_home: Optional[Path] = None,
) -> PriorContact:
    """Scan both applications/ and Gmail for prior contact with an org.

    Args:
        org: organization name to search for
        use_gmail: whether to also scan Gmail (requires google_api.py)
        applications_dir: override the configured APPLICATIONS_DIR (used in tests)
        gmail_label: approved label to query; defaults to LJ's legacy source
        gmail_credential_home: optional token home for a non-LJ mailbox source
    """
    apps = scan_applications(org, applications_dir=applications_dir)
    gmail = (
        scan_gmail(org, label=gmail_label, credential_home=gmail_credential_home)
        if use_gmail
        else []
    )
    return PriorContact(org=org, applications=apps, gmail_events=gmail)
