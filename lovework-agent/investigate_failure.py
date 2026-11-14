#!/usr/bin/env python3
"""investigate_failure.py — the deterministic sweep-failure investigator.

Decoupling plan A4 (2026-10-21): the three Hermes agent jobs
`lovework-<p>-full-meta-investigator` were wake-gated prompts that read a
failed sweep and reasoned about it. This replaces them with code: the
watchdog launcher (`scripts/cron/lovework-watchdog.sh`) calls this after
reconciling the run, and for every failed run record in the window that has
no dossier yet, it writes one — the facts, a classification from the exit
code and the log, and the documented remedy — into
`state/<p>/logs/investigations/<run_id>.md`, and prints a short summary the
launcher reports to the channel.

An agent's judgement stays available ad hoc; it is no longer on the schedule.
Idempotent: a run with a dossier is never re-investigated.

    investigate_failure.py --profile lj [--hours 48] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402  (path set above, matching the sibling modules)

WINDOW_HOURS = 48
LOG_TAIL_LINES = 60

#: Exit code → what it means, from the crawl wrapper's own contract
#: (crawl_operation.py, lovework-crawl.sh).
EXIT_MEANINGS = {
    75: ("writer busy", "another operation held the principal's writer lease; "
                        "the attempt record is evidence — cron retries, so a "
                        "lone 75 is not a failure to act on"),
    2: ("notification token dead", "the Gmail OAuth token failed the "
                                   "in-boundary pre-flight; the bundle was "
                                   "sealed, no crawl ran"),
    78: ("configuration", "the launcher could not resolve the LoveWork root "
                          "or profile"),
    64: ("usage", "the launcher was called with an unsupported principal"),
    143: ("killed", "SIGTERM — the run was stopped from outside (a cap, a "
                    "watchdog, a hand)"),
}

#: Log-line signatures that name the class before the exit code does.
LOG_SIGNATURES = (
    ("[SEAL_FAILED]", "seal failed after the crawl",
     "the crawl may have succeeded; staging is retained for recover() — "
     "re-seal, do not re-crawl"),
    ("[LOCK] writer busy", "writer busy", "see exit 75"),
    ("[TOKEN]", "notification token", "see exit 2"),
    ("429", "provider quota", "the model relay rate-limited the run; the "
                              "assessment cache makes a re-run cheap"),
    ("401", "provider auth", "the relay rejected the key — check "
                             "lovework-agent/.env (the oc_sk_ key, 2026-10-17)"),
    ("Traceback (most recent call last)", "exception",
     "the last exception line in the dossier names it"),
)

REMEDIES = (
    "`lovework-crawl.sh reassess <p>` — re-score the registry against retained "
    "evidence and seal, without re-fetching (the partial-run completion path)",
    "`lovework-crawl.sh replay <p>` — re-process the archived emails only",
    "`lovework-crawl.sh full <p>` — a fresh sweep, when the cause is fixed",
)


def _runs_dir(state_root: Path) -> Path:
    return state_root / "cache" / "runs"


def _dossier_dir(state_root: Path) -> Path:
    return state_root / "logs" / "investigations"


def _parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def failed_runs(state_root: Path, *, run_type: str = "full",
                hours: int = WINDOW_HOURS,
                now: datetime | None = None) -> list[dict]:
    """Failed run records of this type started inside the window, newest first."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    out: list[dict] = []
    runs = _runs_dir(state_root)
    if not runs.is_dir():
        return out
    for path in runs.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(record.get("run_type") or "") != run_type:
            continue
        if str(record.get("status") or "") != "failed":
            continue
        started = _parse_when(record.get("started_at"))
        if started is None or started < cutoff:
            continue
        record["_path"] = str(path)
        out.append(record)
    out.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    return out


def _log_tail(log_file: str | None, lines: int = LOG_TAIL_LINES) -> list[str]:
    if not log_file:
        return []
    path = Path(log_file)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]


