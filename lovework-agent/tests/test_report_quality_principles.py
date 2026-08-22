"""Regression tests for the six report-quality principles (P1–P6).

Each test reproduces one concrete defect from the 2026-07-21 / 2026-08-12
report review and asserts the principle now prevents it. See LEARNINGS.md
"Report quality: six general principles for surfacing leads (2026-08-13)"
for the analysis; docs/20-report-qa-spec.md for the report contract.

P1 — Provenance is mandatory, absence is visible.
P2 — Identity integrity: title is the role, location is a place.
P3 — Market gates fire at the boundary, pre-LLM.
P4 — Known principal intent overrides fresh scoring.
P5 — Liveness is a lifecycle fact.
P6 — Discovery must resolve to the actual advert.
"""

from datetime import date

import pytest

from lead_identity import (
    is_implausible_header,
    is_noise_location,
    is_noise_title,
)


# ── P1: provenance ─────────────────────────────────────────────────────────

def test_p1_linkedin_parser_captures_plain_jobs_view_url():
    """Defect 1/2/3 class: LinkedIn alerts with plain /jobs/view/ URLs must
    keep the URL (previously only /comm/jobs/view/ was captured)."""
    from sources.gmail_lj_jobs import parse_linkedin_alerts

    body = """Your job alert for ML Research
----------------------------------------
ML Research Scientist
DeepMind
London, UK
https://www.linkedin.com/jobs/view/123456789
----------------------------------------"""
    jobs = parse_linkedin_alerts(body)
    assert jobs, "parser must find the listing"
    assert jobs[0]["url"] == "https://www.linkedin.com/jobs/view/123456789"


def test_p1_incremental_report_shows_not_available_when_no_url():
    """Defect 1/2/3 class: the incremental report must render the URL,
    the Found-via line, or an explicit _not available_ — never silence."""
    from incremental_crawl import _render_entry_block
    from wiki_store import WikiEntry

    entry = WikiEntry(
        org_name="NoURL Co", title="Engineer", url=None, discovery_url=None,
        location="London", score=6.0, decision="MAYBE",
        reasoning="x", lifecycle_status="new", source="gmail",
    )
    text = "\n".join(_render_entry_block(entry))
    assert "- **URL**: _not available_" in text


# ── P2: identity integrity ─────────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "Results from the new AI-powered job search — 28 new jobs match your preferences.",  # defect 4
    "£120000 - £150000 per annum + equity + benefits — Permanent",  # defect 6
    "1 day ago",  # relative date as title
    "https://example.com/jobs/1",  # bare URL as title
    "New recommended jobs",  # digest line
    "x",  # too short
])
def test_p2_noise_titles_are_rejected(title):
    assert is_noise_title(title)


@pytest.mark.parametrize("location", [
    "1 day ago",  # defect 6: relative date as location
    "2 hours ago",
    "Expired",
])
def test_p2_noise_locations_are_rejected(location):
    assert is_noise_location(location)


def test_p2_em_dash_prose_header_is_implausible():
    """Defect 15: an em-dash inside a description paragraph must not split
    into a false company/role header."""
    company = ("We use web data to identify things like org structure, tech stack, "
               "and key projects (e.g., GenAI initiatives, cloud migrations).")
    role = "and now we're ready to accelerate"
    assert is_implausible_header(company, role)


def test_p2_real_header_is_not_implausible():
    assert not is_implausible_header("DeepMind", "Research Scientist")
    assert not is_implausible_header("Electric Twin", "Research Engineer (70k-120k) at Electric Twin")


# ── P3: market gates at the boundary ───────────────────────────────────────

def test_p3_us_visitors_only_kills():
    """Defect 12: a landing page saying 'This site is for US visitors only'
    is a deterministic work-auth kill."""
    from matcher import _check_work_auth_kill

    reason = _check_work_auth_kill("Remote", "This site is for US visitors only.")
    assert reason is not None
    assert "work-authorization" in reason


def test_p3_lensa_gated_off_by_default(monkeypatch):
    """Defect 12 class: Lensa (US-market noise) produces no leads unless
    explicitly enabled."""
    from sources import gmail_lj_jobs as G

    calls = []

    def fake_gapi(*args):
        calls.append(args)
        if args[:2] == ("gmail", "search"):
            return [{
                "id": "lensa-1",
                "from": "Lensa Aggregated <aggregated@lensa.com>",
                "subject": "Jobs you might like",
            }]
        if args[:2] == ("gmail", "get"):
            return {"body": "Machine Learning Engineer\nUS\nhttps://lensa.com/jobs/1"}
        if args[:2] == ("gmail", "modify"):
            return {}
        return None

    monkeypatch.setattr(G, "run_gapi", fake_gapi)
    monkeypatch.delenv("LOVEWORK_GMAIL_LENSA_ENABLED", raising=False)
    src = G.GmailLjJobsSource(matcher=_FakeMatcher(), registry=_FakeRegistry(), mark_read=True)
    assert src.run() == []


# ── P4: known intent overrides fresh scoring ───────────────────────────────

