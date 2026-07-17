import json
from pathlib import Path

from snapshot import append_assessments, append_passive_outcomes, append_run
from wiki_store import WikiEntry


def test_append_run_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr("snapshot._git_commit", lambda: "abc123")

    path = append_run(
        tmp_path,
        run_id="run-1",
        profile_name="lj",
        role="general",
        sources=["neolabs"],
        profile_text="profile text",
        model="deepseek-v4",
        provider="openai",
    )

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["event_type"] == "run"
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["sources"] == ["neolabs"]
    assert rows[0]["git_commit"] == "abc123"
    assert len(rows[0]["profile_hash"]) == 64


def test_append_assessments_writes_jsonl(tmp_path):
    entry = WikiEntry(
        org_name="Poetiq",
        title="AI Scientist",
        url="https://example.com/poetiq",
        location="London",
        score=7.6,
        decision="GO",
        reasoning="Applied systems role",
        source="test",
        fit_score=8.0,
        reach_score=7.0,
        flourish_score=8.0,
        combined_score=7.6,
        recommended_action="APPLY_NOW",
        lifecycle_status="new",
        first_seen="2026-07-06",
        discovery_url="https://news.ycombinator.com/item?id=48749307",
        discovery_date="2026-07-01",
    )

    path = append_assessments(
        tmp_path,
        [entry],
        run_id="run-1",
        profile_name="lj",
        role="general",
        sources=["neolabs"],
    )

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["event_type"] == "assessment"
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["advert_hash"]
    assert rows[0]["org_name"] == "Poetiq"
    assert rows[0]["fit_score"] == 8.0
    assert rows[0]["recommended_action"] == "APPLY_NOW"
    assert rows[0]["sources_run"] == ["neolabs"]
    assert rows[0]["discovery_url"].endswith("item?id=48749307")
    assert rows[0]["discovery_date"] == "2026-07-01"


def test_append_passive_outcomes_uses_history_scanner(tmp_path, monkeypatch):
    from history import GmailEvent, PriorApplication, PriorContact

    entry = WikiEntry(
        org_name="Poetiq",
        title="AI Scientist",
        url="https://example.com/poetiq",
        location="London",
        score=7.6,
        decision="GO",
        reasoning="fit",
        source="test",
    )

    def fake_scan_history(org, use_gmail=True):
        assert org == "Poetiq"
        assert use_gmail is True
        return PriorContact(
            org=org,
            applications=[
                PriorApplication(
                    date="2026-07-01",
                    company="Poetiq",
                    role="AI Scientist",
                    path=Path("/applications/20260701-Poetiq-AI_Scientist"),
                    has_rejection=True,
                    rejection_date="2026-07-09",
                )
            ],
            gmail_events=[
                GmailEvent(
                    date="2026-07-04",
                    subject="Interview with Poetiq",
                    from_="jobs@example.com",
                    snippet="next steps",
                    kind="interview",
                ),
                GmailEvent(
                    date="2026-07-05",
                    subject="Newsletter",
                    from_="news@example.com",
                    snippet="other",
                    kind="other",
                ),
            ],
        )

    monkeypatch.setattr("history.scan_history", fake_scan_history)

    path = append_passive_outcomes(tmp_path, [entry], run_id="run-1", use_gmail=True)
    rows = [json.loads(line) for line in path.read_text().splitlines()]

    assert [r["kind"] for r in rows] == ["applied", "rejection", "interview"]
    assert all(r["event_type"] == "outcome" for r in rows)
    assert all(r["run_id"] == "run-1" for r in rows)
    assert rows[0]["advert_hash"]