def classify(record: dict, tail: list[str]) -> tuple[str, str]:
    """(class, what it means) — from the log's signatures first, the exit code
    second, and the wrapper's own words last. Deterministic by construction."""
    joined = "\n".join(tail)
    for needle, name, meaning in LOG_SIGNATURES:
        if needle in joined:
            if name == "exception":
                last = [l for l in tail if re.match(r"^\w+(Error|Exception)\b", l)]
                return name, (last[-1][:200] if last else meaning)
            return name, meaning
    code = record.get("exit_code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    if code in EXIT_MEANINGS:
        return EXIT_MEANINGS[code]
    error = str(record.get("error") or "").strip()
    return "unclassified", (error[:200] if error else
                            f"the crawl subprocess exited {code}; the log tail "
                            "is the evidence")


def _git_line(tail: list[str]) -> str:
    for line in tail:
        if line.startswith("[GIT]"):
            return line
    return ""


def render_dossier(record: dict, tail: list[str], kind: str, meaning: str,
                   *, principal: str, today: date) -> str:
    started = str(record.get("started_at") or "?")
    finished = str(record.get("finished_at") or "?")
    lines = [
        f"# Failure dossier — {principal} — {record.get('run_id')}",
        "",
        f"Written by `investigate_failure.py` on {today.isoformat()} — code, not "
        "an agent: the facts below are read from the run record and the log; "
        "the class is a lookup, the remedy is the documented one. A human (or "
        "an agent you invoke) decides.",
        "",
        "## Facts",
        "",
        f"- **Run**: `{record.get('run_id')}` ({record.get('run_type')})",
        f"- **Started / finished**: {started} → {finished}",
        f"- **Exit code**: {record.get('exit_code')}",
        f"- **Recorded error**: {record.get('error') or '—'}",
        f"- **Log**: `{record.get('log_file') or '—'}`",
        f"- **Report**: `{record.get('report_file') or 'none written'}`",
    ]
    git = _git_line(tail)
    if git:
        lines.append(f"- **Code**: {git}")
    lines += ["", "## Class", "", f"**{kind}** — {meaning}", "", "## Remedy", ""]
    lines += [f"- {r.replace('<p>', principal)}" for r in REMEDIES]
    lines += ["", f"## Log tail (last {len(tail)} lines)", "", "```"]
    lines += tail or ["(no log on file)"]
    lines += ["```", ""]
    return "\n".join(lines)


def investigate(principal: str, *, state_dir: Path | None = None,
                run_type: str = "full", hours: int = WINDOW_HOURS,
                dry_run: bool = False, now: datetime | None = None,
                today: date | None = None) -> dict:
    """Write a dossier for every failed run in the window that has none."""
    state_dir = state_dir or config.STATE_DIR
    state_root = state_dir / principal
    today = today or date.today()
    out = {"principal": principal, "failed": 0, "written": [], "already": 0,
           "dry_run": dry_run, "classes": {}}
    if not state_root.is_dir():
        return out
    dossiers = _dossier_dir(state_root)
    for record in failed_runs(state_root, run_type=run_type, hours=hours, now=now):
        out["failed"] += 1
        target = dossiers / f"{record.get('run_id')}.md"
        if target.is_file():
            out["already"] += 1
            continue
        tail = _log_tail(record.get("log_file"))
        kind, meaning = classify(record, tail)
        out["classes"][str(record.get("run_id"))] = kind
        if dry_run:
            out["written"].append(str(target) + " (dry run)")
            continue
        dossiers.mkdir(parents=True, exist_ok=True)
        text = render_dossier(record, tail, kind, meaning,
                              principal=principal, today=today)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        out["written"].append(str(target))
    return out


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--run-type", default="full")
    parser.add_argument("--hours", type=int, default=WINDOW_HOURS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = investigate(args.profile, run_type=args.run_type, hours=args.hours,
                         dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if not result["failed"]:
        return 0                      # silence: nothing failed, nothing to say
    for path in result["written"]:
        run_id = Path(path).stem
        print(f"[investigate] {args.profile}: {run_id} → "
              f"{result['classes'].get(run_id, '?')} — dossier {path}")
    if result["already"] and not result["written"]:
        print(f"[investigate] {args.profile}: {result['already']} failed run(s) "
              "already have dossiers")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
