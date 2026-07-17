"""
Tests for the job registry (CSV lifecycle tracking).

The registry is the persistent memory of every job we've ever seen. These
tests verify the upsert, lookup, lifecycle transition, and stats logic.
"""

import pytest

from job_registry import (
    JobRegistry,
    STATUS_NEW,
    STATUS_STILL_OPEN,
    STATUS_DISAPPEARED,
    STATUS_LONG_LASTING,
    LONG_LASTING_THRESHOLD_DAYS,
)


def test_new_job_is_new(isolated_config):
    """First-time insertion marks the job as 'new'."""
    r = JobRegistry(csv_path=isolated_config["cache"] / "jobs.csv")
    rec = r.upsert("Acme", "ML Engineer", "https://acme.com/jobs/1")
    assert rec.status == STATUS_NEW
    assert rec.first_seen == rec.last_seen
    assert rec.age_days == 0


def test_second_seen_is_still_open(isolated_config):
    """Re-inserting an existing job marks it 'still_open'."""
    r = JobRegistry(csv_path=isolated_config["cache"] / "jobs.csv")
    r.upsert("Acme", "ML Engineer", "https://acme.com/jobs/1")
    rec = r.upsert("Acme", "ML Engineer", "https://acme.com/jobs/1")
    assert rec.status == STATUS_STILL_OPEN
    assert rec.last_seen >= rec.first_seen


def test_existing_job_is_enriched_with_discovery_provenance(isolated_config):
    r = JobRegistry(csv_path=isolated_config["cache"] / "jobs.csv")
    r.upsert("Talk Machine", "Founding Engineers", "https://talkmachine.com/jobs/engineer")

    rec = r.upsert(
        "Talk Machine",
        "Founding Engineers",
        "https://talkmachine.com/jobs/engineer",
        careers_url="https://news.ycombinator.com/item?id=48749307",
        source="hn_hiring",
        discovery_url="https://news.ycombinator.com/item?id=48749307",
        discovery_date="2026-07-01",
    )

    assert rec.discovery_url.endswith("item?id=48749307")
    assert rec.discovery_date == "2026-07-01"
    assert rec.careers_url == rec.discovery_url
    assert rec.source == "hn_hiring"


def test_lookup_by_org(isolated_config):
    """by_org returns all jobs for a given org."""
    r = JobRegistry(csv_path=isolated_config["cache"] / "jobs.csv")
    r.upsert("Acme", "ML Engineer", "https://acme.com/jobs/1")
    r.upsert("Acme", "AI Scientist", "https://acme.com/jobs/2")
    r.upsert("Other", "Researcher", "https://other.com/jobs/1")

    acme_jobs = r.by_org("Acme")
    assert len(acme_jobs) == 2
    for j in acme_jobs:
        assert j.org == "Acme"


def test_lookup_by_org_is_case_insensitive(isolated_config):
    """by_org matches case-insensitively."""
    r = JobRegistry(csv_path=isolated_config["cache"] / "jobs.csv")
    r.upsert("Acme", "ML Engineer", "https://acme.com/jobs/1")
    assert len(r.by_org("acme")) == 1
    assert len(r.by_org("ACME")) == 1


def test_hash_uniqueness(isolated_config):
    """The same org+title+url produces the same hash (dedup)."""
    r = JobRegistry(csv_path=isolated_config["cache"] / "jobs.csv")
    r.upsert("Acme", "ML Engineer", "https://acme.com/jobs/1")
    r.upsert("Acme", "ML Engineer", "https://acme.com/jobs/1")
    assert r.stats() == {STATUS_STILL_OPEN: 1}


def test_mark_run_complete_marks_unseen_as_disappeared(isolated_config):
    """Jobs not seen in the current run become 'disappeared'."""
    csv_path = isolated_config["cache"] / "jobs.csv"
    r = JobRegistry(csv_path=csv_path)
    # Insert two jobs (simulating a previous run)
    r.upsert("Acme", "ML Engineer", "https://acme.com/jobs/1")
    r.upsert("Acme", "AI Scientist", "https://acme.com/jobs/2")
    # To simulate job 1 not being seen today, directly set its last_seen
    # to an old date in the CSV (since both were just inserted with today's date).
    rows = r._load()
    for row in rows:
        if row["title"] == "ML Engineer":
            row["last_seen"] = "2020-01-01"
    from job_registry import _write_rows
    _write_rows(csv_path, rows)
    # Now only job 2 has last_seen = today. mark_run_complete should see job 1.
    disappeared = r.mark_run_complete()
    assert disappeared == 1
    # The unseen job should now be disappeared
    rec = r.get("Acme", "ML Engineer", "https://acme.com/jobs/1")
    assert rec.status == STATUS_DISAPPEARED


def test_get_status_for_unknown_returns_unknown(isolated_config):
    """A job not in the registry has status 'unknown'."""
    r = JobRegistry(csv_path=isolated_config["cache"] / "jobs.csv")
    assert r.get_status_for("Acme", "ML Engineer") == "unknown"


def test_get_status_for_known_returns_status(isolated_config):
    """A job in the registry has its actual status."""
    r = JobRegistry(csv_path=isolated_config["cache"] / "jobs.csv")
    r.upsert("Acme", "ML Engineer", "https://acme.com/jobs/1")
    assert r.get_status_for("Acme", "ML Engineer", "https://acme.com/jobs/1") == STATUS_NEW


def test_stats_empty(isolated_config):
    """An empty registry has empty stats."""
    r = JobRegistry(csv_path=isolated_config["cache"] / "jobs.csv")
    assert r.stats() == {}


def test_long_lasting_promotion(isolated_config, monkeypatch):
    """Jobs seen over LONG_LASTING_THRESHOLD_DAYS are promoted to long_lasting."""
    from datetime import datetime, timedelta
    from job_registry import _job_hash, _write_rows

    csv_path = isolated_config["cache"] / "jobs.csv"
    r = JobRegistry(csv_path=csv_path)
    # Insert a job with first_seen 60 days ago directly into the CSV
    old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    h = _job_hash("Acme", "ML Engineer", "https://acme.com/jobs/1")
    old_row = {
        "id": "1",
        "org": "Acme",
        "title": "ML Engineer",
        "url": "https://acme.com/jobs/1",
        "careers_url": "",
        "first_seen": old_date,
        "last_seen": old_date,
        "status": "still_open",
        "source": "test",
        "hash": h,
    }
    rows = r._load()
    rows.append(old_row)
    _write_rows(csv_path, rows)
    # Now simulate "seen today" and call mark_run_complete
    r.upsert("Acme", "ML Engineer", "https://acme.com/jobs/1")
    r.mark_run_complete()
    rec = r.get("Acme", "ML Engineer", "https://acme.com/jobs/1")
    assert rec.status == STATUS_LONG_LASTING
    assert rec.age_days > LONG_LASTING_THRESHOLD_DAYS
