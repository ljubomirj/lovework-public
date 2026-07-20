"""
Tests for the Gmail LJ-jobs source.

Pure tests cover the ported classify/parse/noise-filter logic (no network).
The integration tests monkeypatch the shared run_gapi (as bound in the source module)
so the full ingest→match→mark-read flow runs entirely offline.
"""

from types import SimpleNamespace

import pytest

from sources import gmail_lj_jobs as G


# ── Fixtures: a synthetic LinkedIn alert email body ────────────────────────

SAMPLE_BODY = """Your job alert for ML Research
----------------------------------------
ML Research Scientist
DeepMind
London, UK
https://www.linkedin.com/comm/jobs/view/123456789
----------------------------------------
Senior Data Analyst
Acme Analytics
New York, NY
https://www.linkedin.com/comm/jobs/view/999999999
----------------------------------------
AI Engineer
Anthropic
Remote
https://www.linkedin.com/comm/jobs/view/789012345
See all jobs on LinkedIn
This email was intended for lj@example.com.
"""

LENSA_BODY = """<html><body>
<h2>Jobs you might like</h2>
<div><strong>Machine Learning Engineer</strong><br>
Northstar AI<br>New York, NY<br>
<a href="https://lensa.com/machine-learning-engineer/job/abc123">View job</a></div>
<div><strong>Applied AI Researcher</strong><br>
London, UK<br>
<a href="https://lensa.com/applied-ai-researcher/job/def456">Learn more</a></div>
<a href="https://lensa.com/unsubscribe/abc">Unsubscribe</a>
</body></html>"""

LENSA_MARKDOWN_BODY = """Jobs posted on 100+ job boards
[ | ![LE](https://email.lensa.com/logo) |
Nurp LLC․
---|---
Senior AI/ML Quant Researcher Hybrid Alpha Discovery
$200K-$400K / yr.
| Doral, FL
---
Full-Time
](https://email.lensa.com/f/a/job-one)
[ | ![LE](https://email.lensa.com/logo) |
Wealth․com
---|---
Remote AI/ML Applied Scientist Intern
$45-$55 / hr.
Intern• Remote
](https://email.lensa.com/f/a/job-two)
"""

JOHNSONJOBS_BODY = """<html><body>
<p>New Job Post</p>
<p>Arc Labs<br>Remote (US)<br>
<a href="https://johnsonjobs.com/jobs/applied-ai-engineer-123?source=alert">Applied AI Engineer</a></p>
<a href="https://johnsonjobs.com/unsubscribe?token=abc">Unsubscribe</a>
</body></html>"""


# ── Parser ─────────────────────────────────────────────────────────────────

def test_parse_linkedin_alerts_extracts_jobs():
    jobs = G.parse_linkedin_alerts(SAMPLE_BODY)
    titles = [j["title"] for j in jobs]
    assert "ML Research Scientist" in titles
    assert "AI Engineer" in titles
    assert "Senior Data Analyst" in titles

    deepmind = next(j for j in jobs if j["company"] == "DeepMind")
    assert deepmind["location"] == "London, UK"
    assert "linkedin.com/comm/jobs/view/123456789" in deepmind["url"]


def test_parse_linkedin_alerts_drops_footer():
    jobs = G.parse_linkedin_alerts(SAMPLE_BODY)
    for j in jobs:
        assert "intended for" not in j["title"]
        assert "see all jobs" not in j["title"].lower()


def test_parse_empty_body():
    # Empty body yields no blocks.
    assert G.parse_linkedin_alerts("") == []
    # The parser is deliberately liberal (it only runs on classified LinkedIn emails);
    # arbitrary text is not its concern. Just confirm it doesn't crash.
    G.parse_linkedin_alerts("no separators here\njust text")


