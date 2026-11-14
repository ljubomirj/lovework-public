#!/usr/bin/env python3
"""Durable, evidence-bearing lifecycle records for LoveWork runs.

The Hermes cron launcher only knows that it started a detached worker.  This
module records what the worker itself observed: start, terminal crawl result,
and notification outcome.  It is deliberately small and deterministic so a
watchdog can reconcile scheduled work without an LLM.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import CACHE_DIR
from git_context import describe as describe_git

#: Every outer-operation kind (crawl_operation.py's `mode`) that may own a run
#: record. Kept here as the one authority: the crawl CLI validates against it,
#: and a run launched by the wrapper records its type verbatim. When the two
#: lists drifted, `lovework-crawl.sh reassess …` died at startup with
#: "invalid choice: 'reassess'" before the operation began (2026-09-21).
RUN_TYPES: tuple[str, ...] = ("full", "incremental", "replay", "reassess", "consume")

RUNS_DIR = CACHE_DIR / "runs"


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp suitable for durable evidence."""
    return datetime.now(timezone.utc).isoformat()


def local_date_of(stamp: str) -> Optional[date]:
    """The **local** calendar date of a stamped instant — one clock for day maths.

    Every durable stamp here is UTC, and every day-length decision is local
    (`date.today()`, the principal's day). Reading the first ten characters of a UTC
    stamp as if they were a local date makes the two disagree for the hour after local
    midnight whenever the offset is positive: a review written at 00:30 BST stamps
    `…-19T23:30Z` and reads, to day arithmetic, as *yesterday* (2026-10-20, measured —
    three tests failed at 00:0x local and nowhere else).

    Naive stamps are taken at face value; an unparseable one returns None and the
    caller decides (rather than silently treating it as today).
    """
    raw = str(stamp or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone().date()


def _safe_run_id(run_id: str) -> str:
    """Reject path traversal in a run identifier before deriving a file path."""
    if not run_id or any(part in {"", ".", ".."} for part in Path(run_id).parts):
        raise ValueError("run_id must be a non-empty filename, not a path")
    if Path(run_id).name != run_id:
        raise ValueError("run_id must not contain path separators")
    return run_id


def run_path(run_id: str, runs_dir: Path = RUNS_DIR) -> Path:
    """Return the JSON record path for *run_id*."""
    return runs_dir / f"{_safe_run_id(run_id)}.json"


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
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


def load_run(run_id: str, runs_dir: Path = RUNS_DIR) -> dict[str, Any]:
    """Load one run record, raising FileNotFoundError when it does not exist."""
    return json.loads(run_path(run_id, runs_dir).read_text(encoding="utf-8"))


def start_run(
    run_id: str,
    run_type: str,
    *,
    profile: str,
    hermes_home: str,
    log_file: str,
    pid: int | None = None,
    started_at: str | None = None,
    runs_dir: Path = RUNS_DIR,
) -> dict[str, Any]:
    """Create a running record before the crawl starts."""
    record = {
        "run_id": _safe_run_id(run_id),
        "run_type": run_type,
        "status": "running",
        "started_at": started_at or utc_now(),
        "finished_at": None,
        "exit_code": None,
        "profile": profile,
        "hermes_home": hermes_home,
        "pid": pid,
        "log_file": log_file,
        "report_file": None,
        # Which checkout produced this run: branch, HEAD, dirty state, recent
        # commits. Best-effort (never raises) — see git_context.
        "git": describe_git(),
        "notification": {
            "status": "pending",
            "provider": None,
            "message_id": None,
            "attempted_at": None,
            "error": None,
        },
    }
    _atomic_write(run_path(run_id, runs_dir), record)
    return record


def finish_run(
    run_id: str,
    *,
    status: str,
    exit_code: int,
    report_file: str | None = None,
    error: str | None = None,
    finished_at: str | None = None,
    runs_dir: Path = RUNS_DIR,
) -> dict[str, Any]:
    """Record the crawl terminal outcome, independent of notification."""
    if status not in {"succeeded", "failed"}:
        raise ValueError("status must be 'succeeded' or 'failed'")
    record = load_run(run_id, runs_dir)
    record.update(
        {
            "status": status,
            "finished_at": finished_at or utc_now(),
            "exit_code": exit_code,
            "report_file": report_file,
            "error": error,
        }
    )
    _atomic_write(run_path(run_id, runs_dir), record)
    return record


def record_notification(
    run_id: str,
    *,
    status: str,
    provider: str,
    message_id: str | None = None,
    error: str | None = None,
    attempted_at: str | None = None,
    reason: str | None = None,
    runs_dir: Path = RUNS_DIR,
) -> dict[str, Any]:
    """Persist the notification result and its delivery evidence.

    `suppressed` is the third terminal state: the run deliberately sent no
    email, and says why (LJ, 2026-10-18: post-crawl emails retired — the
    Monday state email carries the state instead). It is *not* a failure and
    *not* a delivery: it carries no message_id, so a reader cannot mistake it
    for one, and the watchdog accepts it without waking the investigator.
    """
    if status not in {"sent", "failed", "suppressed"}:
        raise ValueError("notification status must be 'sent', 'failed' or 'suppressed'")
    if status == "sent" and not message_id:
        raise ValueError("a sent notification requires a Gmail message_id")
    if status == "suppressed" and not reason:
        raise ValueError("a suppressed notification must record a reason")
    record = load_run(run_id, runs_dir)
    record["notification"] = {
        "status": status,
        "provider": provider,
        "message_id": message_id,
        "attempted_at": attempted_at or utc_now(),
        "error": error,
        "reason": reason,
    }
    _atomic_write(run_path(run_id, runs_dir), record)
    return record


def list_runs(run_type: str | None = None, runs_dir: Path = RUNS_DIR) -> list[dict[str, Any]]:
    """Return valid run records, newest first, optionally for one run type."""
    if not runs_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in runs_dir.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if run_type is None or record.get("run_type") == run_type:
            records.append(record)
    return sorted(records, key=lambda item: item.get("started_at") or "", reverse=True)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Record LoveWork run lifecycle evidence")
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--run-id", required=True)
    start.add_argument("--run-type", required=True, choices=RUN_TYPES)
    start.add_argument("--profile", required=True)
    start.add_argument("--hermes-home", required=True)
    start.add_argument("--log-file", required=True)
    start.add_argument("--pid", type=int)
    start.add_argument(
        "--started-at",
        help="ISO-8601 start time for evidence-preserving historical reconstruction",
    )

    finish = subparsers.add_parser("finish")
    finish.add_argument("--run-id", required=True)
    finish.add_argument("--status", required=True, choices=("succeeded", "failed"))
    finish.add_argument("--exit-code", required=True, type=int)
    finish.add_argument("--report-file")
    finish.add_argument("--error")
    finish.add_argument(
        "--finished-at",
        help="ISO-8601 finish time for evidence-preserving historical reconstruction",
    )

    notification = subparsers.add_parser("notification")
    notification.add_argument("--run-id", required=True)
    notification.add_argument("--status", required=True,
                              choices=("sent", "failed", "suppressed"))
    notification.add_argument("--reason",
                              help="why no email was sent (required for --status suppressed)")
    notification.add_argument("--provider", required=True)
    notification.add_argument("--message-id")
    notification.add_argument("--error")
    notification.add_argument(
        "--attempted-at",
        help="ISO-8601 notification time for evidence-preserving historical reconstruction",
    )

    args = parser.parse_args()
    if args.command == "start":
        result = start_run(
            args.run_id,
            args.run_type,
            profile=args.profile,
            hermes_home=args.hermes_home,
            log_file=args.log_file,
            pid=args.pid,
            started_at=args.started_at,
            runs_dir=args.runs_dir,
        )
    elif args.command == "finish":
        result = finish_run(
            args.run_id,
            status=args.status,
            exit_code=args.exit_code,
            report_file=args.report_file,
            error=args.error,
            finished_at=args.finished_at,
            runs_dir=args.runs_dir,
        )
    else:
        result = record_notification(
            args.run_id,
            status=args.status,
            provider=args.provider,
            message_id=args.message_id,
            error=args.error,
            attempted_at=args.attempted_at,
            reason=getattr(args, "reason", None),
            runs_dir=args.runs_dir,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
