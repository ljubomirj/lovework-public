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

logger = logging.getLogger(__name__)

# ── Locations ────────────────────────────────────────────────────────────
APPLICATIONS_DIR = config.APPLICATIONS_DIR
GMAIL_LJ_JOBS_LABEL = os.getenv("LOVEWORK_GMAIL_LABEL", "LJ-jobs")


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
    return list(aliases)


def scan_applications(org: str, applications_dir: Optional[Path] = None) -> List[PriorApplication]:
    """Scan applications/ for prior applications to this org.

    Args:
        org: organization name to search for
        applications_dir: override the configured APPLICATIONS_DIR (used in tests)
    """
    apps_dir = applications_dir or config.APPLICATIONS_DIR
    if not apps_dir.is_dir():
        return []

    aliases = _org_aliases(org)
    found: List[PriorApplication] = []

    for app_dir in sorted(apps_dir.iterdir()):
        if not app_dir.is_dir():
            continue
        dir_name = app_dir.name
        dir_lower = dir_name.lower()
        # Check if any alias matches
        if not any(alias in dir_lower for alias in aliases):
            continue

        # Parse the dir name: YYYYMMDD-Company-Role
        m = re.match(r"(\d{4})(\d{2})(\d{2})-(.+)", dir_name)
        if not m:
            continue
        yyyy, mm, dd, rest = m.groups()
        date = f"{yyyy}-{mm}-{dd}"
        parts = rest.split("-", 1)
        company = parts[0] if parts else rest
        role = parts[1] if len(parts) > 1 else ""

        # Look for the .txt file (the job ad copy)
        txt_files = list(app_dir.glob("*.txt"))
        txt_path = txt_files[0] if txt_files else None
        has_rejection = False
        rejection_date = None

        if txt_path:
            try:
                content = txt_path.read_text(encoding="utf-8", errors="ignore")
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

        found.append(PriorApplication(
            date=date,
            company=company,
            role=role.replace("_", " "),
            path=app_dir,
            txt_path=txt_path,
            has_rejection=has_rejection,
            rejection_date=rejection_date,
        ))

    return found


# ── Gmail scanner ───────────────────────────────────────────────────────

def scan_gmail(org: str, max_results: int = 10) -> List[GmailEvent]:
    """Scan Gmail LJ-Jobs label for emails related to this org.

    Uses the shared gmail_accessor (google_api.py via the correct Python interpreter).
    Returns empty list if Gmail is not available or the query fails.
    """
    from gmail_accessor import run_gapi

    # Build a Gmail search query: from:*org* OR subject:*org* within LJ-Jobs label.
    aliases = _org_aliases(org)
    org_query = " OR ".join(f'"{a}"' for a in aliases[:3])  # Limit to 3 aliases
    query = f'label:{GMAIL_LJ_JOBS_LABEL} ({org_query})'

    messages = run_gapi("gmail", "search", query, "--max", str(max_results))
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


def _classify_email(subject: str, from_: str) -> str:
    """Classify a Gmail event by its subject/sender."""
    s = subject.lower()
    f = from_.lower()
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

def scan_history(org: str, use_gmail: bool = True, applications_dir: Optional[Path] = None) -> PriorContact:
    """Scan both applications/ and Gmail for prior contact with an org.

    Args:
        org: organization name to search for
        use_gmail: whether to also scan Gmail (requires google_api.py)
        applications_dir: override the configured APPLICATIONS_DIR (used in tests)
    """
    apps = scan_applications(org, applications_dir=applications_dir)
    gmail = scan_gmail(org) if use_gmail else []
    return PriorContact(org=org, applications=apps, gmail_events=gmail)