# ── Classification ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("sender, subject, expected", [
    ("jobalerts-noreply@linkedin.com", "10 new jobs for you", "linkedin_alert"),
    ("jobs-noreply@linkedin.com", "Your application was sent to Acme", "linkedin_app_sent"),
    ("jobs-listings@linkedin.com", "Job you saved", "linkedin_listing"),
    ("alerts@totaljobs.com", "10 new jobs", "totaljobs_alert"),
    ("alerts@cwjobs.co.uk", "10 new jobs", "cwjobs_alert"),
    ("alerts@talentsourcejobs.co.uk", "10 new jobs", "talentsource_alert"),
    ("jobs@recruitment-london.com", "Rec-London Job Alert", "rec_london_alert"),
    ("JobServe Jobs by Email <jobsbyemail@apps.jobserve.com>", "Agentic engineer", "jobserve_alert"),
    ("Candidate Services <candidate.admin@apps.jobserve.com>", "Important information about your job alert", "jobserve_admin"),
    ("Lensa Aggregated <aggregated@lensa.com>", "Jobs you might like", "lensa_alert"),
    ("Lensa <jobalert@lensa.com>", "New jobs for you", "lensa_alert"),
    ("JJ Alerts <alerts@johnsonjobs.com>", "New Job Post", "johnsonjobs_alert"),
    ("alerts@tech.jobserve.com", "Your job alert", "jobserve_alert"),
    ("feedback@ashbyhq.com", "Application update", "ats_ashby"),
    ("notifications@lever.co", "Apply now", "ats_lever"),
    ("someone@linkedin.com", "Invite", "linkedin_other"),
    ("friend@gmail.com", "Hello", "other"),
])
def test_classify_email(sender, subject, expected):
    assert G.classify_email({"from": sender, "subject": subject}) == expected


def test_parse_totaljobs_alerts_extracts_job_and_seed():
    body = """Senior ML Engineer
Acme AI
London
https://www.totaljobs.com/job/senior-ml-engineer/acme-ai-job123
https://www.totaljobs.com/jobs/machine-learning/in-london
"""
    jobs = G.parse_totaljobs_alerts(body)
    assert jobs == [{
        "title": "Senior ML Engineer",
        "company": "Acme AI",
        "location": "London",
        "url": "https://www.totaljobs.com/job/senior-ml-engineer/acme-ai-job123",
    }]
    assert G.extract_totaljobs_search_url(body) == "https://www.totaljobs.com/jobs/machine-learning/in-london"


def test_parse_cwjobs_alerts_extracts_job_and_seed():
    body = """ML Platform Engineer
Cyberdyne
London
https://www.cwjobs.co.uk/job/ml-platform-engineer/cyberdyne-job456
https://www.cwjobs.co.uk/jobs/ml/in-london
"""
    jobs = G.parse_cwjobs_alerts(body)
    assert jobs == [{
        "title": "ML Platform Engineer",
        "company": "Cyberdyne",
        "location": "London",
        "url": "https://www.cwjobs.co.uk/job/ml-platform-engineer/cyberdyne-job456",
    }]
    assert G.extract_cwjobs_search_url(body) == "https://www.cwjobs.co.uk/jobs/ml/in-london"


def test_parse_talentsource_alerts_handles_jobs_host_and_co_uk_seed():
    body = """AI Engineer
TalentCo
London
https://www.talentsourcejobs.co.uk/job/ai-engineer/123
https://www.talentsourcejobs.co.uk/jobs/ai
"""
    jobs = G.parse_talentsource_alerts(body)
    assert jobs == [{
        "title": "AI Engineer",
        "company": "TalentCo",
        "location": "London",
        "url": "https://www.talentsourcejobs.co.uk/job/ai-engineer/123",
    }]
    assert G.extract_talentsource_search_url(body) == "https://www.talentsourcejobs.co.uk/jobs/ai"


def test_parse_rec_london_alerts_ignores_unsubscribe_and_search_links():
    body = """Unsubscribe
https://www.recruitment-london.com/unsubscribe/abc
AI Engineer
RecruiterCo
London
https://www.recruitment-london.com/jobs/ai-engineer-123
https://www.recruitment-london.com/search?q=ai
"""
    jobs = G.parse_rec_london_alerts(body)
    assert jobs == [{
        "title": "AI Engineer",
        "company": "RecruiterCo",
        "location": "London",
        "url": "https://www.recruitment-london.com/jobs/ai-engineer-123",
    }]


def test_parse_jobserve_alerts_handles_plaintext_and_ignores_search_link():
    body = """New jobs for Agentic engineer
Agentic AI Engineer
Example Labs
London, UK
https://tech.jobserve.com/job-in-London-UK/AGENTIC-AI-ENGINEER-a1b2c3d4/
https://www.jobserve.com/gb/en/JobSearch.aspx?shid=example
"""
    assert G.parse_jobserve_alerts(body) == [{
        "title": "Agentic AI Engineer",
        "company": "Example Labs",
        "location": "London, UK",
        "url": "https://tech.jobserve.com/job-in-London-UK/AGENTIC-AI-ENGINEER-a1b2c3d4/",
    }]


