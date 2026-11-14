"""**First-level monitoring**: read the loop's own record, and escalate only what matters.

LJ, 2026-10-20, after a Telegram message that said *"[tick] lj hit the wall clock (killed
after 1080s); the next run continues"* — correct and useful, but one of dozens he was reading
himself:

  *"Nothing wrong there, but it occurs to me: I'm doing the monitoring. Maybe we can employ
  some LLM based solution to do the 1st level of monitoring?"*

So this is the first level. The division is the project's own doctrine: **the facts are
collected by code** (counts, ages, statuses — exact, free, testable), and **the judgement is
the model's** (is this normal? does the principal need to act? worth saying?). The monitor
never invents a fact: every sentence it prints must cite a signal it was given.

Three verdicts, and the default is silence:

- `quiet` — nothing to say. Prints nothing, records the reasoning.
- `informational` — normal, but worth one line **the first time it is seen**.
- `needs_principal` — a decision only LJ can make.
- `broken` — something is failing and nothing is fixing it.

Every triage is appended to `state/<p>/monitor/triage.jsonl`, so "why did the monitor stay
quiet?" is answerable — the same discipline the heartbeat demanded.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

import config
import health

WINDOW_HOURS = float(os.getenv("LOVEWORK_MONITOR_WINDOW_HOURS", "24"))
#: A **persisting** condition is said again after this long, and not before. The
#: alarm is state-based (see `signature`), so a reworded headline cannot re-arm it;
#: without a cool-off a genuinely stuck loop would go silent for ever.
REALARM_HOURS = float(os.getenv("LOVEWORK_MONITOR_REALARM_HOURS", "6"))
#: A heartbeat older than this means the loop is not running (the tick is hourly).
STALE_HEARTBEAT_MINUTES = float(os.getenv("LOVEWORK_MONITOR_STALE_MINUTES", "150"))
#: A row still in flight after this long is a hang, not a run: the scheduler's own
#: in-flight statuses are otherwise **not** failures. Without this, the monitor reads
#: the row the scheduler wrote for the monitor itself and calls itself broken while it
#: is still thinking (measured 2026-10-20 22:50: 8 alarms, then one accusing itself).
IN_FLIGHT = {"running", "claimed", "pending"}
IN_FLIGHT_GRACE_MINUTES = float(os.getenv("LOVEWORK_MONITOR_INFLIGHT_MINUTES", "90"))


class MonitorVerdict(BaseModel):
    """What the first level concluded. One JSON object, every field short."""

    verdict: Literal["quiet", "informational", "needs_principal", "broken"] = Field(
        description="quiet = nothing to say; informational = normal but worth one line; "
                    "needs_principal = LJ must decide; broken = failing and unfixed")
    headline: str = Field(default="", description="One line, ≤120 chars, no jargon")
    detail: str = Field(default="", description="2-4 sentences, citing the signals by name")
    evidence: list[str] = Field(default_factory=list,
                                description="The signal keys this conclusion rests on")


PROMPT = """You are LoveWork's first-level monitor. LoveWork is a work-discovery agent that \
crawls job adverts, scores them against a principal's profile, prepares application packs, \
and runs an hourly loop that acts on a local model.

Below are the facts collected from its own records in the last {hours:.0f} hours. They are \
complete and they are exact: do not invent anything that is not here, and do not speculate \
about causes you cannot see.

Your job is to decide whether the principal needs to read anything, and to say it in one \
or two sentences if he does.

Judge like this:
- `quiet` — every principal's heartbeat is fresh, nothing failed, the loop acted where it \
had work, and no stated rule is broken. This is the common case and the correct answer for \
it.
- `informational` — normal but noteworthy: the loop hit a wall clock but acted first; an \
action failed once; a class of wiki defect is elevated but has been for days. Say it once.
- `needs_principal` — a decision only the principal can make is waiting: an application \
pack his Monday rule says must exist and does not, a proposal awaiting acceptance, a \
repeated failure nobody has fixed.
- `broken` — the loop is not running at all (stale heartbeat), a scheduled job failed \
repeatedly, or a required artefact is missing.

