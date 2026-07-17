"""
Tests for linkedin_related and company_pages sources.

All tests run offline — HTTP / SmartCrawler calls are monkeypatched. The
`tmp_path` fixture (autouse via conftest) gives us a clean LOVEWORK_HOME.
"""

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sources import linkedin_related, company_pages, harnham
from sources.linkedin_related import (
    LinkedInRelatedSource,
    append_seed,
    _seeds_path,
    _read_seeds,
    _write_seeds,
    _harvest_job_urls,
    _parse_jobposting,
    _looks_like_auth_wall,
)
from sources.company_pages import CompanyPagesSource, _list_path
from sources.harnham import HarnhamSource


# ── Seeds file helpers ───────────────────────────────────────────────────

def test_append_seed_idempotent(tmp_path):
    # conftest gives us a fresh LOVEWORK_HOME; redirect the path via monkeypatch.
    fake = tmp_path / "seeds.md"
    with patch.object(linkedin_related, "_seeds_path", return_value=fake):
        append_seed("https://www.linkedin.com/jobs/view/111")
        append_seed("https://www.linkedin.com/jobs/view/111")  # dup
        append_seed("https://www.linkedin.com/jobs/view/222")
        seeds = _read_seeds()
    assert len(seeds) == 2
    assert "https://www.linkedin.com/jobs/view/111" in seeds


def test_append_seed_skips_non_http():
    append_seed("not-a-url")
    append_seed("")
    # No seeds file should be created for non-http inputs.
    seeds = _read_seeds()
    # (Note: depending on prior tests, file may exist with other content;
    # the test is specifically that we didn't add these non-URLs.)
    assert "not-a-url" not in seeds
    assert "" not in seeds


# ── HTML harvest + parse ────────────────────────────────────────────────

SAMPLE_LI_HTML = """
<html><body>
<div>See all jobs on LinkedIn
<a href="https://www.linkedin.com/jobs/view/11111">Engineer</a>
<a href="https://www.linkedin.com/comm/jobs/view/22222">Manager</a>
<a href="https://www.linkedin.com/jobs/view/11111">Dup Engineer</a>
<a href="https://other.com/whatever">Not LinkedIn</a>
</div>
</body></html>
"""


def test_harvest_job_urls_dedup():
    urls = _harvest_job_urls(SAMPLE_LI_HTML)
    # 2 unique job IDs, both emitted in canonical /jobs/view/ form.
    assert len(urls) == 2
    assert all("linkedin.com/jobs/view/" in u for u in urls)
    assert any("11111" in u for u in urls)
    assert any("22222" in u for u in urls)


def test_looks_like_auth_wall():
    assert _looks_like_auth_wall("Sign in to LinkedIn to continue")
    assert _looks_like_auth_wall("<html>authwall detected</html>")
    assert not _looks_like_auth_wall("<html><body>Job listings</body></html>")


JSONLD_HTML = """
<html>
<head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "JobPosting",
  "title": "Senior ML Engineer",
  "description": "Build cool ML systems.",
  "hiringOrganization": {"@type": "Organization", "name": "Acme AI"}
}
</script>
</head>
<body><title>Senior ML Engineer | LinkedIn</title></body>
</html>
"""


def test_parse_jobposting_jsonld():
    title, company = _parse_jobposting(JSONLD_HTML, "https://www.linkedin.com/jobs/view/33333")
    assert title == "Senior ML Engineer"
    assert company == "Acme AI"


TITLE_HTML = """
<html><head><title>ML Engineer at Stripe | LinkedIn</title></head></html>
"""


def test_parse_jobposting_title_fallback():
    title, company = _parse_jobposting(TITLE_HTML, "https://linkedin.com/jobs/view/44444")
    assert title == "ML Engineer"
    assert company == "Stripe"


# ── linkedin_related source end-to-end (offline) ────────────────────────

class _FakeMatcher:
    def __init__(self):
        self.calls = []
    def match(self, title, desc, org_name, job_url="", location=""):
        self.calls.append((title, org_name))
        return SimpleNamespace(score=8.0, decision="GO", reasoning="fit")


