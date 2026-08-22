"""Lead identity gates (P2 — identity integrity).

The title field must be the role title and the location field a place.
Parser output is not a title: email subjects ("Results from the new
AI-powered job search — 28 new jobs match your preferences."), salary
strings ("£120000 - £150000 per annum + equity + benefits"), relative
dates ("1 day ago"), and description paragraphs are identity garbage and
must be repaired or dropped at the source — never scored and surfaced.

Centralising the gates here keeps every source (Gmail parsers, HN
parsers, aggregators) on one definition instead of drifting per parser.
"""

from __future__ import annotations

import re

# Titles that are clearly not a role: email-subject / digest lines,
# salary strings, relative dates, generic employment labels, or URLs.
TITLE_NOISE_PATTERNS = [
    # Aggregator/email-subject digests.
    r"^\s*results?\s+from\s+the\s+new",
    r"new\s+jobs?\s+match",
    r"^\s*(top\s+)?job\s+picks",
    r"^\s*new\s+recommended",
    r"^\s*jobs?\s+that\s+match\s+your",
    r"^\s*(you\s+might\s+like|recommended\s+for\s+you)",
    # Salary / comp strings used as a title.
    r"^\s*[£$€]\s?\d[\d,]*",
    r"\bper\s+annum\b",
    r"\bpa\s*\+\s*(equity|bonus|benefits)",
    r"\bsalary\b",
    # Relative dates ("1 day ago") are never a title.
    r"^\s*\d+\s+(day|days|hour|hours|week|weeks|month|months)\s+ago\b",
    # URL / location-only fallbacks.
    r"^https?://",
    r"^\s*(remote|onsite|hybrid)\s*[-–]?\s*(remote|onsite|hybrid)?\s*$",
]

# Locations that are actually dates, digests, or email noise.
LOCATION_NOISE_PATTERNS = [
    r"^\s*\d+\s+(day|days|hour|hours|week|weeks|month|months)\s+ago\b",
    r"^\s*(new|updated|expired|closed|filled)\s*$",
    r"^\s*\d+\s*(new)?\s*jobs?\s+match",
]

# A company name is a short proper noun — a long prose sentence is a
# mis-split header (e.g. an em-dash inside a description paragraph).
MAX_COMPANY_WORDS = 6
MAX_ROLE_WORDS = 12
MIN_TITLE_CHARS = 4


def is_noise_title(title: str) -> bool:
    """True when a parsed title is not a role title (subject/salary/date/etc.)."""
    value = (title or "").strip()
    if len(value) < MIN_TITLE_CHARS:
        return True
    lowered = value.lower()
    if re.search(r"https?://", lowered):
        return True
    return any(re.search(p, lowered) for p in TITLE_NOISE_PATTERNS)


def is_noise_location(location: str) -> bool:
    """True when a parsed location is not a place (date/digest noise)."""
    lowered = (location or "").strip().lower()
    if not lowered:
        return False  # empty is fine — omitted, not mis-labelled
    return any(re.search(p, lowered) for p in LOCATION_NOISE_PATTERNS)


def is_implausible_header(company: str, role: str) -> bool:
    """True when a split header is probably a mis-split description.

    ``_split_header`` treats "|", "–", "—" as separators, so an em-dash
    inside a description paragraph (e.g. "...happy customers — and now
    we're ready to accelerate.") yields a prose company / prose role.
    A company is a short proper noun; a role is a short noun phrase.
    """
    company_words = re.findall(r"\S+", company or "")
    role_words = re.findall(r"\S+", role or "")
    if len(company_words) > MAX_COMPANY_WORDS:
        return True
    if len(role_words) > MAX_ROLE_WORDS:
        return True
    # A role that is really a sentence fragment (verb-led, long tail)
    # is a mis-split; keep it conservative and cheap.
    if re.search(r"\b(and|because|however|which|that|our)\b", (role or "").lower()):
        return len(role_words) >= 8
    return False
