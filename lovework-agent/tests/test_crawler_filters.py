"""
Tests for the crawler filters (location + recency).

The filters drop:
- US-only locations
- Plain "Remote" (which usually means US-only)
- Jobs older than 4 weeks

These are deterministic rules — pure-Python tests, no LLM needed.
"""

import pytest

from crawler import is_location_acceptable, is_recent
from matcher import _titles_similar


# ── Location tests ──────────────────────────────────────────────────────

@pytest.mark.parametrize("loc,expected", [
    # UK locations — accept
    ("London, UK", True),
    ("Edinburgh, Scotland", True),
    ("Cambridge, UK", True),
    # EU locations — accept
    ("Paris, France", True),
    ("Berlin, Germany", True),
    ("Amsterdam, Netherlands", True),
    ("Zurich, Switzerland", True),
    # Remote with EU/global qualifier — accept
    ("Remote, EU", True),
    ("Remote, Global", True),
    ("Remote, Worldwide", True),
    ("Remote, Anywhere", True),
    ("Remote (UK)", True),
    ("Remote (International)", True),
    ("Hybrid (International)", True),
    # US-only — reject
    ("San Francisco, CA", False),
    ("New York, NY", False),
    ("Boston, MA", False),
    ("Seattle, WA", False),
    ("Remote (US)", False),
    ("Remote, US only", False),
    # Plain "Remote" without qualifier — reject (US-timezone assumed)
    ("Remote", False),
    ("Hybrid", False),
    # Unknown / missing — accept (let LLM decide later)
    (None, True),
    ("", True),
])
def test_is_location_acceptable(loc, expected):
    assert is_location_acceptable(loc) == expected


def test_location_acceptable_handles_semicolon_separated():
    """Semicolon-separated locations: accept if ANY option is UK/EU."""
    # Has international remote — accept
    assert is_location_acceptable(
        "Remote (International); Berkeley Office; Remote (US)"
    ) is True
    # Only US options — reject
    assert is_location_acceptable("Berkeley Office; Remote (US)") is False
    # UK + US mix — accept (UK is one option)
    assert is_location_acceptable("London, UK; New York, NY") is True


def test_location_acceptable_handles_pipe_separated():
    """Pipe-separated locations work too."""
    assert is_location_acceptable("London, UK | New York, NY") is True


# ── Recency tests ───────────────────────────────────────────────────────

@pytest.mark.parametrize("date,expected", [
    ("2 weeks ago", True),
    ("3 weeks ago", True),
    ("5 weeks ago", False),
    ("1 month ago", True),
    ("2 months ago", False),
    ("Today", True),
    ("Yesterday", True),
    (None, True),  # missing — be conservative
    ("", True),
])
def test_is_recent(date, expected):
    assert is_recent(date) == expected


def test_is_recent_iso_date_within_window():
    """An ISO date within 4 weeks is recent."""
    from datetime import datetime, timedelta
    recent = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    assert is_recent(recent) is True


def test_is_recent_iso_date_outside_window():
    """An ISO date more than 4 weeks ago is not recent."""
    from datetime import datetime, timedelta
    old = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    assert is_recent(old) is False


# ── Title similarity (for re-apply detection) ───────────────────────────

@pytest.mark.parametrize("a,b,expected", [
    ("AI Scientist", "AI Scientist", True),         # exact
    ("AI Scientist", "AI Engineer", False),         # different role
    ("Research Scientist", "Senior Research Scientist", True),  # seniority
    ("ML Engineer", "Machine Learning Engineer", False),  # different wording
    ("Software Engineer", "Senior Software Engineer", True),
    ("Data Scientist", "Senior Data Scientist", True),
    ("Research Engineer", "ML Research Engineer", True),  # shared "research engineer"
    ("Product Manager", "Engineering Manager", False),  # different role
])
def test_titles_similar(a, b, expected):
    assert _titles_similar(a, b) == expected


def test_titles_similar_case_insensitive():
    """Title matching is case-insensitive."""
    assert _titles_similar("AI SCIENTIST", "ai scientist") is True
    assert _titles_similar("Research Engineer", "RESEARCH ENGINEER") is True