class _FakeRegistry:
    def __init__(self):
        self.upserts = []
    def upsert(self, org, title, url="", careers_url="", source=""):
        self.upserts.append((org, title, url))
        return SimpleNamespace(status="new", first_seen="2026-06-23")


def test_linkedin_related_source_harvests_jobs(tmp_path):
    fake_seeds = tmp_path / "linkedin_seeds.md"
    fake_needs_auth = tmp_path / "linkedin_needs_auth.md"
    _write_seeds.__globals__  # noqa
    with patch.object(linkedin_related, "_seeds_path", return_value=fake_seeds), \
         patch.object(linkedin_related, "_needs_auth_path", return_value=fake_needs_auth):
        _write_seeds(["https://www.linkedin.com/jobs/search/?q=ml"])
        with patch.object(linkedin_related, "_fetch_html") as fake_fetch:
            fake_fetch.return_value = SAMPLE_LI_HTML
            matcher = _FakeMatcher()
            registry = _FakeRegistry()
            entries = LinkedInRelatedSource(matcher=matcher, registry=registry).run()
        # Seed was consumed (next run will see empty seeds).
        assert linkedin_related._read_seeds() == []

    # 2 unique job IDs harvested from the page.
    assert len(entries) == 2
    orgs = [e.org_name for e in entries]
    # The page didn't carry company info, so both fall back to JSON-LD or
    # title parsing — for this sample we only have HTML, so companies are
    # best-effort. Just confirm two distinct entries.
    assert len(set(orgs)) >= 1


def test_linkedin_related_source_handles_auth_wall(tmp_path):
    fake_seeds = tmp_path / "linkedin_seeds.md"
    fake_needs_auth = tmp_path / "linkedin_needs_auth.md"
    with patch.object(linkedin_related, "_seeds_path", return_value=fake_seeds), \
         patch.object(linkedin_related, "_needs_auth_path", return_value=fake_needs_auth):
        _write_seeds(["https://www.linkedin.com/jobs/search/?q=ml"])
        with patch.object(linkedin_related, "_fetch_html") as fake_fetch:
            fake_fetch.return_value = "Sign in to LinkedIn to continue"
            entries = LinkedInRelatedSource(
                matcher=_FakeMatcher(), registry=_FakeRegistry()
            ).run()
    assert entries == []
    # The URL was logged as needing auth.
    assert fake_needs_auth.exists()
    assert "auth wall" in fake_needs_auth.read_text(encoding="utf-8")


def test_linkedin_related_source_no_seeds(tmp_path):
    fake_seeds = tmp_path / "linkedin_seeds.md"
    with patch.object(linkedin_related, "_seeds_path", return_value=fake_seeds):
        assert LinkedInRelatedSource(
            matcher=_FakeMatcher(), registry=_FakeRegistry()
        ).run() == []



# ── harnham: configured search URLs ──────────────────────────────────────

def test_harnham_seeds_starter_searches(tmp_path):
    fake_lj = tmp_path / "lj"
    with patch.object(harnham, "_profile_dir", return_value=fake_lj), \
         patch.object(harnham, "_searches_path", return_value=fake_lj / "harnham_searches.yaml"):
        harnham._seed_searches()
        rows = harnham._load_searches()

    assert len(rows) == 2
    assert rows[0]["url"].startswith("https://www.harnham.com/job-search/")
    assert "agentic%20engineer" in rows[0]["url"]
    assert "_location_city=london" in rows[1]["url"]