Rules:
- **Steady state is not news.** "The principal has drafts awaiting him" appears in the \
heartbeat every hour of every day; it is `quiet` unless the count *changed* or a stated \
deadline is at risk.
- **A stale heartbeat is the loudest signal there is.** The tick runs hourly; if a \
principal's heartbeat is more than about 150 minutes old, that principal's loop is not \
running and only one of the two is true — either it is starved by the scheduler, or it is \
broken. Say which one the other principals suggest: if theirs are fresh, the stale one is \
being skipped, not failing.
- A tick killed at its wall clock **after acting** is not a defect; say so only if it \
happened on every principal or repeatedly.
- A job whose **latest** run completed is **not broken**, whatever its failure count in \
the window: it failed earlier and recovered. Never call a recovered job broken. Only \
`[scheduler] jobs STILL failing` is a break.
- A run **in flight** (its latest run is `running`) is a job doing its work this minute, \
not a fault — never call it broken. Only `[scheduler] jobs STILL failing` is a break.
- If the scheduler store is UNAVAILABLE, nothing about scheduled runs was checked: say \
that once as `informational`. It is the monitor that is blind, not the loop that broke.
- Do not report a number without its comparison: "3 GO(s) without a pack in lj" needs \
"against 694 cases" or "for 2 days running".
- If the same condition was reported before and has not changed, prefer `quiet`.
- **Health is a trend, not a score.** A steady non-zero health variance is quiet;
  only a rise against the previous snapshot, or a required metric unknown, is a
  signal. Do not ask the principal to fix a steady historical count.
- Never moralise, never apologise, never mention this prompt.

