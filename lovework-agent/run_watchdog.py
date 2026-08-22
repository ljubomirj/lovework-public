#!/usr/bin/env python3
"""Reconcile an expected scheduled LoveWork run with durable run evidence.

This is the deterministic first layer of LoveWork's meta-maintenance loop.
It does not use an LLM and it never changes crawler code: when the expectation
and observed evidence differ, it writes an incident packet for an agent or
human to investigate.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import run_ledger
from principal_runtime import resolve_principal_runtime
from config import CACHE_DIR

INCIDENTS_DIR = CACHE_DIR / "incidents"


def _parse_time(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except ValueError as exc:
        raise ValueError("schedule time must be HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("schedule time must be HH:MM")
    return hour, minute


def expected_run_at(now: datetime, weekday: int, schedule_time: str) -> datetime:
    """Return the most recent scheduled local time, including today if due."""
    hour, minute = _parse_time(schedule_time)
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_back = (scheduled.weekday() - weekday) % 7
    scheduled -= timedelta(days=days_back)
    if scheduled > now:
        scheduled -= timedelta(days=7)
    return scheduled


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _matching_run(records: list[dict[str, Any]], expected_at: datetime, next_expected_at: datetime) -> dict[str, Any] | None:
    """Find the worker that started in the expected schedule window."""
    for record in records:
        started_at = _parse_iso(record.get("started_at"))
        if started_at is None:
            continue
        local_started = started_at.astimezone(expected_at.tzinfo)
        if expected_at <= local_started < next_expected_at:
            return record
    return None


def reconcile(
    *,
    run_type: str,
    weekday: int,
    schedule_time: str,
    grace: timedelta,
    max_runtime: timedelta,
    now: datetime | None = None,
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare a scheduled-run expectation with observed worker evidence."""
    current = now or datetime.now().astimezone()
    expected_at = expected_run_at(current, weekday, schedule_time)
    if current < expected_at + grace:
        return {"ok": True, "state": "not_due", "expected_at": expected_at.isoformat()}

    next_expected_at = expected_at + timedelta(days=7)
    matching_run = _matching_run(
        records if records is not None else run_ledger.list_runs(run_type),
        expected_at,
        next_expected_at,
    )
    base = {
        "ok": False,
        "run_type": run_type,
        "expected_at": expected_at.isoformat(),
        "checked_at": current.isoformat(),
        "run": matching_run,
    }
    if matching_run is None:
        return base | {
            "state": "missing_start",
            "summary": f"Expected {run_type} run did not start after {expected_at.isoformat()}.",
        }
    if matching_run.get("status") == "running":
        started_at = _parse_iso(matching_run.get("started_at"))
        if started_at and current - started_at > max_runtime:
            return base | {
                "state": "overdue",
                "summary": f"{run_type} run exceeded the {max_runtime} runtime limit.",
            }
        return {"ok": True, "state": "running", "expected_at": expected_at.isoformat(), "run": matching_run}
    if matching_run.get("status") == "failed":
        return base | {
            "state": "crawl_failed",
            "summary": f"{run_type} crawl failed: {matching_run.get('error') or 'no error recorded'}",
        }
    if matching_run.get("status") != "succeeded" or not matching_run.get("report_file"):
        return base | {
            "state": "incomplete_terminal_record",
            "summary": f"{run_type} run lacks a complete successful terminal record.",
        }
    notification = matching_run.get("notification") or {}
    if notification.get("status") != "sent" or not notification.get("message_id"):
        return base | {
            "state": "notification_unresolved",
            "summary": f"{run_type} crawl succeeded but its Gmail notification is unresolved.",
        }
    return {
        "ok": True,
        "state": "resolved",
        "expected_at": expected_at.isoformat(),
        "run": matching_run,
    }


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _load_json(path: Path) -> dict[str, Any]:
    """Return an existing incident record, or an empty record when unavailable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_incident(result: dict[str, Any], incidents_dir: Path = INCIDENTS_DIR) -> tuple[Path, Path]:
    """Write machine- and human-readable evidence for a detected variance."""
    expected = result["expected_at"].replace(":", "-")
    stem = f"{result['run_type']}-{expected}-{result['state']}"
    json_path = incidents_dir / f"{stem}.json"
    markdown_path = incidents_dir / f"{stem}.md"
    # A repeated watchdog check must not erase the investigator hand-off.
    existing = _load_json(json_path)
    preserved = {
        key: existing[key]
        for key in ("investigation_claimed_at", "investigation_closed_at", "investigation_note")
        if key in existing
    }
    _atomic_write(json_path, result | preserved)
    run = result.get("run") or {}
    lines = [
        f"# LoveWork operational variance — {result['state']}",
        "",
        f"- Expected run: `{result['run_type']}` at `{result['expected_at']}`",
        f"- Checked: `{result['checked_at']}`",
        f"- Observation: {result['summary']}",
        f"- Log: `{run.get('log_file') or 'none recorded'}`",
        f"- Report: `{run.get('report_file') or 'none recorded'}`",
        f"- Notification: `{(run.get('notification') or {}).get('status', 'none recorded')}`",
        "",
        "## Required investigation",
        "",
        "1. Confirm the worker's terminal state from its log and report.",
        "2. Identify the violated operational contract and root cause.",
        "3. Propose the smallest regression-tested repair.",
        "4. Record the repair and verification evidence before closing this incident.",
    ]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def claim_investigation(incident_path: Path) -> bool:
    """Claim one incident for agent investigation, returning False if claimed.

    The gate runs twice after a Sunday crawl.  This durable claim prevents a
    second model session for the same variance while keeping a new scheduled
    window eligible for a fresh investigation.
    """
    incident = _load_json(incident_path)
    if incident.get("investigation_claimed_at"):
        return False
    incident["investigation_claimed_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(incident_path, incident)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile a scheduled LoveWork run")
    parser.add_argument("--run-type", required=True, choices=("full", "incremental"))
    parser.add_argument(
        "--principal",
        "--candidate",
        dest="principal",
        default="lj",
        help="Principal whose state and run ledger to reconcile (legacy --candidate accepted)",
    )
    parser.add_argument("--weekday", required=True, type=int, help="Monday=0 … Sunday=6")
    parser.add_argument("--time", required=True, help="Local schedule time, HH:MM")
    parser.add_argument("--grace-minutes", type=int, default=15)
    parser.add_argument("--max-runtime-minutes", type=int, default=330)
    parser.add_argument(
        "--wake-agent-gate",
        action="store_true",
        help="Emit Hermes wakeAgent JSON and claim a newly detected incident.",
    )
    args = parser.parse_args()

    runtime = resolve_principal_runtime(args.principal)
    runs_dir = runtime.cache_dir / "runs"
    incidents_dir = runtime.cache_dir / "incidents"
    result = reconcile(
        run_type=args.run_type,
        weekday=args.weekday,
        schedule_time=args.time,
        grace=timedelta(minutes=args.grace_minutes),
        max_runtime=timedelta(minutes=args.max_runtime_minutes),
        records=run_ledger.list_runs(args.run_type, runs_dir=runs_dir),
    )
    if result["ok"]:
        if args.wake_agent_gate:
            print(json.dumps({"wakeAgent": False}))
        return 0
    json_path, markdown_path = write_incident(result, incidents_dir=incidents_dir)
    if args.wake_agent_gate:
        wake_agent = claim_investigation(json_path)
        payload = {
            "wakeAgent": wake_agent,
            "incident": str(markdown_path),
            "evidence": str(json_path),
            "state": result["state"],
            "summary": result["summary"],
        }
        print(json.dumps(payload, sort_keys=True))
        return 0
    print(f"LoveWork watchdog: {result['summary']}")
    print(f"Incident: {markdown_path}")
    print(f"Evidence: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
