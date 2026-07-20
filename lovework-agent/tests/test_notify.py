"""Tests for profile-aware completion email delivery."""

import json

from pathlib import Path
from types import SimpleNamespace

import notify


def test_gmail_notification_passes_active_profile_and_records_receipt(monkeypatch, tmp_path):
    profile = tmp_path / "profiles" / "hermel"
    profile.mkdir(parents=True)
    gapi = tmp_path / "google_api.py"
    gapi.write_text("# test placeholder\n")
    captured = {}

    monkeypatch.setattr(notify, "_HERMES_HOME", profile)
    monkeypatch.setattr(notify, "_find_gapi", lambda: gapi)
    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "sent", "id": "gmail-message-123"}),
            stderr="",
        )

    monkeypatch.setattr(notify.subprocess, "run", fake_run)

    result = notify.send_email_result("subject", "body")
    assert result == {
        "ok": True,
        "provider": "gmail_api",
        "message_id": "gmail-message-123",
    }
    assert captured["env"]["HERMES_HOME"] == str(profile)
    assert captured["command"][1] == str(gapi)
    assert captured["command"][-2:] == ["--body", "body"]


def test_gmail_notification_rejects_missing_delivery_receipt(monkeypatch, tmp_path):
    gapi = tmp_path / "google_api.py"
    gapi.write_text("# test placeholder\n")
    monkeypatch.setattr(notify, "_find_gapi", lambda: gapi)
    monkeypatch.setattr(
        notify.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="{}", stderr=""),
    )

    result = notify.send_email_result("subject", "body")

    assert result["ok"] is False
    assert "sent message id" in result["error"]


def test_parse_report_derives_decisions_from_new_action_format(tmp_path):
    report = tmp_path / "new-format.md"
    report.write_text(
        """# FULL SWEEP — 2026-07-19 12:42:40 BST

### A — Role
- **Score**: 8.0/10
- **Action**: APPLY_NOW

### B — Role
- **Score**: 7.0/10
- **Action**: MONITOR

### C — Role
- **Score**: 6.0/10
- **Action**: WATCH

### D — Role
- **Score**: 1.0/10
- **Action**: DROP
""",
        encoding="utf-8",
    )

    assert notify.parse_report(report)["decisions"] == {
        "GO": 1, "MAYBE": 1, "FLAG": 1, "DROP": 1,
    }


def test_email_uses_authoritative_final_log_summary(tmp_path):
    log = tmp_path / "full.log"
    log.write_text(
        """noise before
============================================================
LoveWork Results — LJ-general
============================================================
GO:     27
MAYBE:  30
FLAG:   44
DROP:   290
Total:  391

New (first time seen):      74
Long-lasting (>30d open):   0
Disappeared this run:       69

★ New GO/MAYBE listings:
  [8.6] Shift — AI Engineering Lead
        https://news.ycombinator.com/item?id=48748972

★ GO listings:
  [8.6] Talk Machine — Founding Engineers
        https://talkmachine.com/jobs/engineer

◆ MAYBE listings:
  [7.4] Poetiq — AI Scientist

Wiki: /example/wiki
afterwards
""",
        encoding="utf-8",
    )
    info = {
        "run_type": "Full Sweep", "date": "2026-07-19", "report_name": "report.md",
        "decisions": {}, "lifecycle": {}, "total": 0, "top_gos": [],
    }

    summary = notify.extract_log_summary(log)
    _, body = notify.format_email(info, summary)

    assert "GO:     27" in body
    assert "Disappeared this run:       69" in body
    assert "https://talkmachine.com/jobs/engineer" in body
    assert "Top picks:" not in body
