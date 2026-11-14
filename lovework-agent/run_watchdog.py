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
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import run_ledger
from principal_runtime import resolve_principal_runtime
from config import CACHE_DIR

INCIDENTS_DIR = CACHE_DIR / "incidents"

#: A run still in flight past its configured bound is only a variance when it
#: has stopped making progress, or when it has outgrown the worst run this
#: principal has ever finished.  ``--max-runtime-minutes`` is calibrated by
#: hand in each principal's watchdog script and can sit below the principal's
#: own observed tail (VJ: 105 min configured against 4h32m observed, which
#: fired `overdue` on healthy sweeps on 2026-10-04, 10-11 and 10-18); deriving
#: a floor from the principal's own history keeps that mis-calibration from
#: spending a model session, without hiding a genuinely stuck worker.
BOUND_HEADROOM = 1.25
LOG_STALE_MINUTES = 15


def _worker_evidence(record: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Best-effort liveness evidence for an in-flight run.

    Distinguishes a legitimately long run (process alive, log being written)
    from a hung one (process gone or log stale) in the overdue incident packet.
    Never raises: every field is optional and failures degrade to None.
    """
    evidence: dict[str, Any] = {"pid": record.get("pid")}
    pid = record.get("pid")
    try:
        if pid is None:
            raise ValueError("no pid recorded")
        os.kill(int(pid), 0)
        evidence["process_alive"] = True
    except (TypeError, ValueError, ProcessLookupError):
        evidence["process_alive"] = False
    except PermissionError:
        evidence["process_alive"] = None
    log_file = record.get("log_file")
    if log_file:
        try:
            modified = datetime.fromtimestamp(Path(log_file).stat().st_mtime, tz=timezone.utc)
            evidence["log_last_write"] = modified.isoformat()
            evidence["log_stale_seconds"] = max(0, int((now - modified).total_seconds()))
        except OSError:
            evidence["log_last_write"] = None
            evidence["log_stale_seconds"] = None
    return evidence


def observed_runtime_minutes(records: list[dict[str, Any]] | None) -> float | None:
    """Largest observed runtime (minutes) among this run type's successes.

    The hand-set overdue bound lives in the principal's watchdog script, and a
    bound calibrated to the median (or to a pre-tracker era) fires on
    legitimate long runs. This tail is what `effective_runtime_minutes` floors
    the bound with, so a slow-but-progressing run is not a variance; it is also
    recorded in every `overdue` packet, making that mis-calibration a fact
    rather than a judgment the investigator has to re-derive (docs/14 §loop,
    step 7: "a deterministic check that would catch the same problem next
    time"). Never raises; records without a usable interval are skipped.
    """
    best: float | None = None
    for record in records or []:
        if record.get("status") != "succeeded":
            continue
        started = _parse_iso(record.get("started_at"))
        finished = _parse_iso(record.get("finished_at"))
        if started is None or finished is None:
            continue
        minutes = (finished - started).total_seconds() / 60.0
        if minutes <= 0:
            continue
        best = minutes if best is None else max(best, minutes)
    return best


def effective_runtime_minutes(
    configured_minutes: int,
    observed_tail: float | None,
    headroom: float = BOUND_HEADROOM,
) -> int:
    """The bound actually applied: the configured one, floored at the tail.

    A hand-set bound below the principal's own observed worst case turns every
    healthy overrun into an `overdue` variance and, downstream, into a model
    session that can only ever conclude "the bound is wrong".  Taking the
    larger of the configured bound and the observed tail plus *headroom* keeps
    the check meaningful — a run longer than anything the principal has ever
    finished still alarms — while a merely slow run does not.

    No history means no derivation: the configured bound stands alone.
    """
    if observed_tail is None or observed_tail <= 0:
        return configured_minutes
    return max(configured_minutes, math.ceil(observed_tail * headroom))


def _still_progressing(worker: dict[str, Any], stale_seconds: float) -> bool:
    """True when the worker is alive and its log was written recently.

    Both facts are required: a live pid with a silent log is exactly the shape
    of a hung worker, which is what `overdue` is for.
    """
    stale = worker.get("log_stale_seconds")
    return worker.get("process_alive") is True and stale is not None and stale <= stale_seconds


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
    bound_headroom: float = BOUND_HEADROOM,
    log_stale: timedelta = timedelta(minutes=LOG_STALE_MINUTES),
) -> dict[str, Any]:
    """Compare a scheduled-run expectation with observed worker evidence."""
    current = now or datetime.now().astimezone()
    expected_at = expected_run_at(current, weekday, schedule_time)
    if current < expected_at + grace:
        return {"ok": True, "state": "not_due", "expected_at": expected_at.isoformat()}

    next_expected_at = expected_at + timedelta(days=7)
    observed_records = records if records is not None else run_ledger.list_runs(run_type)
    matching_run = _matching_run(observed_records, expected_at, next_expected_at)
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
        elapsed = current - started_at if started_at else None
        if elapsed is not None and elapsed > max_runtime:
            worker = _worker_evidence(matching_run, current)
            observed_tail = observed_runtime_minutes(observed_records)
            configured_minutes = int(max_runtime.total_seconds() // 60)
            effective_minutes = effective_runtime_minutes(
                configured_minutes, observed_tail, bound_headroom
            )
            bounds: dict[str, Any] = {
                "configured_max_minutes": configured_minutes,
                "effective_max_minutes": effective_minutes,
            }
            if observed_tail is not None:
                bounds["observed_max_minutes"] = round(observed_tail, 1)
            if _still_progressing(worker, log_stale.total_seconds()) and elapsed <= timedelta(
                minutes=effective_minutes
            ):
                return base | bounds | {
                    "ok": True,
                    "state": "running_long",
                    "summary": (
                        f"{run_type} run is still in flight at "
                        f"{elapsed.total_seconds() / 60.0:.0f} min, inside the "
                        f"{effective_minutes}-minute bound derived from this principal's "
                        f"observed maximum; worker alive and its log is being written."
                    ),
                    "worker": worker,
                }
            result = base | bounds | {
                "state": "overdue",
                "summary": f"{run_type} run exceeded the {max_runtime} runtime limit.",
                "worker": worker,
            }
            if observed_tail is not None and observed_tail > configured_minutes:
                result["bound_note"] = (
                    f"configured --max-runtime-minutes ({configured_minutes}) sits below this "
                    f"principal's observed successful-run maximum ({observed_tail:.0f} min); "
                    f"the applied bound is {effective_minutes} min (tail × headroom "
                    f"{bound_headroom:g}), so the bound is not the variance — this run is "
                    "overdue because its worker stopped progressing or outgrew that bound."
                )
            return result
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
    status = str(notification.get("status") or "")
    if status == "suppressed":
        # A *terminal* state, not a gap: the run deliberately sent no email and
        # said why (post-crawl emails retired 2026-10-18; the Monday state email
        # carries the state). Nothing to reconcile, so nothing to wake for — the
        # reason travels with the verdict so a reader sees it in the incident
        # markdown too.
        return {
            "ok": True,
            "state": "resolved",
            "notification": "suppressed"
            + (f" — {notification.get('reason')}" if notification.get("reason") else ""),
            "expected_at": expected_at.isoformat(),
            "run": matching_run,
        }
    if status != "sent" or not notification.get("message_id"):
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
    ]
    worker = result.get("worker") or {}
    if worker:
        lines += [
            f"- Worker pid: `{worker.get('pid')}`",
            f"- Process alive: `{worker.get('process_alive')}`",
            f"- Log last write: `{worker.get('log_last_write')}` (stale {worker.get('log_stale_seconds')}s)",
        ]
    if "observed_max_minutes" in result:
        lines.append(f"- Observed successful-run max: `{result['observed_max_minutes']}` min")
    if result.get("effective_max_minutes") is not None:
        lines.append(
            f"- Applied bound: `{result['effective_max_minutes']}` min "
            f"(configured `{result.get('configured_max_minutes')}` min)"
        )
    if result.get("bound_note"):
        lines.append(f"- Bound check: {result['bound_note']}")
    lines += [
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
        "--bound-headroom",
        type=float,
        default=BOUND_HEADROOM,
        help=(
            "Factor applied to this principal's observed successful tail to derive the "
            f"applied bound when it exceeds --max-runtime-minutes (default {BOUND_HEADROOM})."
        ),
    )
    parser.add_argument(
        "--log-stale-minutes",
        type=int,
        default=LOG_STALE_MINUTES,
        help=(
            "An in-flight worker whose log is older than this is treated as stalled "
            f"whatever the bound says (default {LOG_STALE_MINUTES})."
        ),
    )
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
        bound_headroom=args.bound_headroom,
        log_stale=timedelta(minutes=args.log_stale_minutes),
    )
    if result["ok"]:
        if args.wake_agent_gate:
            print(json.dumps({"wakeAgent": False}))
            return 0
        if result.get("state") == "running_long":
            # One line, no model session: the check ran and the run is healthy
            # but slow. Silence here would hide the overrun from the operator.
            print(f"LoveWork watchdog: {result['summary']}")
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
