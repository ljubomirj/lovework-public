"""
Tests for the HN Algolia API helpers and the hn_hiring + hn_jobs sources.

Pure tests (no network): exercise the parsers with synthetic HN-shaped
data. Integration tests monkeypatch fetch_item to return canned responses,
proving the end-to-end thread → entries path works offline.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sources import hn_common
from sources.hn_hiring import HNHiringSource
from sources.hn_jobs import (
    HNHiringJobsSource,
    _age_text_to_days,
    _parse_jobs_html,
    _split_title_to_org_role,
)


# ── Title regex ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("title, expected", [
    ("Ask HN: Who is hiring (June 2026)", ("June", 2026)),
    ("Ask HN: Who is hiring? (June 2026)", ("June", 2026)),
    ("Ask HN: Who is hiring? (May 2026)", ("May", 2026)),
    ("Ask HN: Who is hiring  (April 2026)  ", ("April", 2026)),
    ("Ask HN: Who wants to be hired? (June 2026)", None),  # wrong thread
    ("Random story", None),
    ("", None),
])
def test_parse_hiring_thread_title(title, expected):
    assert hn_common.parse_hiring_thread_title(title) == expected


# ── Comment parser ────────────────────────────────────────────────────────

SAMPLE_COMMENT_HTML = (
    "<p>Anthropic | AI Research Engineer | San Francisco / Remote | "
    "https://anthropic.com/careers/123</p>"
    "<p>We are looking for an AI research engineer to work on...</p>"
)


def test_parse_hn_job_comment_basic():
    parsed = hn_common.parse_hn_job_comment({
        "objectID": 12345, "author": "lj", "text": SAMPLE_COMMENT_HTML,
    })
    assert parsed is not None
    assert parsed["company"] == "Anthropic"
    assert parsed["role"] == "AI Research Engineer"
    assert parsed["location"] == "San Francisco / Remote"
    assert "anthropic.com" in parsed["url"]
    assert "AI research engineer" in parsed["body"]


def test_parse_hn_job_comment_separator_dash():
    parsed = hn_common.parse_hn_job_comment({
        "objectID": 1, "text": "<p>Stripe – Software Engineer – Remote</p><p>Help us build...</p>",
    })
    assert parsed is not None
    assert parsed["company"] == "Stripe"
    assert parsed["role"] == "Software Engineer"
    assert parsed["location"] == "Remote"


def test_parse_hn_job_comment_skips_meta_lines():
    parsed = hn_common.parse_hn_job_comment({
        "objectID": 1, "text": "<p>my company is hiring. We are...</p>",
    })
    # The first non-skip line doesn't have a recognisable header.
    assert parsed is None


def test_parse_hn_job_comment_html_entities():
    parsed = hn_common.parse_hn_job_comment({
        "objectID": 1,
        "text": "<p>Acme | Engineer &amp; Manager | Remote</p><p>Job &amp; team</p>",
    })
    assert parsed is not None
    assert "Engineer" in parsed["role"] and "&amp;" not in parsed["role"]
    assert "Job & team" in parsed["body"]


def test_parse_hn_job_comment_normalises_firebase_provenance():
    parsed = hn_common.parse_hn_job_comment({
        "id": 48749307,
        "by": "azmaty",
        "time": 1782922755,
        "text": "<p>Talk Machine | Founding Engineers | Remote</p><p>Voice AI role.</p>",
    })
    assert parsed is not None
    assert parsed["hn_comment_id"] == 48749307
    assert parsed["hn_author"] == "azmaty"
    assert parsed["discovery_date"] == "2026-07-01"


def test_parse_hn_job_comment_no_header_returns_none():
    parsed = hn_common.parse_hn_job_comment({
        "objectID": 1, "text": "<p>just a random comment</p>",
    })
    assert parsed is None


# ── hn_hiring source end-to-end (offline) ─────────────────────────────────

SAMPLE_THREAD = {
    "objectID": 48357725,
    "kids": [100, 101, 102],
}

SAMPLE_COMMENTS = [
    {
        "objectID": 100, "parent_id": 48357725,
        "text": "<p>Anthropic | AI Engineer | Remote</p><p>Great role.</p>",
    },
    {
        "objectID": 101, "parent_id": 48357725,
        "text": "<p>Meta | Research Scientist | Menlo Park</p><p>PhD preferred.</p>",
    },
    {
        "objectID": 102, "parent_id": 48357725,
        "text": "<p>just a comment without header</p>",
    },
]


class _FakeRegistry:
    def __init__(self):
        self.upserts = []
    def upsert(
        self, org, title, url="", careers_url="", source="",
        discovery_url="", discovery_date="",
    ):
        self.upserts.append((org, title, discovery_url, discovery_date))
        return SimpleNamespace(status="new", first_seen="2026-06-23")


class _FakeMatcher:
    def __init__(self):
        self.calls = []
    def match(self, title, desc, org_name, job_url="", location=""):
        self.calls.append((title, org_name, location))
        return SimpleNamespace(score=7.5, decision="GO", reasoning=f"fit {title}")


def test_hn_hiring_source_end_to_end():
    """Run the source with a fixed thread_id and monkeypatched HTTP."""
    with patch.object(hn_common, "fetch_item") as fake_fetch:
        fake_fetch.side_effect = lambda oid: (
            SAMPLE_THREAD if oid == 48357725 else
            next((c for c in SAMPLE_COMMENTS if c["objectID"] == oid), None)
        )
        matcher = _FakeMatcher()
        registry = _FakeRegistry()
        src = HNHiringSource(matcher=matcher, registry=registry, thread_id=48357725)
        entries = src.run()

    # 2 valid job comments (Anthropic + Meta), 1 dropped (no header).
    assert len(entries) == 2
    titles = sorted(e.title for e in entries)
    assert titles == ["AI Engineer", "Research Scientist"]
    orgs = sorted(e.org_name for e in entries)
    assert orgs == ["Anthropic", "Meta"]
    # Registry saw both.
    assert any(row[:2] == ("Anthropic", "AI Engineer") for row in registry.upserts)
    assert any(row[:2] == ("Meta", "Research Scientist") for row in registry.upserts)
    # Matcher received location for the work-auth kill.
    assert any(loc == "Remote" for (_, _, loc) in matcher.calls)


def test_hn_hiring_source_no_thread_returns_empty():
    src = HNHiringSource(matcher=_FakeMatcher(), registry=_FakeRegistry(), thread_id=None)
    from sources import hn_hiring
    with patch.object(hn_hiring, "find_latest_hiring_thread_id", return_value=None):
        assert src.run() == []


def test_hn_hiring_source_handles_empty_thread():
    with patch.object(hn_common, "fetch_item") as fake_fetch:
        fake_fetch.return_value = {"objectID": 999, "kids": []}
        src = HNHiringSource(matcher=_FakeMatcher(), registry=_FakeRegistry(), thread_id=999)
        assert src.run() == []


# ── hn_jobs source end-to-end (offline) ──────────────────────────────────

SAMPLE_JOBS_HTML = """
<html><body>
<tr class="athing" id="111">
  <td class="title"><span class="age"><a href="item?id=111">2 days ago</a></span></td>
  <td class="title">  <a href="https://www.ycombinator.com/companies/great-question/jobs/J5TNvQH-ai-engineer-intern">Great Question (YC W21) Is Hiring Applied AI Interns (ycombinator.com)</a>  </td>