def test_p4_active_application_suppresses_fresh_opportunity(isolated_config):
    """Defect 14: Talk Machine applied and in progress must not re-surface
    as a fresh GO. The reapply kill fires on any same-role application
    within the cooldown, not only rejections."""
    from datetime import datetime, timedelta

    from matcher import _check_reapply_kill

    apps = isolated_config["applications"]
    recent = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    d = apps / f"{recent.replace('-', '')}-Talk_Machine-Engineer"
    d.mkdir()
    (d / f"{recent.replace('-', '')}-Talk_Machine-Engineer.txt").write_text(
        "Applied. In progress."
    )

    result = _check_reapply_kill("Talk Machine", "Founding Engineers", applications_dir=apps)
    assert result is not None
    assert "in progress" in result.lower()


# ── P5: liveness is a lifecycle fact ───────────────────────────────────────

def test_p5_expired_advert_is_dropped(monkeypatch, tmp_path):
    """Defect 16: an advert whose primary page says 'This position is no
    longer active' is dropped before scoring, never surfaced as GO/MAYBE."""
    from enrichment import EnrichingMatcher, LeadEnricher

    seen = {}

    class FakeInner:
        def match(self, *a, **kw):
            seen["called"] = True
            return None  # must never be reached

    class FakeFetcher(LeadEnricher):
        def __init__(self, cache_dir):
            super().__init__(cache_dir=cache_dir)

        def _fetch(self, url):
            return (
                "This position is no longer active. Either the position was "
                "filled, or the ad has expired.",
                "http",
            )

    enricher = FakeFetcher(tmp_path)
    matcher = EnrichingMatcher(FakeInner(), enricher)
    result = matcher.match(
        "Research Engineer", "desc", "Arsenal FC",
        job_url="https://careers.arsenal.com/jobs/8083296", location="London Colney, UK",
    )
    assert seen.get("called") is None, "expired advert must not reach the inner matcher"
    assert result.decision == "DROP"
    assert "expired" in result.reasoning.lower()


def test_p5_expiry_marker_detected():
    from enrichment import _expiry_marker

    assert _expiry_marker("This position is no longer active")
    assert _expiry_marker("The position has been filled. Thanks for your interest.")
    assert not _expiry_marker("We are hiring for Q4; roles open now.")


# ── P6: discovery resolves to the actual advert ────────────────────────────

def test_p6_social_post_link_is_followed(monkeypatch, tmp_path):
    """Defect 16: an X post whose real link sits in the post text must be
    followed to the actual advert page before scoring."""
    from enrichment import EnrichingMatcher, LeadEnricher

    post_body = (
        "Karun Singh on X: \"We're hiring a Research Engineer at Arsenal. "
        "This is a long post body so the extracted text clears the useful "
        "length threshold and triggers social-link resolution. We are "
        "building state of the art AI models for the football domain, "
        "working directly with the Men's First Team, focused on the "
        "application layer for research to advance coaching and analysis "
        "workflows. https://t.co/F2WXIhkJJe\" / X Post"
    )
    careers_body = (
        "Research Engineer — Arsenal FC. This position is no longer active. "
        "Either the position was filled, or the ad has expired."
    )

    class _FakeResponse:
        text = ""
        url = ""

        def raise_for_status(self):
            return None

    def fake_get(url, *a, **kw):
        r = _FakeResponse()
        r.url = url
        if "x.com" in url:
            r.text = f"<html><body>{post_body}</body></html>"
        else:
            # t.co short link resolves (follow_redirects) to the real advert
            # page — which says the position is no longer active.
            r.text = f"<html><body>{careers_body}</body></html>"
        return r

    monkeypatch.setattr("httpx.get", fake_get)
    enricher = LeadEnricher(cache_dir=tmp_path)

    seen = {}

    class FakeInner:
        def match(self, *a, **kw):
            seen["called"] = True
            return None  # must never be reached for an expired advert

    matcher = EnrichingMatcher(FakeInner(), enricher)
    result = matcher.match(
        "Research Engineer", "desc", "Arsenal FC",
        job_url="https://x.com/karun1710/status/2080382035613405584", location="London Colney, UK",
    )
    assert seen.get("called") is None, "expired advert must not reach the inner matcher"
    assert result.decision == "DROP"
    assert "expired" in result.reasoning.lower()


def _drop_result():
    from matcher import MatchResult

    return MatchResult(
        fit_score=0.0, reach_score=0.0, flourish_score=0.0,
        combined_score=0.0, score=0.0, decision="DROP", recommended_action="DROP",
        reasoning="",
    )


# ── shared fakes (mirror test_gmail_lj_jobs.py) ────────────────────────────

class _FakeMatcher:
    def match(self, title, description, org_name, job_url="", location=""):
        from matcher import MatchResult

        return MatchResult(
            fit_score=5.0, reach_score=5.0, flourish_score=5.0,
            combined_score=5.0, score=5.0, decision="MAYBE",
            recommended_action="MONITOR", reasoning="fake",
        )


class _FakeRegistry:
    def upsert(self, **kwargs):
        return None