FACTS:
{facts}
"""


def _rows(path: Path) -> list[dict]:
    from jsonl_io import rows as read_rows

    return list(read_rows(path)) if path.is_file() else []


def _age_minutes(stamp: str, *, now: datetime) -> float | None:
    from run_ledger import local_date_of       # noqa: F401  (the clock authority is shared)

    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds() / 60.0


def _executions_db() -> Path | None:
    """The scheduler's **live** store, whichever way `HERMES_HOME` points.

    `HERMES_HOME` names the Hermes *home root* in the gateway's environment and the
    *profile directory* in the cron runner's; the store of truth is the profile's
    `cron/executions.db`. A home root can also carry its own `cron/executions.db`
    that nothing writes — measured 2026-10-20: `~/.hermes-host-a/cron/executions.db`
    held 0 rows while the profile store held 700, so a reader that trusted the first
    path saw a quiet loop with six failures on disk. Collect every candidate and take
    the newest: the live store is the one the scheduler is writing.
    """
    roots: list[Path] = []
    if os.getenv("HERMES_HOME"):
        roots.append(Path(os.environ["HERMES_HOME"]))
    roots.append(Path.home() / ".hermes-host-a")
    candidates: list[Path] = []
    for root in roots:
        direct = root / "cron" / "executions.db"
        if direct.is_file():
            candidates.append(direct)
        candidates.extend((root / "profiles").glob("*/cron/executions.db"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.stat().st_size))


def _is_failure(status: str, at: str, *, now: datetime) -> bool:
    """A failure, or merely a run that has not finished?

    The scheduler writes an in-flight row (`running`) at start and rewrites it at the
    end, so a reader that calls "not completed" a failure calls the **monitor itself**
    broken every time it runs — which is how the 22:50 message accused job
    `f9515d76dd25` of failing while that job was the monitor, mid-thought. In flight
    is not a failure; in flight **past the grace** is a hang, and a hang is.
    """
    if status == "completed":
        return False
    if status in IN_FLIGHT:
        age = _age_minutes(at, now=now)
        return age is not None and age > IN_FLIGHT_GRACE_MINUTES
    return True


def _cron_jobs_file(state_dir: Path) -> Path:
    """LoveWork's own run record: one row per launcher run (env.sh's EXIT trap)."""
    return state_dir / "logs" / "jobs.jsonl"


def _rollup_cron_rows(path: Path, window_hours: float, *, now: datetime) -> list[dict]:
    """One row per job from our own `jobs.jsonl`: the latest exit decides broken."""
    cutoff = (now - timedelta(hours=window_hours)).isoformat()
    by_job: dict[str, dict] = {}
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []
    for row in sorted(rows, key=lambda r: str(r.get("at") or "")):
        job = str(row.get("job") or "")
        if not job or str(row.get("at") or "") < cutoff:
            continue
        entry = by_job.setdefault(job, {"job": job, "failures": []})
        entry["latest"] = {"at": str(row.get("at") or "")[:19],
                           "status": "completed" if int(row.get("exit") or 0) == 0
                                     else f"exit {row.get('exit')}"}
        if int(row.get("exit") or 0) != 0:
            entry["failures"].append({"at": str(row.get("at") or "")[:19],
                                      "status": f"exit {row.get('exit')}"})
    out = []
    for entry in by_job.values():
        if not entry["failures"]:
            continue
        entry["count"] = len(entry["failures"])
        entry["broken"] = entry["latest"]["status"] != "completed"
        entry["state"] = "failing" if entry["broken"] else "recovered"
        entry["last_at"] = entry["failures"][-1]["at"]
        entry["error"] = entry["failures"][-1]["status"]
        del entry["failures"]
        out.append(entry)
    out.sort(key=lambda row: (not row["broken"], row["last_at"]), reverse=True)
    return out


def _cron_failures(window_hours: float, *, now: datetime,
                   state_dir: Path | None = None) -> list[dict]:
    """Jobs that failed in the window, with **one row per job**: is it still broken?

    A failure is a fact; *broken* is a state. The difference is the whole point
    (2026-10-20: eight Telegram alarms for one syntax error that had already been
    fixed — the count of failures inside a 24 h window keeps the condition "true"
    long after the last completion). So the latest run decides `broken`, and a job
    that failed and then completed is reported as recovered, never as broken.

    **Since the Hermes severance (2026-10-22) the scheduler is the user's
    crontab**, and cron keeps no queryable table — so the first source is
    LoveWork's own `state/logs/jobs.jsonl`, written by `scripts/cron/env.sh`'s
    EXIT trap for every launcher run. The Hermes store stays as the fallback for
    a host that has not cut over, and is never mixed with ours.
    """
    own = _cron_jobs_file(state_dir) if state_dir is not None else None
    if own is not None and own.is_file():
        return _rollup_cron_rows(own, window_hours, now=now)
    db = _executions_db()
    if db is None:
        return []
    cutoff = (now - timedelta(hours=window_hours)).isoformat()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        found = list(con.execute(
            "select job_id, started_at, status, error from executions "
            "where started_at >= ? order by started_at desc",
            (cutoff,)))
        con.close()
    except sqlite3.Error:
        return []
    by_job: dict[str, dict] = {}
    for job_id, started_at, status, error in found:
        entry = by_job.setdefault(job_id, {"job": job_id, "latest": None, "failures": []})
        if entry["latest"] is None:          # desc order: the first row seen is the newest
            entry["latest"] = {"at": str(started_at)[:19], "status": status}
        if _is_failure(status, str(started_at), now=now):
            entry["failures"].append({
                "at": str(started_at)[:19], "status": status,
                "error": (str(error or "").strip().splitlines() or [""])[0][:160]})
    out = []
    for entry in by_job.values():
        if not entry["failures"]:
            continue
        latest = entry["latest"]
        entry["count"] = len(entry["failures"])
        entry["broken"] = _is_failure(latest["status"], latest["at"], now=now)
        entry["state"] = ("failing" if entry["broken"]
                          else "running" if latest["status"] in IN_FLIGHT
                          else "recovered")
        entry["last_at"] = entry["failures"][0]["at"]
        entry["error"] = entry["failures"][0]["error"]
        del entry["failures"]
        out.append(entry)
    out.sort(key=lambda row: (not row["broken"], row["last_at"]), reverse=True)
    return out


def signals(*, profiles: list[str] | None = None, state_dir: Path | None = None,
            window_hours: float = WINDOW_HOURS, now: datetime | None = None) -> dict:
    """The facts, collected deterministically. No model, no network, no guessing."""
    state_dir = state_dir or config.STATE_DIR
    now = now or datetime.now(timezone.utc)
    profiles = profiles or list(config.principal_names(state_dir))
    cutoff = now - timedelta(hours=window_hours)
    out: dict = {"at": now.isoformat(timespec="seconds"), "window_hours": window_hours,
                 "principals": {}, "cron": _cron_failures(window_hours, now=now,
                                                        state_dir=state_dir),
                 "cron_store": ("ok" if _cron_jobs_file(state_dir).is_file()
                                or _executions_db() is not None else "unavailable")}

    for principal in profiles:
        root = state_dir / principal
        if not root.is_dir():
            continue
        beat = (_rows(root / "loop" / "heartbeat.jsonl") or [{}])[-1]
        receipts = [r for r in _rows(root / "loop" / "receipts.jsonl")
                    if str(r.get("at", "")) >= cutoff.isoformat(timespec="seconds")
                    and r.get("action")]
        acted = [r for r in receipts if r.get("outcome") == "acted"]
        failed = [r for r in receipts if r.get("outcome") == "failed"]
        blocked = [r for r in receipts if r.get("outcome") == "blocked"]
        envelope = {}
        env_path = root / "loop" / f"envelope-{now.date().isoformat()}.json"
        if env_path.is_file():
            try:
                envelope = json.loads(env_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                envelope = {}
        selfheal = {}
        heal_path = root / "cache" / "selfheal.json"
        if heal_path.is_file():
            try:
                selfheal = json.loads(heal_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                selfheal = {}
        try:
            health_state = health.read_status(root)
        except Exception:                   # a malformed health file is not a crash
            health_state = None
        out["principals"][principal] = {
            "heartbeat": {
                "at": beat.get("at"),
                "minutes_old": round(_age_minutes(beat.get("at"), now=now) or -1, 1),
                "outcome": beat.get("outcome"),
                "why": (beat.get("why") or "")[:160],
                "due": beat.get("due"),
            },
            "receipts": {"acted": len(acted), "failed": len(failed),
                         "blocked": len(blocked), "total": len(receipts)},
            "last_failure": (failed[-1].get("error") or "")[:200] if failed else "",
            "envelope": {k: envelope.get(k) for k in
                         ("calls", "minutes", "ticks", "bound") if k in envelope},
            "selfheal": {"at": selfheal.get("at"),
                         "classes": selfheal.get("classes") or {},
                         "rose": selfheal.get("rose") or [],
                         "gaps": selfheal.get("gaps") or {}},
            "health": health_state,
        }
    return out


def _facts_text(sig: dict) -> str:
    """The signals as the model reads them — compact, complete, no prose."""
    lines = [f"now: {sig['at']}  window: {sig['window_hours']:.0f}h"]
    for principal, data in sig["principals"].items():
        beat = data["heartbeat"]
        lines.append(
            f"\n[{principal}] heartbeat {beat['minutes_old']} min old — outcome "
            f"{beat['outcome']!r}, due {beat['due']}, why: {beat['why']!r}")
        rec = data["receipts"]
        lines.append(f"[{principal}] receipts in window: acted {rec['acted']}, "
                     f"failed {rec['failed']}, blocked {rec['blocked']}, total {rec['total']}")
        if data["last_failure"]:
            lines.append(f"[{principal}] last failure: {data['last_failure']}")
        if data["envelope"]:
            lines.append(f"[{principal}] envelope today: {data['envelope']}")
        heal = data["selfheal"]
        if heal.get("classes"):
            total = sum(heal["classes"].values())
            lines.append(f"[{principal}] selfheal {heal.get('at')}: "
                         f"{total} defect(s) across {len(heal['classes'])} class(es); "
                         f"rose: {heal['rose'] or 'none'}")
        hstate = data.get("health")
        if hstate:
            markers = []
            if hstate.get("rose"):
                markers.append("ROSE " + ",".join(hstate["rose"]))
            if hstate.get("unknown"):
                markers.append(f"UNKNOWN {hstate['unknown']}")
            detail = (f"[{principal}] health variance "
                      f"{float(hstate.get('variance', 0.0)):.3f} "
                      f"(previous {hstate.get('previous_variance')}, "
                      f"delta {float(hstate.get('delta', 0.0)):+.3f}); "
                      f"unknown {hstate.get('unknown', 0)}"
                      + (f"; worst {hstate['worst'][0].get('name')} "
                         f"{float(hstate['worst'][0].get('error', 0)):.2f}"
                         if hstate.get("worst") else "")
                      + ("; " + "; ".join(markers) if markers else ""))
            lines.append(detail)
    failing = [row for row in sig["cron"] if row["state"] == "failing"]
    running = [row for row in sig["cron"] if row["state"] == "running"]
    recovered = [row for row in sig["cron"] if row["state"] == "recovered"]
    if sig.get("cron_store") == "unavailable":
        lines.append("\n[scheduler] store UNAVAILABLE — scheduled runs were NOT checked "
                     "(blindness, not a passing grade).")
    if failing:
        lines.append("\n[scheduler] jobs STILL failing (their latest run did not complete):")
        for row in failing[:8]:
            lines.append(f"  job {row['job']}: {row['count']} failure(s) in window; "
                         f"latest run {row['latest']['at']} {row['latest']['status']}: "
                         f"{row['error']}")
    if running:
        lines.append("\n[scheduler] runs in flight right now (started, not finished — "
                     "NOT a break):")
        for row in running[:8]:
            lines.append(f"  job {row['job']}: a run started {row['latest']['at']} is "
                         f"still in progress; {row['count']} earlier failure(s) in window")
    if recovered:
        lines.append("\n[scheduler] jobs that failed earlier and have COMPLETED since "
                     "(recovered — not a current break):")
        for row in recovered[:8]:
            lines.append(f"  job {row['job']}: {row['count']} failure(s) in window; "
                         f"latest run {row['latest']['at']} completed")
    if not sig["cron"]:
        lines.append("\n[scheduler] every scheduled LoveWork run in the window completed.")
    return "\n".join(lines)


def triage(sig: dict, *, client=None) -> tuple[MonitorVerdict, str]:
    """One local-model call: what, if anything, does the principal need to know?"""
    if client is None:
        from tick import model_client

        client = model_client()
    facts = _facts_text(sig)
    verdict = client.structured(
        [{"role": "user",
          "content": PROMPT.format(hours=sig["window_hours"], facts=facts)}],
        MonitorVerdict, temperature=0.0)
    return verdict, facts


def _state_paths(state_dir: Path, principal: str) -> tuple[Path, Path]:
    root = state_dir / principal / "monitor"
    return root / "triage.jsonl", root / "last.json"


def record(sig: dict, verdict: MonitorVerdict, facts: str, *,
           state_dir: Path, principal: str | None = None) -> None:
    """Every triage is written down, whatever it concluded — the audit of the auditor."""
    target = principal or (next(iter(sig["principals"]), "lj"))
    ledger, last = _state_paths(state_dir, target)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": sig["at"], "verdict": verdict.verdict, "headline": verdict.headline,
           "detail": verdict.detail, "evidence": verdict.evidence}
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    last.write_text(json.dumps({**row, "facts": facts}, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _dedupe_path(state_dir: Path) -> Path:
    """One alarm memory for the loop, not one per principal.

    The triage ledger is per principal (who was told what), but "speak" is a statement
    about the loop as a whole — and `_subject` may name a different principal between
    runs, so a per-principal memory forgets the last alarm exactly when the model
    changes its mind about whose it was.
    """
    return state_dir / "monitor" / "dedupe.json"


def signature(sig: dict) -> str:
    """The condition a reader would act on — built from **states**, never from prose.

    A model at temperature 0 still rewords its headline, so equality of sentences is
    not equality of condition (measured 2026-10-20: eight Telegram alarms for one
    syntax error that had already been fixed, each with a fresh sentence). The
    signature is what "the same thing" means: which heartbeats are stale, which
    scheduled jobs are still failing, which defect classes rose.
    """
    stale = sorted(name for name, data in sig["principals"].items()
                   if (data["heartbeat"]["minutes_old"] or -1) > STALE_HEARTBEAT_MINUTES)
    payload = {
        "stale_heartbeats": stale,
        "cron_broken": sorted(row["job"] for row in sig["cron"] if row["broken"]),
        "selfheal_rose": sorted(
            f"{name}:{cls}" for name, data in sig["principals"].items()
            for cls in (data["selfheal"].get("rose") or [])),
    }
    return json.dumps(payload, sort_keys=True)


def _last_alarm(state_dir: Path) -> dict:
    path = _dedupe_path(state_dir)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _remember_alarm(state_dir: Path, *, condition: str, at: str) -> None:
    path = _dedupe_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"signature": condition, "spoken_at": at},
                               indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _cooling_off(last: dict, *, now: datetime) -> bool:
    """True while the last alarm is recent enough that saying it again is noise."""
    stamp = str(last.get("spoken_at") or "")
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when).total_seconds() < REALARM_HOURS * 3600


def _subject(sig: dict, verdict: MonitorVerdict, profiles: list[str] | None) -> str:
    """Whose ledger gets the row: the principal the verdict names, else the first one.

    One row per run, not three: a triage is a statement about the loop as a whole, and
    copying it into every principal's ledger would make "what did the monitor say about
    lj?" answerable with another principal's row.
    """
    names = list(sig["principals"]) or ["lj"]
    if profiles and len(profiles) == 1:
        return profiles[0]
    said = f"{verdict.headline} {verdict.detail}".lower()
    for name in names:
        if name in said:
            return name
    return names[0]


def run(*, profiles: list[str] | None = None, state_dir: Path | None = None,
        dry_run: bool = False, explain: bool = False, client=None) -> dict:
    """Collect, triage, and say only what is worth saying."""
    state_dir = state_dir or config.STATE_DIR
    sig = signals(profiles=profiles, state_dir=state_dir)
    verdict, facts = triage(sig, client=client)
    principal = _subject(sig, verdict, profiles)
    condition = signature(sig)
    now = datetime.fromisoformat(sig["at"])
    last = _last_alarm(state_dir)
    repeat = (verdict.verdict != "quiet"
              and last.get("signature") == condition
              and _cooling_off(last, now=now))
    speak = verdict.verdict != "quiet" and not repeat
    if not dry_run:
        record(sig, verdict, facts, state_dir=state_dir, principal=principal)
        if speak:
            _remember_alarm(state_dir, condition=condition, at=sig["at"])
    result = {"at": sig["at"], "verdict": verdict.verdict, "headline": verdict.headline,
              "detail": verdict.detail, "evidence": verdict.evidence,
              "spoke": speak, "repeat": repeat, "facts": facts if explain else ""}
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default="all")
    ap.add_argument("--dry-run", action="store_true",
                    help="do not write the triage ledger")
    ap.add_argument("--explain", action="store_true",
                    help="print the facts the model was given, then the verdict")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--collect-only", action="store_true",
                    help="print the collected signals and stop (no model call)")
    args = ap.parse_args()
    config.set_active_profile(args.profile)
    profiles = None if args.profile == "all" else [args.profile]

    if args.collect_only:
        print(json.dumps(signals(profiles=profiles), indent=2, sort_keys=True))
        return 0

    result = run(profiles=profiles, dry_run=args.dry_run, explain=args.explain)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.explain:
        print(result["facts"])
        print("\n--- verdict ---")
    if not result["spoke"]:
        print(f"[monitor] quiet ({result['verdict']})"
              + (" — already reported" if result["repeat"] else ""), file=sys.stderr)
        return 0
    print(f"[monitor] {result['verdict']}: {result['headline']}")
    if result["detail"]:
        print(result["detail"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