def test_parse_jobserve_alerts_uses_html_anchor_title():
    body = """<html><body>
<p>Example Robotics<br>Cambridge, UK<br>
<a href="https://www.jobserve.com/trx-adv/ABC123?src=email">Senior Agent Engineer</a></p>
<a href="https://www.jobserve.com/unsubscribe/abc">Unsubscribe</a>
</body></html>"""
    assert G.parse_jobserve_alerts(body) == [{
        "title": "Senior Agent Engineer",
        "company": "Example Robotics",
        "location": "Cambridge, UK",
        "url": "https://www.jobserve.com/trx-adv/ABC123?src=email",
    }]


def test_parse_lensa_alerts_extracts_cards_and_ignores_unsubscribe():
    jobs = G.parse_lensa_alerts(LENSA_BODY)
    assert jobs == [
        {
            "title": "Machine Learning Engineer",
            "company": "Northstar AI",
            "location": "New York, NY",
            "url": "https://lensa.com/machine-learning-engineer/job/abc123",
        },
        {
            "title": "Applied AI Researcher",
            "company": "Applied AI Researcher",
            "location": "London, UK",
            "url": "https://lensa.com/applied-ai-researcher/job/def456",
        },
    ]


def test_parse_lensa_alerts_handles_real_markdown_tracking_cards():
    jobs = G.parse_lensa_alerts(LENSA_MARKDOWN_BODY)
    assert jobs == [
        {
            "title": "Senior AI/ML Quant Researcher Hybrid Alpha Discovery",
            "company": "Nurp LLC",
            "location": "Doral, FL",
            "url": "https://email.lensa.com/f/a/job-one",
        },
        {
            "title": "Remote AI/ML Applied Scientist Intern",
            "company": "Wealth.com",
            "location": "Remote",
            "url": "https://email.lensa.com/f/a/job-two",
        },
    ]


def test_parse_johnsonjobs_alerts_extracts_listing_and_ignores_unsubscribe():
    assert G.parse_johnsonjobs_alerts(JOHNSONJOBS_BODY) == [{
        "title": "Applied AI Engineer",
        "company": "Arc Labs",
        "location": "Remote (US)",
        "url": "https://johnsonjobs.com/jobs/applied-ai-engineer-123?source=alert",
    }]


# ── Noise pre-filter ───────────────────────────────────────────────────────

@pytest.mark.parametrize("title, expected_nonfit", [
    ("Quantitative Trader", True),
    ("Portfolio Manager", True),
    ("Data Analyst", True),
    ("Software Engineer", True),               # pure SWE → noise
    ("Software Engineer, ML Platform", False),  # has ml → keep
    ("ML Research Scientist", False),
    ("AI Engineer", False),
])
def test_is_obvious_nonfit(title, expected_nonfit):
    assert G.is_obvious_nonfit(title) is expected_nonfit


def test_is_boilerplate():
    assert G.is_boilerplate("See all jobs on LinkedIn")
    assert G.is_boilerplate("ab")  # too short
    assert G.is_boilerplate("https://linkedin.com/jobs")
    assert not G.is_boilerplate("ML Research Scientist")


# ── Source: graceful no-op when Gmail is unavailable ───────────────────────

def test_source_noop_when_gapi_unavailable(monkeypatch):
    """If the Gmail accessor returns None (no google_api.py / no auth), source no-ops."""
    monkeypatch.setattr(G, "run_gapi", lambda *a, **kw: None)
    src = G.GmailLjJobsSource(matcher=None, registry=None)
    assert src.run() == []


# ── Source: offline end-to-end ingest → match → mark-read ──────────────────

class _FakeMatcher:
    def match(self, title, desc, org_name, job_url="", location=""):
        return SimpleNamespace(score=8.0, decision="GO", reasoning=f"fit {title}")


class _FakeRegistry:
    def __init__(self):
        self.upserts = []

    def upsert(self, org, title, url="", careers_url="", source=""):
        self.upserts.append((org, title))
        return SimpleNamespace(status="new", first_seen="2026-06-23")