</tr>
<tr class="athing" id="222">
  <td class="title"><span class="age"><a href="item?id=222">1 hour ago</a></span></td>
  <td class="title">  <a href="https://stripe.com/jobs/ml-platform">Stripe Is Hiring ML Platform Engineer</a>  </td>
</tr>
<tr class="athing" id="333">
  <td class="title"><span class="age"><a href="item?id=333">2 months ago</a></span></td>
  <td class="title">  <a href="https://old.com/jobs/old">Old Job</a>  </td>
</tr>
</body></html>
"""


def test_parse_jobs_html_extracts_rows():
    rows = _parse_jobs_html(SAMPLE_JOBS_HTML)
    assert len(rows) == 3
    assert rows[0]["hn_id"] == "111"
    assert "Great Question" in rows[0]["title"]
    assert "ycombinator.com" in rows[0]["url"]
    assert rows[0]["age_text"] == "2 days ago"
    assert rows[0]["age_days"] == 2
    assert rows[2]["age_days"] == 60  # 2 months


@pytest.mark.parametrize("text, expected", [
    ("3 hours ago", 0),
    ("1 day ago", 1),
    ("5 days ago", 5),
    ("1 month ago", 30),
    ("2 months ago", 60),
    ("1 year ago", 365),
    ("", None),
    ("yesterday", None),
])
def test_age_text_to_days(text, expected):
    assert _age_text_to_days(text) == expected


@pytest.mark.parametrize("title, expected_org, expected_role_part", [
    ("Anthropic Is Hiring AI Engineers", "Anthropic", "AI Engineers"),
    ("Stripe Is Hiring Software Engineers, ML Platform", "Stripe", "Software Engineers, ML Platform"),
    ("Acme | ML Engineer | Remote", "Acme", "ML Engineer | Remote"),
    ("Solo Title", "Solo Title", "Solo Title"),
])
def test_split_title_to_org_role(title, expected_org, expected_role_part):
    org, role = _split_title_to_org_role(title)
    assert org == expected_org
    assert role == expected_role_part


def test_hn_jobs_source_end_to_end():
    """Run hn_jobs with a fake HTML response — should drop the 2-month-old entry."""
    with patch("sources.hn_jobs._get_html") as fake_get, \
         patch("sources.hn_jobs._fetch_meta_description", return_value=""):
        fake_get.return_value = SAMPLE_JOBS_HTML
        matcher = _FakeMatcher()
        registry = _FakeRegistry()
        src = HNHiringJobsSource(matcher=matcher, registry=registry)
        entries = src.run()

    # 2 kept (2 days, 1 hour), 1 dropped (2 months).
    assert len(entries) == 2
    assert all(entry.discovery_url.startswith("https://news.ycombinator.com/item?id=") for entry in entries)
    assert all(entry.discovery_date for entry in entries)
    titles = [e.title for e in entries]
    assert any("Great Question" in t for t in titles)
    assert any("Stripe" in t for t in titles)
    # Age text appended to reasoning.
    assert any("2 days ago" in e.reasoning or "1 hour ago" in e.reasoning for e in entries)


def test_hn_jobs_source_handles_no_html():
    with patch("sources.hn_jobs._get_html", return_value=None):
        assert HNHiringJobsSource(matcher=_FakeMatcher(), registry=_FakeRegistry()).run() == []
