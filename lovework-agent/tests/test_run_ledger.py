"""Tests for durable crawl-run evidence and deterministic reconciliation."""

from datetime import datetime, timedelta, timezone

import run_ledger
import run_watchdog


LONDON_SUMMER = timezone(timedelta(hours=1))
EXPECTED = datetime(2026, 7, 19, 9, 0, tzinfo=LONDON_SUMMER)
CHECKED = datetime(2026, 7, 19, 15, 0, tzinfo=LONDON_SUMMER)


def resolved_record() -> dict:
    return {
        "run_id": "full-20260719-090000-1",
        "run_type": "full",
        "status": "succeeded",
        "started_at": "2026-07-19T08:00:00+00:00",
        "finished_at": "2026-07-19T11:42:00+00:00",
        "report_file": "wiki/reports/2026-07-19-report.md",
        "notification": {
            "status": "sent",
            "provider": "gmail_api",
            "message_id": "gmail-message-123",
        },
    }


def test_run_ledger_records_terminal_outcome_and_notification(tmp_path):
    run_ledger.start_run(
        "full-20260719-090000-1",
        "full",
        profile="hermel",
        hermes_home="/home/ljubomir/.hermes-gigul2/profiles/hermel",
        log_file="/tmp/full.log",
        runs_dir=tmp_path,
    )
    run_ledger.finish_run(
        "full-20260719-090000-1",
        status="succeeded",
        exit_code=0,
        report_file="wiki/reports/2026-07-19-report.md",
        runs_dir=tmp_path,
    )
    record = run_ledger.record_notification(
        "full-20260719-090000-1",
        status="sent",
        provider="gmail_api",
        message_id="gmail-message-123",
        runs_dir=tmp_path,
    )

    assert record["status"] == "succeeded"
    assert record["notification"]["message_id"] == "gmail-message-123"
    assert run_ledger.list_runs("full", tmp_path)[0]["run_id"] == record["run_id"]


def test_watchdog_accepts_complete_run_with_gmail_proof():
    result = run_watchdog.reconcile(
        run_type="full",
        weekday=6,
        schedule_time="09:00",
        grace=timedelta(minutes=15),
        max_runtime=timedelta(minutes=330),
        now=CHECKED,
        records=[resolved_record()],
    )

    assert result["ok"] is True
    assert result["state"] == "resolved"


def test_watchdog_writes_incident_when_notification_is_missing(tmp_path):
    record = resolved_record()
    record["notification"] = {"status": "failed", "provider": "gmail_api", "message_id": None}
    result = run_watchdog.reconcile(
        run_type="full",
        weekday=6,
        schedule_time="09:00",
        grace=timedelta(minutes=15),
        max_runtime=timedelta(minutes=330),
        now=CHECKED,
        records=[record],
    )

    json_path, markdown_path = run_watchdog.write_incident(result, tmp_path)

    assert result["ok"] is False
    assert result["state"] == "notification_unresolved"
    assert json_path.exists()
    assert "Required investigation" in markdown_path.read_text(encoding="utf-8")


def test_watchdog_investigation_claim_survives_repeated_observation(tmp_path):
    record = resolved_record()
    record["notification"] = {"status": "failed", "provider": "gmail_api", "message_id": None}
    result = run_watchdog.reconcile(
        run_type="full", weekday=6, schedule_time="09:00",
        grace=timedelta(minutes=15), max_runtime=timedelta(minutes=330),
        now=CHECKED, records=[record],
    )
    json_path, _ = run_watchdog.write_incident(result, tmp_path)

    assert run_watchdog.claim_investigation(json_path) is True
    # The normal watchdog can observe the same incident again without waking
    # a second autonomous investigator.
    run_watchdog.write_incident(result, tmp_path)
    assert run_watchdog.claim_investigation(json_path) is False


def test_watchdog_detects_missing_scheduled_start():
    result = run_watchdog.reconcile(
        run_type="full",
        weekday=6,
        schedule_time="09:00",
        grace=timedelta(minutes=15),
        max_runtime=timedelta(minutes=330),
        now=CHECKED,
        records=[],
    )

    assert result["ok"] is False
    assert result["state"] == "missing_start"