def test_source_offline_pipeline(monkeypatch):
    """Full flow with Gmail monkeypatched: search → parse → registry → match → mark-read."""
    modify_calls = []

    def fake_gapi(*args):
        if args[:2] == ("gmail", "search"):
            return [{"id": "m1", "from": "jobalerts-noreply@linkedin.com",
                     "subject": "Your job alert", "date": "2026-06-23"}]
        if args[:2] == ("gmail", "get"):
            return {"body": SAMPLE_BODY}
        if args[:2] == ("gmail", "modify"):
            modify_calls.append(args)
            return {}
        return None

    monkeypatch.setattr(G, "run_gapi", fake_gapi)

    registry = _FakeRegistry()
    src = G.GmailLjJobsSource(matcher=_FakeMatcher(), registry=registry, mark_read=True)
    entries = src.run()

    # 3 listings in SAMPLE_BODY, but "Senior Data Analyst" is filtered as obvious nonfit.
    titles = sorted(e.title for e in entries)
    assert titles == ["AI Engineer", "ML Research Scientist"]
    assert all(e.decision == "GO" and e.score == 8.0 for e in entries)
    assert all(e.source == "gmail_lj_jobs" for e in entries)
    assert all(e.lifecycle_status == "new" for e in entries)

    # Registry saw the two kept jobs (nonfit filtered before upsert).
    assert ("DeepMind", "ML Research Scientist") in registry.upserts
    assert ("Anthropic", "AI Engineer") in registry.upserts
    assert all(t != "Senior Data Analyst" for _, t in registry.upserts)

    # Email marked read (cron idempotency).
    assert len(modify_calls) == 1
    assert "UNREAD" in modify_calls[0]


def test_source_mark_read_disabled(monkeypatch):
    """mark_read=False leaves mail untouched."""
    calls = []

    def fake_gapi(*args):
        calls.append(args)
        if args[:2] == ("gmail", "search"):
            return [{"id": "m1", "from": "x@gmail.com", "subject": "hi"}]
        return None

    monkeypatch.setattr(G, "run_gapi", fake_gapi)
    src = G.GmailLjJobsSource(matcher=_FakeMatcher(), registry=_FakeRegistry(), mark_read=False)
    src.run()
    assert not any(c[:2] == ("gmail", "modify") for c in calls)


def test_source_lensa_alert_dispatches_and_marks_read(monkeypatch):
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
            return {"body": LENSA_BODY}
        if args[:2] == ("gmail", "modify"):
            return {}
        return None

    monkeypatch.setattr(G, "run_gapi", fake_gapi)
    src = G.GmailLjJobsSource(
        matcher=_FakeMatcher(), registry=_FakeRegistry(), mark_read=True,
    )
    entries = src.run()
    assert [entry.title for entry in entries] == [
        "Machine Learning Engineer", "Applied AI Researcher",
    ]
    assert all(entry.url.startswith("https://lensa.com/") for entry in entries)
    assert any(call[:2] == ("gmail", "modify") for call in calls)


def test_source_johnsonjobs_alert_dispatches_and_marks_read(monkeypatch):
    calls = []

    def fake_gapi(*args):
        calls.append(args)
        if args[:2] == ("gmail", "search"):
            return [{
                "id": "johnsonjobs-1",
                "from": "JJ Alerts <alerts@johnsonjobs.com>",
                "subject": "New Job Post",
            }]
        if args[:2] == ("gmail", "get"):
            return {"body": JOHNSONJOBS_BODY}
        if args[:2] == ("gmail", "modify"):
            return {}
        return None

    monkeypatch.setattr(G, "run_gapi", fake_gapi)
    entries = G.GmailLjJobsSource(
        matcher=_FakeMatcher(), registry=_FakeRegistry(), mark_read=True,
    ).run()
    assert [entry.title for entry in entries] == ["Applied AI Engineer"]
    assert entries[0].url.startswith("https://johnsonjobs.com/jobs/")
    assert any(call[:2] == ("gmail", "modify") for call in calls)


def test_recognised_lead_with_zero_parsed_jobs_stays_unread(monkeypatch):
    """A provider template change must not silently consume its lead email."""
    calls = []

    def fake_gapi(*args):
        calls.append(args)
        if args[:2] == ("gmail", "search"):
            return [{
                "id": "jobserve-1",
                "from": "JobServe Jobs by Email <jobsbyemail@apps.jobserve.com>",
                "subject": "Agentic engineer",
            }]
        if args[:2] == ("gmail", "get"):
            return {"body": "A changed template with no recognised listing links"}
        return {}

    monkeypatch.setattr(G, "run_gapi", fake_gapi)
    src = G.GmailLjJobsSource(matcher=_FakeMatcher(), registry=_FakeRegistry(), mark_read=True)

    assert src.run() == []
    assert not any(c[:2] == ("gmail", "modify") for c in calls)