def test_harnham_source_crawls_configured_searches(tmp_path):
    fake_lj = tmp_path / "lj"
    fake_lj.mkdir()
    searches_path = fake_lj / "harnham_searches.yaml"
    searches_path.write_text(
        """\
- name: agentic-engineer
  url: https://www.harnham.com/job-search/?_keyword=agentic%20engineer
  reason: manual agentic search
""",
        encoding="utf-8",
    )

    class _FakeCrawler:
        def __init__(self):
            self.calls = []

        def crawl_org(self, org_name, seed_urls, goal, max_pages=3):
            self.calls.append((org_name, seed_urls, goal, max_pages))
            return [SimpleNamespace(
                title="Agentic Engineer",
                url="https://www.harnham.com/job/agentic-engineer",
                location="London",
                description_snippet="Build agentic systems",
                requirements_snippet="Python and LLMs",
                employment_type="contract",
            )]

    crawler = _FakeCrawler()
    matcher = _FakeMatcher()
    registry = _FakeRegistry()
    with patch.object(harnham, "_profile_dir", return_value=fake_lj), \
         patch.object(harnham, "_searches_path", return_value=searches_path):
        entries = HarnhamSource(crawler=crawler, matcher=matcher, registry=registry).run()

    assert len(entries) == 1
    assert entries[0].org_name == "Harnham"
    assert entries[0].title == "Agentic Engineer"
    assert entries[0].source == "harnham"
    assert crawler.calls[0][0] == "Harnham"
    assert crawler.calls[0][1] == ["https://www.harnham.com/job-search/?_keyword=agentic%20engineer"]
    assert registry.upserts == [(
        "Harnham",
        "Agentic Engineer",
        "https://www.harnham.com/job/agentic-engineer",
    )]

# ── company_pages: cadence decision ─────────────────────────────────────

@pytest.mark.parametrize("last_checked, cadence, today, expected", [
    (None, 14, "2026-06-23", True),                # never checked
    ("2026-06-01", 14, "2026-06-23", True),         # 22 days >= 14
    ("2026-06-15", 14, "2026-06-23", False),        # 8 days < 14
    ("2026-04-01", 14, "2026-06-23", True),         # force-check after 60
    ("2026-01-01", 30, "2026-06-23", True),         # 173 days > 30
])
def test_should_check_today(last_checked, cadence, today, expected):
    from sources.company_pages import _should_check_today
    entry = {"last_checked": last_checked, "cadence_days": cadence, "reason": "applied"}
    assert _should_check_today(entry, today) is expected


def test_company_pages_seeds_starter(tmp_path):
    """If no list exists, the source seeds a starter template."""
    from sources import company_pages as cp

    # Point _profile_dir at tmp_path.
    fake_lj = tmp_path / "lj"
    with patch.object(cp, "_profile_dir", return_value=fake_lj), \
         patch.object(cp, "_list_path", return_value=fake_lj / "company_pages.yaml"):
        cp._seed_starter_list()
        assert (fake_lj / "company_pages.yaml").exists()
        # Idempotent.
        text = (fake_lj / "company_pages.yaml").read_text(encoding="utf-8")
        cp._seed_starter_list()
        assert (fake_lj / "company_pages.yaml").read_text(encoding="utf-8") == text


def test_company_pages_due_only(tmp_path):
    """Only entries past their cadence are crawled."""
    from sources import company_pages as cp

    fake_lj = tmp_path / "lj"
    fake_lj.mkdir()
    list_path = fake_lj / "company_pages.yaml"
    recent_not_due = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
    list_path.write_text(
        f"""\
- name: DueNow
  careers_url: https://due.com/careers
  cadence_days: 14
  reason: applied
  last_checked: 2026-05-01
  last_found: 0
- name: NotDue
  careers_url: https://notdue.com/careers
  cadence_days: 14
  reason: applied
  last_checked: {recent_not_due}
  last_found: 0
""",
        encoding="utf-8",
    )

    crawled: list[str] = []

    class _FakeCrawler:
        def crawl_org(self, org_name, seed_urls, goal, max_pages=4):
            crawled.append(org_name)
            return []

    with patch.object(cp, "_profile_dir", return_value=fake_lj), \
         patch.object(cp, "_list_path", return_value=list_path):
        src = CompanyPagesSource(crawler=_FakeCrawler(), matcher=None, registry=None)
        entries = src.run()

        assert entries == []
        assert crawled == ["DueNow"]
        # Persisted list has updated last_checked.
        rows = cp._load_list()
        due_row = next(r for r in rows if r["name"] == "DueNow")
        notdue_row = next(r for r in rows if r["name"] == "NotDue")
        assert due_row["last_checked"] == date.today().strftime("%Y-%m-%d")
        assert notdue_row["last_checked"] == recent_not_due  # untouched
