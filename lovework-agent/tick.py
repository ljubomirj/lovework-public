#!/usr/bin/env python3
"""tick.py — one tick of the LoveWork loop, on this host.

`decide.py` answers *what is due*. This runs that answer: a cheap deterministic
tick, and — only when something is due and the agent owns it — ONE bounded
action, with a receipt. The doctrine is ch43; the charter is ch42.

    tick.py --profile all --mode status     # decide + one receipt line per principal
    tick.py --profile lj --mode act         # SLOT: drain the queue, draft only
    tick.py --profile lj --mode act --apply # …and write the effects
    tick.py --profile all --mode act --apply            # the cron form: all principals
    tick.py --profile lj --mode act --apply --post-crawl # the wrapper's after-seal pass
    tick.py --profile lj --max-actions 1 --apply        # the single-shot form

Three bounds, all enforced here rather than asked for in a prompt:

- **The slot, not a fixed quantum (2026-10-18).** LJ: *"'Tick hourly' is fine,
  '1 or X action per tick' … and then we go back to sleep do nothing with the
  time is not. Keep processing the queue … until the queue is empty, or until
  we are close to 1h slot expiring; if latter, stop and yield the time to the
  next tick. This is like scheduling the cpu by the os kernel scheduler."* The
  default run is therefore **slot mode** (`--max-actions 0`): drain the queue
  back-to-back — round-robin across principals, one action per principal per
  cycle — until nothing is due or the next hourly boundary approaches; then
  yield. A fixed count (`--max-actions N`, N ≥ 1) is still honoured for the
  single-shot form the tests and manual runs use.
- **A daily envelope.** Model calls and wall-clock minutes per principal per
  day, counted in `state/<p>/loop/envelope-<date>.json`. With the slot as the
  working bound the envelope is the **runaway net**, not the throttle; the
  conservative module defaults apply only when the wrapper sets no env.
- **Authority.** Only items the decider marks `agent` or `both` are actionable.
  A `principal` item is surfaced, never acted on.

**The post-crawl pass** (`--post-crawl`) is the same engine with a longer
clock and one guard relaxed: the crawl wrapper launches it *after* a sweep has
sealed, so the skip windows (which exist to keep the loop off the GPU the crawl
is using) no longer apply — the crawl is demonstrably done. The lock pair
(lease + busy processes) still guards every action, and a busy host is waited
out (bounded) rather than yielded to, because there is no "next tick" coming
behind a post-crawl run.

Writes stay in `state/<p>/loop/` — receipts, envelopes, drafts — and, with
`--apply`, in the pack the action owns. The tick deliberately does **not** write
the page cache (`use_cache=False`): `cache/` is inside the sealed tree, so a
tick artifact landing there would be captured by the next seal as if it were
crawl evidence.

Exit codes follow the house vocabulary: 0 the tick completed (whatever it
found), 1 the tick failed, 2 usage, 75 a writer holds the principal.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pydantic import BaseModel, Field

import config

#: The runner's own bound, used when the envelope variables are unset. A tick
#: every 15 minutes is 96 ticks a day; without a cap the loop could spend a day's
#: budget by lunchtime.
DEFAULT_DAILY_CALLS = int(os.getenv("LOVEWORK_LOOP_DAILY_CALLS", "40"))
DEFAULT_DAILY_MINUTES = int(os.getenv("LOVEWORK_LOOP_DAILY_MINUTES", "30"))

#: Fetches per action. The investigation needs the advert and, at most, one
#: company page; anything more is a crawl, and crawls are not a tick's job.
MAX_FETCHES_PER_ACTION = 2

#: The hosts the loop may *act* on. It ran on host-b first, to stay clear of
#: the production schedule; on 2026-10-11 LJ moved it to **host-a** — keeping two
#: hosts in step was more work than the interference it avoided. So the guard is
#: no longer "which host" (that is now the one host that owns the state) but
#: **which lock**: `lease_holder` and `busy_processes` below. Reading, auditing
#: and repairing are allowed anywhere; acting is the guarded half.
DEFAULT_TICK_HOSTS = os.getenv("LOVEWORK_TICK_HOSTS", "host-a")

#: Running work the tick must not act beside, whatever the clock says. The
#: windows catch the *scheduled* hours; this catches a job that overran them,
#: which is the case a time window cannot see. Long-lived services (the
#: dashboard, the inference server) are deliberately absent — they always run.
BUSY_MARKERS = (
    "crawl_operation.py",      # the sealed crawl wrapper
    "main.py",                 # the pipeline itself
    "incremental_crawl.py",
    "consume.py",              # the shared-source consume
    "brief.py",                # the 06:00/07:00 morning chain
    "watch.py",
    "selfheal.py",
    "link_check.py",
    "run_watchdog.py",
    "prepare_cases.py",
    "enrichment.py",
    "jnj_letter.py",
    "build_manual.py",
)

#: When not to tick. The inference endpoint is on host-a — the same box the
#: scheduled crawls use — so a tick during a sweep competes with it for the GPU.
#: The windows below are the known crawl hours (weekend sweeps Sat 09:00-20:00
#: LJ, Sun 09:00-18:00 VJ+KJ; the daily 06:00 and 07:00 jobs). Format:
#: `day:HH-HH`, where `day` is `daily`, `mon`…`sun`, or `weekday`/`weekend`.
#: The coarse guard, now that the tick runs on the production host: the hours the
#: schedule actually occupies, read from the Hermes cron store (2026-10-11) —
#: LJ's sweep Sat 08:00 (1.8–8.8 h) plus the evening watchdog and
#: meta-investigator (22:30–23:00); the consumes Sun 06:30/08:30 and the VJ/KJ
#: sweeps 10:00–17:00 with their watchdogs; and the daily 06:00 chain
#: (recheck → morning → brief → the pack job, which can run to ~09:00).
#: The *precise* guard is the lock pair below — a window cannot see a job that
#: overran, and a lock cannot see a job that has not started.
DEFAULT_SKIP_WINDOWS = os.getenv(
    "LOVEWORK_TICK_SKIP_WINDOWS", "sat:07-23,sun:06-20,daily:05-09")

#: The minute the hourly cron fires (lovework-tick at :05). In slot mode the
#: run's clock is "until the next tick", so the engine needs to know where the
#: next tick is: the next wall-clock occurrence of this minute, less the margin
#: below. An env, not a constant, so a cadence change is a wrapper edit and not
#: a code change.
SLOT_BOUNDARY_MINUTE = int(os.getenv("LOVEWORK_TICK_SLOT_MINUTE", "5"))

#: How many minutes before the boundary a slot stops acting. The next tick
#: should start with the run lock free and the receipts already written, so the
#: yield happens a little early rather than exactly on the wire.
SLOT_MARGIN_MINUTES = float(os.getenv("LOVEWORK_TICK_SLOT_MARGIN_MINUTES", "3"))

#: The post-crawl pass's clock (minutes). LJ, 2026-10-18: *"process all GO-s
#: after the full crawl and prepare the application packs"* — one pass per
#: sealed sweep, on the local model, long enough to work a whole report's GOs.
POST_CRAWL_MINUTES = float(os.getenv("LOVEWORK_TICK_POST_CRAWL_MINUTES", "300"))

#: How long a post-crawl pass waits out a busy host before giving up. The
#: Saturday-night jobs (watchdog 22:30, meta-investigator gate 22:35) are
#: minutes long and must not end a five-hour pass; the same signals that make
#: an hourly tick *yield* make a post-crawl pass *wait*, because nothing comes
#: behind it to resume the work.
POST_CRAWL_BUSY_WAIT_MINUTES = float(
    os.getenv("LOVEWORK_TICK_BUSY_WAIT_MINUTES", "10"))

#: The granularity of that wait. One sleep of this length per re-check.
BUSY_WAIT_STEP_SECONDS = float(os.getenv("LOVEWORK_TICK_BUSY_WAIT_STEP", "30"))
_DAY_ALIASES = {
    "daily": set(range(7)),
    "weekday": {0, 1, 2, 3, 4},
    "weekend": {5, 6},
    "mon": {0}, "tue": {1}, "wed": {2}, "thu": {3}, "fri": {4},
    "sat": {5}, "sun": {6},
}


class Investigation(BaseModel):
    """What the investigation must answer (docs/41 C8).

    **Every field is optional with a default, on purpose.** The LAN model is a
    strong reasoner with weaker JSON discipline than the relay model: asked for
    a strict object it omits fields, `model_validate_json` then raises, and
    `LLMClient.structured` retries — measured 2026-10-11 as 303 s and once
    4,631 s for a single investigation, all of it retries. A forgiving schema
    makes a partial answer a *valid* answer, which is what the doctrine wants
    anyway ("insufficient evidence" is an acceptable outcome): the gaps land in
    `unknown` instead of failing the call.
    """

    entity: str = Field(default="", description="legal entity, location, size")
    people: list[str] = Field(default_factory=list,
                              description="people named at the COMPANY — never "
                                          "the principal or the applicant")
    product: str = Field(default="", description="what the company sells")
    hiring_evidence: list[str] = Field(default_factory=list)
    funding: str = Field(default="", description="funding/runway, or what is unknown")
    collaborators: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list,
                             description="work-mode and work-auth risks first")
    unknown: list[str] = Field(default_factory=list,
                               description="what the evidence does not say")
    questions: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    confidence: str = Field(default="low", description="low | moderate | high")

LOOP_DIRNAME = "loop"


# ── the loop's own residue (never the sealed tree) ───────────────────────

def in_skip_window(when: datetime | None = None,
                   spec: str | None = None) -> str:
    """The window that forbids a tick now, or "" — deterministic and cheap.

    A tick during a sweep competes with the crawl for the GPU on host-a, which
    is the one resource the loop shares with the skeleton (ch43 §2). Skipping is
    a *deferral*, not a failure: the next tick picks the work up.
    """
    when = when or datetime.now()
    for entry in (spec if spec is not None else DEFAULT_SKIP_WINDOWS).split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        day, _, hours = entry.partition(":")
        try:
            start, _, end = hours.partition("-")
            start_h, end_h = int(start), int(end)
        except ValueError:
            continue                       # a malformed window is ignored
        if when.weekday() not in _DAY_ALIASES.get(day.strip().lower(), set()):
            continue
        if start_h <= when.hour < end_h:
            return entry
    return ""


def host_allows_acting(host: str | None = None, spec: str | None = None) -> bool:
    """May this host run an action? A host rule that is not code is a hope.

    The env is read at call time, not at import: a test (or a one-off run) must
    be able to say which host it is acting as without reloading the module.
    """
    if spec is None:
        spec = os.getenv("LOVEWORK_TICK_HOSTS", DEFAULT_TICK_HOSTS)
    allowed = {h.strip().lower() for h in spec.split(",") if h.strip()}
    if not allowed or "*" in allowed:
        return True
    name = (host or os.uname().nodename).lower()
    # Substring, not equality: macOS reports `host-b.local`, `MacBook2.lan` or
    # the bare name depending on how the machine was named, and a guard that
    # stops the loop on its own host because of a suffix is worse than no guard.
    return any(a in name for a in allowed)


# ── the two locks (2026-10-11: the tick shares a host with the schedule) ──

def lease_holder(state_root: Path) -> str:
    """Who holds the principal's writer lease, or "" — probed, never taken.

    The same `flock` `writer_boundary` takes for a crawl, a seal, a consume or
    the brief. Non-mutating on purpose: no `O_CREAT`, so a principal whose lock
    file does not exist is simply free, and the probe leaves no residue in
    `strata/` (ch42 §3.3 — the agent does not edit sealed ground).
    """
    lock = state_root / "strata" / "writer.lock"
    if not lock.exists():
        return ""
    try:
        import fcntl
    except ImportError:                      # not a Unix host: nothing to probe
        return ""
    try:
        fd = os.open(lock, os.O_RDWR)
    except OSError:
        return ""
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return f"a writer holds {state_root.name}"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return ""
    finally:
        os.close(fd)


class AnotherTickRunning(RuntimeError):
    """A tick holds the run lock — a second one would duplicate its work."""


@contextmanager
def tick_run_lock(state_dir: Path, *, wait: bool = False):
    """Hold `state/.writer-locks/tick.lock` for this tick's duration.

    One tick at a time, whatever the cadence: with `--max-actions` above one a
    run can last tens of minutes, and two overlapping runs would fetch and
    investigate the same pack — the *last* writer would win and the first would
    have spent GPU for nothing. The launcher ticks three principals
    sequentially, so a per-run lock also keeps the three of them out of each
    other's way. Non-blocking by default: a held lock is a deferral.
    """
    import fcntl

    lock_dir = Path(state_dir) / ".writer-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = os.open(lock_dir / "tick.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        flags = fcntl.LOCK_EX if wait else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(handle, flags)
        except OSError as exc:
            raise AnotherTickRunning(str(exc)) from exc
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def busy_processes(markers: tuple[str, ...] = BUSY_MARKERS,
                   proc_root: str = "/proc") -> str:
    """Running LoveWork work on this host, or "".

    Read from `/proc` directly rather than shelling out to `ps`: the same fact
    from one authority, and cheap enough to ask every 30 minutes. A host without
    `/proc` (macOS) returns "" — the windows and the lease still guard there.
    """
    if not os.path.isdir(proc_root):
        return ""
    mine = str(os.getpid())
    for entry in os.listdir(proc_root):
        if not entry.isdigit() or entry == mine:
            continue
        try:
            with open(os.path.join(proc_root, entry, "cmdline"), "rb") as handle:
                cmdline = handle.read().decode("utf-8", "replace")
        except OSError:
            continue                          # the process died mid-scan
        if not cmdline or "tick.py" in cmdline:
            continue
        for marker in markers:
            if marker in cmdline:
                return f"{marker} is running (pid {entry})"
    return ""


def loop_dir(state_root: Path) -> Path:
    return state_root / LOOP_DIRNAME


TICK_REPORT_NAME = "tick-report.md"


def _local_stamp(at: str | None) -> str:
    """`2026-10-21T09:05:12+00:00` → `2026-10-21 10:05:12 BST`; tolerant of junk."""
    from datetime import datetime as _dt

    try:
        moment = _dt.fromisoformat(str(at).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except (TypeError, ValueError):
        return str(at or "")


def _tick_report_section(receipt: dict) -> str:
    """One run as a reader needs it: what was due, what was done, with which model."""
    lines = [f"## {_local_stamp(receipt.get('at'))} · {receipt.get('mode') or 'act'}"
             f" · {receipt.get('outcome') or 'unknown'}"]
    due = receipt.get("due")
    if due is not None:
        extra = []
        if receipt.get("needs_principal") is not None:
            extra.append(f"needs principal: {receipt['needs_principal']}")
        if isinstance(receipt.get("maintenance"), dict):
            extra.append("maintenance ran")
        lines.append(f"- **due at start**: {due}"
                     + (f" ({', '.join(extra)})" if extra else ""))
    counts = []
    if receipt.get("actions") is not None:
        counts.append(f"{receipt['actions']} action(s)")
    if receipt.get("calls") is not None:
        counts.append(f"{receipt['calls']} model call(s)")
    if receipt.get("seconds") is not None:
        counts.append(f"{float(receipt['seconds']):.1f}s")
    elif receipt.get("minutes") is not None:
        counts.append(f"{receipt['minutes']} min")
    if counts:
        lines.append("- **this run**: " + " · ".join(counts))
    if receipt.get("packs"):
        lines.append("- **packs**: " + ", ".join(str(p) for p in receipt["packs"][:6]))
    if receipt.get("action"):
        lines.append(f"- **last action**: `{receipt['action']}`"
                     + (f" → {receipt['effect']}" if receipt.get("effect") else ""))
    if receipt.get("blocked"):
        lines.append("- **blocked**: " + ", ".join(receipt["blocked"][:6]))
    if receipt.get("why"):
        lines.append(f"- **why**: {receipt['why']}")
    if receipt.get("stopped_because"):
        lines.append(f"- **stopped**: {receipt['stopped_because']}")
    try:
        settings = config.llm_settings(receipt.get("profile"))
        lines.append(f"- **model**: {settings.describe()}")
    except Exception:      # a report must never fail a tick
        pass
    return "\n".join(lines) + "\n"


def _append_tick_report(state_root: Path, receipt: dict) -> None:
    """Append this run to the principal's **continual tick report** (2026-10-21).

    The crawl produces one report per sweep (`wiki/reports/…-full-report.md`); the
    loop is continuous, so its report is a **day file every run appends to**:
    `state/<p>/loop/tick-report.md`. When the day changes, the old file moves to
    `loop/reports/tick-<date>.md` — the same "read the newest, archive the rest"
    shape as the crawl reports, and nothing is deleted. Written from the *receipt*,
    so every path out of a tick (acted, quiet, deferred, blocked, failed) leaves a
    line, and the model line names the profile file that would serve it — the first
    thing to check when a run behaves differently from yesterday.

    Best effort: a report that cannot be written must not fail the loop.
    """
    profile = receipt.get("profile")
    if not profile:
        return
    loop = loop_dir(state_root)
    path = loop / TICK_REPORT_NAME
    today = _local_stamp(receipt.get("at"))[:10] or "unknown"
    try:
        loop.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            first = path.read_text(encoding="utf-8").splitlines() or [""]
            if first and today not in first[0]:
                archive = loop / "reports"
                archive.mkdir(parents=True, exist_ok=True)
                stamp = first[0].rsplit("—", 1)[-1].strip() or "previous"
                path.replace(archive / f"tick-{stamp}.md")
        if not path.is_file():
            path.write_text(
                f"# LoveWork tick report — {profile} — {today}\n\n"
                "The loop's own record: one section per run, newest last. Append-only for\n"
                "the day; the previous day is archived beside it in `reports/`. Written by\n"
                "`tick.py` on every path out (acted, quiet, deferred, blocked, failed).\n\n"
                "Where to look, and what the other loop files hold: docs/50-where-to-look.md\n",
                encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n" + _tick_report_section(receipt))
    except OSError:
        pass          # a report that cannot be written must not fail a tick


def _is_provider_outage(exc: BaseException) -> bool:
    """Is this the **provider's** problem, not ours? (2026-10-21)

    The relay answered 503 "Upstream request failed: Endpoint is unavailable" to
    three attempts in three seconds at 09:20 while vj and kj acted in the same run.
    The tick recorded lj as `failed`, and the wrapper alarmed — but a provider blip
    is a **deferral**: the work is still due next hour, nothing was lost, and a
    permanent alarm per blip teaches the principal to ignore the channel.

    Server-side statuses and connection failures defer; a 4xx (bad request, bad
    key, a bug in our payload) still fails, because the next hour will not fix it.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status >= 500 or status == 429):
        return True
    return type(exc).__name__ in (
        "APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout",
        "ReadTimeout", "RemoteProtocolError", "Timeout", "TimeoutError",
        "ConnectionError", "ConnectionResetError",
    )


def _write_heartbeat(state_root: Path, receipt: dict, *, report: bool = True) -> None:
    """**Did the tick run?** — a question the receipts cannot answer.

    The receipts are written only when a tick *acts*, so a deferring tick leaves no
    trace at all: on 2026-10-17 a watchdog outside Hermes read `ledger-scan.json` —
    a cache-invalidation marker, rewritten only when the model-call ledger changes —
    as a heartbeat, and reported vj and kj as stopped for six days when they were
    deferring by design (the Saturday sweep window).

    So every tick writes this, on every path out: acted, quiet, deferred, blocked or
    failed. It answers "is the loop alive?", and it carries *why*, so the answer is
    not merely a timestamp.

    **And it appends a history** (2026-10-18). The single file keeps only the *latest*
    state, which answers "is it alive now?" and cannot answer "why was Friday quiet?" —
    LJ asked exactly that when the loop had produced no receipt since 2026-10-16 while
    the heartbeats looked fresh. An overwriting heartbeat is a real artefact answering
    the question it was built for (aliveness) and not the one being asked (history).
    The history is append-only and small: one row per tick, newest last.

    `report=False` is for the **mid-run** heartbeat: the slot writes one after
    every action (so "did it act recently?" has an answer during a long run), and
    feeding those into the continual tick report produced six sections for one run,
    all carrying the same start time and a stale `quiet` outcome (measured
    2026-10-21, lj's 10:05 run). Only the run's **final** heartbeat appends a
    report section.
    """
    path = loop_dir(state_root) / "heartbeat.json"
    row = {
        "at": receipt.get("at"),
        "profile": receipt.get("profile"),
        "mode": receipt.get("mode"),
        "outcome": receipt.get("outcome"),
        "why": receipt.get("why"),
        "due": receipt.get("due"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with (loop_dir(state_root) / "heartbeat.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass          # a history that cannot be written must not fail a tick
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "at": receipt.get("at"),
            "profile": receipt.get("profile"),
            "mode": receipt.get("mode"),
            "outcome": receipt.get("outcome"),
            "why": receipt.get("why"),
            "due": receipt.get("due"),
        }, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass          # a heartbeat that cannot be written must not fail a tick
    if report:
        _append_tick_report(state_root, receipt)


def _envelope_path(state_root: Path, today: date) -> Path:
    return loop_dir(state_root) / f"envelope-{today.isoformat()}.json"


def read_envelope(state_root: Path, today: date) -> dict:
    path = _envelope_path(state_root, today)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Spent, not reset — which is what this comment always claimed and
            # what returning zeros did the opposite of: a torn envelope would
            # have handed the loop a fresh day's budget.
            return {"date": today.isoformat(), "calls": DEFAULT_DAILY_CALLS,
                    "minutes": float(DEFAULT_DAILY_MINUTES), "ticks": 0,
                    "corrupt": True}
    return {"date": today.isoformat(), "calls": 0, "minutes": 0.0, "ticks": 0}


def spend(state_root: Path, today: date, *, calls: int = 0,
          minutes: float = 0.0) -> dict:
    """Add to today's spend and persist it. Returns the new envelope."""
    envelope = read_envelope(state_root, today)
    envelope["calls"] = int(envelope.get("calls", 0)) + calls
    envelope["minutes"] = round(float(envelope.get("minutes", 0.0)) + minutes, 2)
    envelope["ticks"] = int(envelope.get("ticks", 0)) + 1
    path = _envelope_path(state_root, today)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    return envelope


#: The most any single action may run, in minutes. A pack costs 3-6 minutes on the
#: local model (measured 2026-10-19), so 10 is generous for the work and short enough
#: that six of them fit a 40-minute slot; `LOVEWORK_ACTION_MINUTES=0` disables the
#: ceiling for a deliberate long job.
ACTION_MINUTES_ENV = "LOVEWORK_ACTION_MINUTES"
DEFAULT_ACTION_MINUTES = 10.0


def action_deadline(run_deadline: float | None, minutes_left: float,
                    *, now: float | None = None) -> float:
    """The hard stop for **one** action: the envelope, the run, and the ceiling.

    A pure function on purpose — the three bounds are the whole rule, and a rule
    about time that can only be tested by waiting is a rule nobody tests.
    """
    now = time.monotonic() if now is None else now
    deadline = now + max(1.0, minutes_left * 60)
    if run_deadline:
        deadline = min(deadline, run_deadline)
    try:
        ceiling = float(os.getenv(ACTION_MINUTES_ENV, DEFAULT_ACTION_MINUTES))
    except ValueError:
        ceiling = DEFAULT_ACTION_MINUTES
    if ceiling > 0:
        deadline = min(deadline, now + ceiling * 60)
    return deadline


def budget_left(state_root: Path, today: date) -> dict:
    envelope = read_envelope(state_root, today)
    calls_left = DEFAULT_DAILY_CALLS - int(envelope.get("calls", 0))
    minutes_left = DEFAULT_DAILY_MINUTES - float(envelope.get("minutes", 0.0))
    return {"calls_left": calls_left, "minutes_left": round(minutes_left, 2),
            "exhausted": calls_left <= 0 or minutes_left <= 0,
            "bound": ("env" if os.getenv("LOVEWORK_LOOP_DAILY_CALLS")
                      or os.getenv("LOVEWORK_LOOP_DAILY_MINUTES") else "default")}


def append_receipt(state_root: Path, receipt: dict) -> Path:
    """One row per *acted* tick. A read-only tick writes nothing: ch27 P6 says
    read-only calls need no audit debris, consequential effects do."""
    path = loop_dir(state_root) / "receipts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, sort_keys=True) + "\n")
    return path


# ── the model ────────────────────────────────────────────────────────────

#: The investigation's token ceiling and the client's patience. Measured
#: 2026-10-11 against the LAN endpoint: a full investigation is ~3,000
#: completion tokens (the model's reasoning is billed inside `max_tokens`, the
#: same trap as deepseek-v4.1-flash), 48 s at default effort, and 19 s at
#: `reasoning_effort="low"` — but "low" also broke the JSON discipline and the
#: call came back as prose. Default effort, generous ceiling, one attempt.
INVESTIGATION_MAX_TOKENS = int(os.getenv("LOVEWORK_TICK_MAX_TOKENS", "16000"))


#: The loop's model **is the crawl's model** (LJ, 2026-10-21): *"for the llm
#: calls, instead of localllm on host-a:8081, use OpenCode Go opencode-go
#: subscription … model deepseek-v4.1-flash with thinking or reasoning setting
#: to max."* Nothing is bound to the LAN endpoint any more; `model_client()`
#: takes its key, base URL, model and effort from `config`, so the loop and the
#: crawl cannot drift apart again. The longer timeout is the loop's only
#: difference: an investigation is a bigger call than a match.
TICK_LLM_TIMEOUT_SECONDS = int(os.getenv("LOVEWORK_TICK_LLM_TIMEOUT", "600"))


def model_client():
    """A client on the configured relay — the same one the crawl uses.

    The timeout is set before `llm_client` is imported because that module reads
    it once, at import. The crawl keeps its own (shorter) timeout.
    """
    os.environ.setdefault("LOVEWORK_LLM_TIMEOUT", str(TICK_LLM_TIMEOUT_SECONDS))
    from llm_client import LLMClient

    return LLMClient()


def model_reachable(timeout: float = 10.0) -> tuple[bool, str]:
    """Is the relay answering with our credential? One cheap GET, no model call.

    The old gate asked whether the **LAN endpoint was idle**; a subscription
    relay has no idle state, but it can be unreachable (network, expired key,
    402 quota). A draining run still owes its caller one honest deferral instead
    of a queue's worth of failed actions, each spending envelope and writing a
    failed receipt (the tick's own doctrine: a failed attempt is spent, or a
    retry loop runs unbounded).
    """
    import urllib.error
    import urllib.request

    url = config.LLM_BASE_URL.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {config.LLM_API_KEY or ''}",
        # The relay fronts the API with a WAF that 403s Python's default
        # `Python-urllib/3.x` agent while answering curl and browser agents
        # (measured 2026-10-21: identical request, urllib 403, curl 200).
        "User-Agent": "lovework/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            response.read(256)
        return (200 <= int(status) < 300), f"HTTP {status} from {url}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} from {url}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ── one action: investigate a prepared pack ──────────────────────────────

#: The gate `application_packs.uninvestigated_packs` reads: present means the
#: pack's investigation block is still empty.
NOT_INVESTIGATED_MARKER = "Status: NOT YET INVESTIGATED"

#: How long a pack whose advert search failed is left alone before the loop
#: tries again. A search costs a provider call; a LinkedIn-only advert will not
#: become findable next hour, and nine of them per tick is not "cycling over the
#: tasks", it is a metronome. Failed attempts are remembered per pack.
ADVERT_SEARCH_TTL_DAYS = float(os.getenv("LOVEWORK_ADVERT_SEARCH_TTL_DAYS", "7"))

#: How long a draft counts as this action's progress. Without this, the pack
#: whose block is still empty is picked again on every tick: measured
#: 2026-10-11, lj investigated `z0.ai` three times in 65 minutes and the daily
#: envelope was spent re-doing one pack while the corpus stood still. The draft
#: is the residue of a draft-only tick, so it is the marker that stops the
#: repeat — the dossier marker only moves with `--apply`.
DRAFT_FRESH_DAYS = int(os.getenv("LOVEWORK_TICK_DRAFT_FRESH_DAYS", "14"))


def draft_path(state_root: Path, pack_dir: Path) -> Path:
    """Where this pack's investigation draft lives (the action's own residue)."""
    return loop_dir(state_root) / "drafts" / f"{pack_dir.name}.md"


def action_can_do(item: dict, state_root: Path, today: date, *, apply: bool) -> bool:
    """Can the action this item names do work *right now*?

    The decider names candidates (what is due and worth doing); the runner knows
    what it has already done. A pack whose draft is fresh is finished work in
    drafts-only mode, so a multi-action run must skip it and move on — without
    this, the run stopped after one action with `last action: needs_principal`
    (measured 2026-10-13). With `--apply` a fresh draft whose block is still
    empty is *not* finished: it is the catch-up case, and it is doable.
    """
    if item.get("action") == "review_go":
        # A GO re-read recently is finished work: its note is the principal's, and
        # a second pass over unchanged evidence could only repeat the first. The
        # decider already leaves these out; asking again here is what stops a
        # *stale* decision (read a moment before the last action) from redoing it.
        if review_is_fresh(state_root, Path(item["target"]), today):
            return False
        # …and a pack with no advert link is a *permanent* condition: there is
        # nothing to fetch, so nothing to re-read. Skipping it here is what stops
        # it becoming the head of the queue forever — a `blocked` action ends a
        # single-action run, so every later GO would wait behind it (the rule the
        # pack actions already follow, measured for them 2026-10-13). The
        # decider leaves these out too; this is the runner's own check.
        #
        # A **re-post link** is the same class (2026-11-04): `review_go` refuses to
        # score a board page, so a stale decision naming one costs a fetch and
        # blocks. Its work is `resolve_advert`.
        from application_packs import advert_link_is_board

        advert = pack_advert_url(item["target"])
        return bool(advert) and not advert_link_is_board(advert)
    if item.get("action") == "resolve_advert":
        # Only a pack with no link is work; a resolved pack is completable and
        # must not be re-searched (the decider names the two separately). A pack
        # whose search failed is cooled down, not retried every hour.
        if not item.get("target"):
            return False
        pack = Path(item["target"])
        return bool(apply) and _needs_advert_link(pack) \
            and not _advert_searched_recently(state_root, pack, today)
    if item.get("action") not in ("investigate_pack", "complete_pack") \
            or not item.get("target"):
        return True
    pack_dir = Path(item["target"])
    facts = _pack_facts(pack_dir)
    if not facts["url"]:
        # No advert link is a *permanent* condition: nothing to review or verify
        # against, so this is not work the runner can do — and if it were
        # attempted, a `blocked` action would end the run and every later pack
        # would wait behind a pack nobody can complete.
        return False
    if item["action"] == "complete_pack":
        # Completion continues past the investigation, so a fresh draft is not
        # "done": the queue decides, and a pack that is complete is not in it.
        # But without `--apply` a completion writes nothing, so it cannot advance
        # a pack — and a run that keeps picking the same one is the livelock this
        # project has already paid for twice (measured here: four dry-run
        # completions of one pack in a single run).
        return bool(apply)
    if not draft_is_fresh(state_root, pack_dir, today):
        return True
    if not apply:
        return False
    return NOT_INVESTIGATED_MARKER in (facts["text"] or "")


def draft_is_fresh(state_root: Path, pack_dir: Path, today: date) -> bool:
    """Has this pack been drafted recently enough to leave alone?"""
    path = draft_path(state_root, pack_dir)
    if not path.is_file():
        return False
    age = (today - date.fromtimestamp(path.stat().st_mtime)).days
    return age <= DRAFT_FRESH_DAYS


# ── the review ledger: one row per GO re-read ────────────────────────────
#
# LJ, 2026-10-18: *"after that - to be reviewing existing GOs and refining them,
# WE want high quality, high SNR."* Every GO has a pack, so the loop's pack work
# is exhausted; what is left is the **quality** of what is on file. The unit of
# that work is one advert: fetch it, re-score it against the advert itself, and
# say what moved.
#
# The ledger is the loop's own residue (like `drafts/`), never a second score
# store: the scores themselves stay where they always were — the pack's dossier
# holds the original judgement, and this holds *when we last re-read it and what
# we saw*. `reviews.jsonl` is what makes "oldest reviewed first, fewest reviews
# first" answerable without re-reading a single pack.

#: How long a review counts as this action's progress. Without it the loop picks
#: the same GO every tick — the residue-repeats-itself class this loop has paid
#: for twice (docs/43 §9 defects 2 and the 2026-10-13 livelock).
REVIEW_FRESH_DAYS = int(os.getenv("LOVEWORK_REVIEW_FRESH_DAYS", "14"))

#: What counts as a **material** move in the score. The measurement behind the
#: number is in `calibration.py`: on the same 58 VJ adverts, same profile, same
#: prompt, two relay models disagree advert by advert with a spread of ±1.9 even
#: after the level offset is applied (decision agreement 0.65-0.72). A move
#: smaller than that is not distinguishable from the model's own noise, so the
#: threshold sits just above it — and a **tier** change is material whatever the
#: delta, because the tier is the decision-relevant unit ("is it still a GO?").
REVIEW_MATERIAL_DELTA = float(os.getenv("LOVEWORK_REVIEW_MATERIAL_DELTA", "2.0"))


def review_log_path(state_root: Path) -> Path:
    """The per-advert review ledger (the loop's own residue, like `drafts/`)."""
    return loop_dir(state_root) / "reviews.jsonl"


def read_reviews(state_root: Path) -> dict:
    """Fold `loop/reviews.jsonl` into one row per pack, newest row winning.

    The decider reads this to rank the queue (a never-reviewed GO first, then the
    oldest, then the fewest reviews) and the action reads it to know whether the
    advert it just fetched is the one the last review already scored. One reader,
    so the ranking and the action cannot disagree about what was reviewed.

    A line that will not parse is skipped, not fatal: this is the loop's own
    bookkeeping, and a torn tail must not stop the loop from acting.
    """
    path = review_log_path(state_root)
    out: dict = {}
    if not path.is_file():
        return out
    for line in physical_lines(path):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        pack = str(row.get("pack") or "")
        if not pack:
            continue
        prior = out.get(pack)
        row = dict(row)
        row["count"] = int((prior or {}).get("count", 0)) + 1
        out[pack] = row
    return out


def record_review(state_root: Path, row: dict) -> Path:
    """Append one review row. Append-only, and the loop's own residue."""
    path = review_log_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return path


def review_is_fresh(state_root: Path, pack_dir: Path, today: date,
                    reviews: dict | None = None) -> bool:
    """Has this GO been re-read recently enough to leave alone?

    `reviews` is the folded ledger when the caller already holds it: the decider
    reads it once to rank the whole queue, and asking per candidate would be one
    file read per GO for the same fact.
    """
    row = (read_reviews(state_root) if reviews is None else reviews).get(pack_dir.name)
    if not row:
        return False
    # The stamp is UTC, `today` is local: read the stamp through the one helper that
    # knows the difference (2026-10-20 — slicing `[:10]` off a UTC stamp made a review
     # written at 00:30 local read as *yesterday*, and three tests disagreed with it).
    from run_ledger import local_date_of

    when = local_date_of(str(row.get("at") or ""))
    if when is None:
        return False
    return (today - when).days <= REVIEW_FRESH_DAYS


def review_draft_path(state_root: Path, pack_dir: Path) -> Path:
    """The review note's draft — the action's residue when `--apply` is off.

    In `loop/drafts/`, beside the investigation draft and for the same reason: the
    residue belongs to the loop, not to the sealed tree. The `.review.md` suffix
    keeps it distinct from `draft_path` (the investigation's), because the two are
    different work on the same pack and the freshness of one is not the freshness
    of the other.
    """
    return loop_dir(state_root) / "drafts" / f"{pack_dir.name}.review.md"


def review_note_path(pack_dir: Path) -> Path:
    """The review note **in the pack** — the reader's copy (docs/43 §7)."""
    return Path(pack_dir) / "review.md"


class PackQueueUnavailable(RuntimeError):
    """The authority that says a pack is still worth doing could not be read.

    Named rather than swallowed: the previous `except Exception: queued = set()`
    made an unreadable queue *widen* the action set (the `or pending` fallback),
    so a broken dependency silently licensed work the queue would have refused.
    """


def pending_packs(state_root: Path, *, today: date | None = None) -> list[Path]:
    """The packs the queue says are still worth doing, newest first."""
    import pack_queue

    rows = pack_queue.queue(state_dir=state_root.parent, profile=state_root.name,
                            today=today)
    return [Path(row["pack"]) for row in rows]


def _first_uninvestigated_pack(state_root: Path, *,
                               today: date | None = None) -> Path | None:
    """The newest pack whose investigation block is empty and undrafted.

    Both authorities are reused rather than re-derived: `uninvestigated_packs`
    owns "is the block filled" and `pack_queue` owns "is this still worth
    doing". `PackQueueUnavailable` propagates — the caller defers rather than
    guessing.
    """
    from application_packs import uninvestigated_packs

    today = today or date.today()
    apps = state_root / "applications"
    # `uninvestigated_packs` yields the dossier `.txt` paths; `pack_queue`
    # yields pack *directories*. Normalise to directories here, once — the
    # mismatch silently emptied every intersection (2026-10-11).
    pending = [txt.parent for txt in uninvestigated_packs(apps)]
    if not pending:
        return None
    try:
        queued = set(pending_packs(state_root, today=today))
    except Exception as exc:
        raise PackQueueUnavailable(f"{type(exc).__name__}: {exc}") from exc
    pool = [p for p in pending if p in queued] or pending
    # A pack drafted inside the freshness window is not work: the draft is the
    # residue, and its reader is the principal.
    fresh = [p for p in pool if not draft_is_fresh(state_root, p, today)]
    if not fresh:
        return None
    return sorted(fresh, key=lambda p: p.name)[-1]


def _default_fetcher():
    """The repo's provider chain (`exa,http`), used with the cache *off*: the
    cache lives inside the sealed tree, and a tick artifact landing there would
    be captured by the next seal as if it were crawl evidence."""
    from crawler import fetch_page

    return lambda url, use_cache=True: fetch_page(url, use_cache=False)


#: Hosts whose front page is a marketplace, not an employer. Fetching it spends
#: half an action's fetch budget on an index page and hands the model noise:
#: measured 2026-10-11, `apply.workable.com/` returned 1 char and
#: `linkedin.com/` 5,001 chars of job-board navigation, while the advert itself
#: said everything the investigation needed.
_AGGREGATOR_MARKERS = (
    "workable.com", "greenhouse.io", "lever.co", "ashbyhq.com", "workday",
    "smartrecruiters.com", "linkedin.com", "indeed.", "totaljobs.com",
    "cwjobs.", "digitaljobs.com", "wellfound.com", "otta.com",
    "workinstartups.com", "jobs.ashbyhq", "apply.",
)


def _needs_advert_link(pack: Path) -> bool:
    """No link at all, or only a re-post's link — either way the advert is missing."""
    from application_packs import advert_link_is_board

    url = pack_advert_url(pack)
    return (not url) or advert_link_is_board(url)


#: An advert shorter than this is not an advert. Real ones here run 2,000-9,000
#: characters; a board stub or a tracking redirect returns a few hundred
#: (measured 2026-11-03: 873 characters of a LinkedIn job-alert page). Under the
#: floor a review refuses to score and the pack retries next tick, rather than a
#: pack being closed on a page nobody read.
MIN_ADVERT_CHARS = int(os.getenv("LOVEWORK_MIN_ADVERT_CHARS", "1000"))


def _site_root(url: str) -> str:
    """The company's front page for an advert URL (`…/careers/role` → `…/`).

    Empty for an advert hosted by an applicant-tracking system or a board: the
    site root there is the *board*, so the honest answer is "no second page to
    fetch" rather than a wasted fetch of someone else's index.
    """
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    host = parts.netloc.lower()
    if any(marker in host for marker in _AGGREGATOR_MARKERS):
        return ""
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def _pack_parts(pack_dir: Path) -> tuple[str, str]:
    """Organisation and role from a pack directory's own name.

    Delegates to `application_packs.pack_parts` — one authority for the name, so
    a decision and the action it names cannot disagree about which pack it is.
    """
    from application_packs import pack_parts

    return pack_parts(pack_dir)


def _pack_facts(pack_dir: Path) -> dict:
    """Org, role, advert link and advert text from the pack's own files."""
    # The dossier is named after the pack directory (AGENTS.md §3), so ask for
    # it by name: `next(glob("*.txt"))` picks arbitrarily when a pack holds
    # more than one text file.
    from application_packs import advert_url, dossier_path, markdown_link_target

    dossier = dossier_path(pack_dir)
    markdown = pack_dir / "application_pack.md"
    text = dossier.read_text(encoding="utf-8", errors="ignore") if dossier else ""
    org, role = _pack_parts(pack_dir)
    facts = {"pack": pack_dir, "dossier": dossier, "text": text,
             "org": org, "role": role, "url": ""}
    if markdown.is_file():
        md = markdown.read_text(encoding="utf-8", errors="ignore")
        head = md.splitlines()[0] if md.splitlines() else ""
        if " — " in head:
            facts["org"], facts["role"] = [part.strip() for part in
                                           head.lstrip("# ").split(" — ", 1)]
        import re
        # The whole cell, not its first token: `_not retained_` (the pack
        # writer's own placeholder) was read as the URL `_not`, and the tick then
        # asked the fetcher for "_not" — a wasted attempt and a library error line
        # in the delivered output (2026-10-11). The cell is bare in a `.txt`-era
        # front page and a markdown link in a converted one, and the predicate
        # that unwraps both and refuses a placeholder is
        # `application_packs.advert_url` — the same one the dossier's readers use.
        link = re.search(r"^- \*\*Link\*\*: (.+)$", md, re.M)
        if link:
            facts["url_raw"] = markdown_link_target(link.group(1)).strip("<>")
            facts["url"] = advert_url(facts["url_raw"])
    return facts


def pack_advert_url(pack_dir: Path) -> str:
    """The advert link a pack carries, or "" — **can this pack be fetched?**

    One reader, because two callers ask it and both must answer the same: the
    decider (which must not name work that cannot be done) and the runner's
    `action_can_do` (which must skip it rather than let a `blocked` action end the
    run). A pack with no advert link is a *permanent* condition — its receipt
    would be identical every tick, and every later GO would wait behind it (the
    rule the pack actions already follow, measured for them 2026-10-13).
    """
    from application_packs import pack_advert_url as _shared

    return _shared(Path(pack_dir))


def outer_run_id(run_id: str):
    """The scoped run-id context (`pack_completion.outer_run_id`).

    Kept as a name here because the investigation action uses it; the
    implementation lives with the model calls, where the reason for scoping is
    written down (a leaked id makes subprocesses skip the writer lease).
    """
    from pack_completion import outer_run_id as _impl

    return _impl(run_id)


def _advert_search_cache(state_root: Path) -> dict:
    path = Path(state_root) / "cache" / "advert_search.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _advert_searched_recently(state_root: Path, pack: Path, today: date) -> bool:
    stamp = _advert_search_cache(state_root).get(str(Path(pack).resolve()), "")
    try:
        when = date.fromisoformat(str(stamp)[:10])
    except ValueError:
        return False
    return (today - when).days < ADVERT_SEARCH_TTL_DAYS


def _remember_advert_search(state_root: Path, pack: Path, today: date) -> None:
    path = Path(state_root) / "cache" / "advert_search.json"
    cache = _advert_search_cache(state_root)
    cache[str(Path(pack).resolve())] = today.isoformat()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass          # a cache that cannot be written must not fail the action


def resolve_advert(state_root: Path, *, target=None, apply: bool, client=None,
                   fetcher=None, today=None, run_id=None, **_) -> dict:
    """Find the employer's own advert for a pack that has no link (2026-10-23).

    LJ, on a tick that reported *"item(s) the agent owns are not doable now …
    no advert link"*: *"if there is work to be done on the leads and applications
    — this is the time to do it. We are tick-ng lovework, we are cycling over all
    tasks."* A pack whose advert link is missing was a **permanent** block; with
    Tavily search it is ordinary work: search, take the employer's own advert
    (never a board or mirror), write it into the pack's front page, and the next
    decision sees a completable pack.

    Idempotent: a pack that already has a link is not picked (the decider and
    `action_can_do` both refuse it), and a second call reports it.
    """
    from application_packs import pack_parts, set_advert_link

    if not target:
        return {"outcome": "blocked", "why": "resolve_advert needs a pack target"}
    pack = Path(target)
    if not pack.is_dir():
        return {"outcome": "blocked", "why": f"the pack is not there: {pack}"}
    previous = pack_advert_url(pack)
    if previous and not _needs_advert_link(pack):
        return {"outcome": "acted", "effect": "the pack already has an advert link"}
    if not apply:
        return {"outcome": "blocked", "why": "resolve_advert writes; run with --apply"}
    org, role = pack_parts(pack)
    from web_search import find_original_advert

    match = find_original_advert(org, role)
    url = getattr(match, "advert_url", "") or ""
    if not url:
        _remember_advert_search(state_root, pack, today or date.today())
        return {"outcome": "blocked",
                "why": f"no original advert found for {org} — {role} "
                       f"(provider {getattr(match, 'provider', '')!r}); "
                       f"not retried for {ADVERT_SEARCH_TTL_DAYS:g} days"}
    if not set_advert_link(pack, url):
        return {"outcome": "blocked",
                "why": f"the pack has no application_pack.md to write the link into: {pack}"}
    # `acted` must carry what backs it, or the audit rightly calls it
    # evidence-free — this action was the whole of the remaining defect list
    # (22 rows, 2026-11-03), because it recorded neither a fetch nor a
    # provenance. Two honest shapes, never silence:
    #   * the advert page fetched now → that fetch is the evidence;
    #   * the page could not be fetched → the search hit that produced the link
    #     is the provenance, said plainly.
    provenance = (f"search: {getattr(match, 'provider', '') or 'unknown provider'} "
                  f"rank {getattr(match, 'rank', -1)} for {org!r} / {role!r}")
    fetches: list[dict] = []
    evidence_source = provenance
    try:
        text, provider = (fetcher or _default_fetcher())(url, use_cache=False)
    except Exception as exc:                 # a failed confirmation is not a failure
        text, provider = "", f"error: {type(exc).__name__}"
    if text:
        fetches.append({"url": url, "provider": provider, "chars": len(text)})
        evidence_source = url
    else:
        evidence_source = (f"{provenance}; the advert page itself was not fetched "
                           f"({provider or 'no provider'})")
    result = {"outcome": "acted", "effect": f"advert link resolved: {url}",
              "fetches": fetches, "evidence_source": evidence_source}
    if previous:
        # The pack pointed at a re-post; say which link this replaced, so the
        # receipt explains a URL changing under a pack that already had one.
        result["replaced_board_link"] = previous
    return result


def investigate_pack(state_root: Path, *, target=None, apply: bool, client,
                     deadline: float, fetcher=None,
                     today: date | None = None,
                     run_id: str = "") -> dict:
    """Fetch what the advert and the company say, ask the local model for the
    six-axis investigation, and draft it (or write it with `--apply`).

    `target` is the pack the *decision* named, and it is the target that is
    worked. Choosing a pack here instead was the 2026-10-11 defect: the receipt
    named the first due item's decision while the action investigated a
    different pack, so the loop's verification record was false for every tick
    it ran. Without a target (a direct invocation, a test) the newest undrafted
    pending pack is used.
    """
    today = today or date.today()
    if target is not None:
        pack_dir = Path(target)
        if not pack_dir.is_dir():
            return {"outcome": "blocked",
                    "why": f"the decision's pack is not there: {pack_dir}"}
        if draft_is_fresh(state_root, pack_dir, today):
            draft_file = draft_path(state_root, pack_dir)
            facts = _pack_facts(pack_dir)
            # A draft whose block is still empty is the drafts-only era's
            # residue (or a run whose --apply was off): the investigation exists
            # and was simply never published into the pack. Fill it from the
            # draft on disk — **no model call** — and say so in the receipt.
            if apply and NOT_INVESTIGATED_MARKER in (facts["text"] or ""):
                applied = _apply_dossier(state_root, facts,
                                         draft_file.read_text(encoding="utf-8"))
                if applied.get("contended"):
                    return {"outcome": "deferred", "contended": True,
                            "why": "writer holds the principal",
                            "pack": str(pack_dir), "effect": "draft only",
                            "verification": str(draft_file)}
                return {"outcome": "acted", "action": "investigate_pack",
                        "calls": 0, "fetches": [], "confidence": "",
                        "unknown": [], "pack": str(pack_dir),
                        # The evidence is the draft itself: this path publishes an
                        # investigation that already happened, so it fetches
                        # nothing and calls nothing, and the audit must be able to
                        # tell that from "acted with no evidence at all".
                        "evidence_source": str(draft_file),
                        "effect": applied["effect"] + " (from the existing draft)",
                        "verification": str(draft_file)}
            return {"outcome": "needs_principal",
                    "why": "already drafted; the draft is the principal's to read",
                    "pack": str(pack_dir),
                    "verification": str(draft_file)}
    else:
        try:
            pack_dir = _first_uninvestigated_pack(state_root, today=today)
        except PackQueueUnavailable as exc:
            return {"outcome": "deferred",
                    "why": f"pack queue unreadable, so what is worth doing is "
                           f"unknown: {exc}"}
        if pack_dir is None:
            return {"outcome": "needs_principal",
                    "why": "no undrafted pack is pending: the drafts are the "
                           "principal's to read"}
    facts = _pack_facts(pack_dir)
    if not facts["url"]:
        raw = facts.get("url_raw") or ""
        # No advert link is nothing to verify against, and a model asked anyway
        # answers from its priors — an "INVESTIGATED" dossier with no evidence
        # behind it is the one artefact this loop must never write (vj's iwoca
        # draft, 2026-10-11, is the example: 12 fields, all "unknown").
        return {"outcome": "blocked",
                "why": ("the pack carries no advert link"
                        + (f" (its Link cell says {raw!r})" if raw else "")),
                "pack": str(pack_dir)}

    fetch_page = fetcher or _default_fetcher()
    evidence, fetches = [], []
    # The advert first, then the company's own front page: the advert says what
    # the role is, the site says what the company is — the two halves of the
    # investigation, and two fetches is the whole budget. An ATS-hosted advert
    # has no second page (`_site_root` returns ""), which is the honest answer.
    urls = [u for u in (facts["url"], _site_root(facts["url"])) if u]
    for url in urls[:MAX_FETCHES_PER_ACTION]:
        if time.monotonic() > deadline:
            break
        text, provider = fetch_page(url, use_cache=False)
        if text:
            evidence.append(f"SOURCE: {url} (via {provider})\n{text[:12000]}")
            fetches.append({"url": url, "provider": provider, "chars": len(text)})
    if not evidence:
        return {"outcome": "blocked",
                "why": f"nothing could be fetched from {facts['url']}",
                "pack": str(pack_dir), "fetches": fetches}

    prompt = _investigation_prompt(facts, evidence)
    # The tick's calls are not crawl evidence. Unbound, `model_transcripts`
    # falls back to `config.DATASET_DIR` — which is lj's — so vj's and kj's tick
    # calls were landing in *LJ's* dataset ledger inside the sealed tree
    # (measured 2026-10-11: 12 `[tick]` rows in state/example/dataset/model_calls.jsonl,
    # including kj's Tacit Labs and vj's iwoca). The loop's residue belongs in
    # the loop's directory.
    from model_transcripts import active_dataset_dir

    run_id = run_id or (f"tick-{state_root.name}-"
                        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%S')}")
    with active_dataset_dir(loop_dir(state_root) / "transcripts"), outer_run_id(run_id):
        investigation = client.structured(
            [{"role": "user", "content": prompt}], Investigation,
            max_tokens=INVESTIGATION_MAX_TOKENS,
            context=f"[tick] investigate {pack_dir.name}")

    draft = _render_investigation(facts, investigation, fetches)
    # Not named `draft_path`: that is the module's function, and binding the
    # name here would turn the call above into an UnboundLocalError.
    draft_file = draft_path(state_root, pack_dir)
    draft_file.parent.mkdir(parents=True, exist_ok=True)
    # Atomic for the same reason, with a sharper edge: a torn draft still
    # *exists*, and `draft_is_fresh` reads existence as "this pack is done".
    draft_tmp = draft_file.with_name(draft_file.name + ".tmp")
    draft_tmp.write_text(draft, encoding="utf-8")
    os.replace(draft_tmp, draft_file)

    applied = _apply_dossier(state_root, facts, draft) if apply else {"effect": "draft only"}
    if applied.get("contended"):
        return {"outcome": "deferred", "contended": True,
                "why": "writer holds the principal",
                "pack": str(pack_dir), "effect": "draft only",
                "verification": str(draft_file)}
    effect = applied["effect"]
    return {"outcome": "acted", "action": "investigate_pack",
            "pack": str(pack_dir), "fetches": fetches,
            "confidence": investigation.confidence,
            "unknown": investigation.unknown, "effect": effect,
            "verification": str(draft_file)}


# The prompt lives in `prompts.py` (2026-10-15).
from prompts import investigation_prompt as _investigation_prompt


def _render_investigation(facts: dict, inv, fetches: list[dict]) -> str:
    def bullets(values, empty: str) -> str:
        return "\n".join(f"- {v}" for v in values) if values else f"- {empty}"

    return (
        f"# Company, hiring, and work reality — {facts['org']}\n\n"
        f"Status: INVESTIGATED (tick, {date.today().isoformat()}) — "
        f"confidence **{inv.confidence}**\n\n"
        f"## Entity\n\n{inv.entity}\n\n"
        f"## People\n\n{bullets(inv.people, 'none named in the evidence')}\n\n"
        f"## Product\n\n{inv.product}\n\n"
        f"## Hiring evidence\n\n{bullets(inv.hiring_evidence, 'none found')}\n\n"
        f"## Funding\n\n{inv.funding}\n\n"
        f"## Likely daily collaborators\n\n"
        f"{bullets(inv.collaborators, 'not stated')}\n\n"
        f"## Risks\n\n{bullets(inv.risks, 'none identified')}\n\n"
        f"## Unknown\n\n{bullets(inv.unknown, 'nothing material')}\n\n"
        f"## Questions for the principal\n\n{bullets(inv.questions, 'none')}\n\n"
        f"## Sources\n\n"
        + ("\n".join(f"- {u}" for u in inv.sources) or
           bullets([f"{f['url']} (via {f['provider']})" for f in fetches],
                   "no page could be fetched — nothing was verified"))
        + "\n\n---\n\n_Produced by the LoveWork tick. A dossier, not a "
          "submission._\n")


def _apply_dossier(state_root: Path, facts: dict, draft: str) -> dict:
    """Fill the pack's block with `draft`, under the principal's writer lease.

    The one effect outside `state/<p>/loop/`: the lease is taken for the write
    only and contention is a deferral with its own exit code, not a failure
    (ch43 §5). One authority, because two callers use it — the fresh
    investigation and the catch-up below.
    """
    if facts["dossier"] is None:
        return {"effect": "draft only — no dossier to fill"}
    from writer_boundary import WriterBusy, standalone_writer_guard

    try:
        with standalone_writer_guard(state_root.name, mode="tick",
                                     state_root=state_root):
            return {"effect": _write_investigation(facts, draft)}
    except WriterBusy:
        return {"contended": True}


def _write_investigation(facts: dict, draft: str) -> str:
    """Replace the dossier's `NOT YET INVESTIGATED` block with the draft.

    The marker is the gate `application_packs.uninvestigated_packs` reads, so
    the write is exactly what makes the pack ready — and it is the only write
    this action performs outside `state/<p>/loop/`.
    """
    from application_packs import NOT_INVESTIGATED_MARKER, block_end

    dossier: Path = facts["dossier"]
    text = dossier.read_text(encoding="utf-8")
    start = text.find(NOT_INVESTIGATED_MARKER)
    if start < 0:
        return "marker not found — nothing written"
    head = text[:start]
    # The block ends at the next section rule — the `=` rule of the `.txt`
    # dossiers or, since the 2026-10-15 conversion to markdown, the next `## `
    # heading. Asked for through `application_packs.block_end`: looking only for
    # the rule meant this action could not fill a single markdown dossier, and it
    # said nothing about it (measured 2026-10-16 — every pack stayed
    # `NOT YET INVESTIGATED` while the tick kept reporting drafts written).
    end = block_end(text, start)
    if end < 0:
        # No separator after the marker: the block runs to the end of the file
        # and anything "after" it is not a section — refusing beats truncating
        # the principal's dossier (2026-10-11).
        return "no block end after the marker — nothing written"
    tail = text[end:]
    body = draft.split("\n", 1)[1] if "\n" in draft else draft
    # Atomic: a crash between truncate and write would leave the principal's
    # pack half-filled, which is worse than not writing it (ch42 §7 weakness 3).
    tmp = dossier.with_name(dossier.name + ".tick-tmp")
    tmp.write_text(head + body + tail, encoding="utf-8")
    os.replace(tmp, dossier)
    return f"dossier block filled ({len(body)} chars)"


# ── one action: review an existing GO against its own advert ─────────────
#
# LJ, 2026-10-18. Every GO has a pack, so the loop's *pack* work is done; the
# work left is the quality of what is on file. The evidence for this action is
# LJ's own measurement: PostHog's GO was 8.9 with an invented DSPy rationale when
# the score was computed from a careers *list*, and 8.2 with a grounded one when
# the advert itself was read. A GO whose score never saw the advert is the
# highest-value review, because part of its reasoning was invented.

def _evidence_hash(text: str) -> str:
    """The fetched advert's identity, so a repeat review can be recognised.

    Hashes exactly the bytes the matcher is handed (bounded by
    `crawler.EVIDENCE_CHARS`), not the whole page: two fetches that differ only
    beyond what the model reads are the same evidence.
    """
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _matcher_chain(profile: str, client):
    """The matcher the crawl uses, minus the wrappers that write or read elsewhere.

    `pipeline.py` composes four: `JobMatcher` → `AssessmentCachingMatcher` →
    `EvidenceGroundedMatcher` → `EnrichingMatcher`. Three differences here, each a
    rule rather than a convenience:

    - **Enriching is absent**: this action fetches the advert *itself*
      (`use_cache=False`), because the whole point is that the review reads the
      advert as it is now. Letting the enricher fetch again would spend a second
      fetch on the same page.
    - **The assessment cache is absent**: it lives in `cache/assessments/`, inside
      the sealed tree, and the tick must not write there — a tick artifact landing
      in `cache/` would be captured by the next seal as if it were crawl evidence
      (docs/43 §7).
    - **`use_history=False`**: the same ruling `reassess.py` makes for its own
      replay, in the same words — *"This is a replay of retained material, not an
      opportunity to inspect the principal's mailbox or change a Gmail read
      state."* A review is exactly that, so the prior-contact scan (which reads
      Gmail through a subprocess, per principal, per review) is not run. What that
      costs is named rather than hidden: the reapply kill and the applied-before
      cap are not consulted. Neither could have fired when the GO was *scored* —
      either would have made it a DROP — and "have I applied since?" is the pack's
      own status line's business, not a re-score's.

    What remains is the judgement itself: the same prompt, the same profile text,
    the same principal-evidence grounding, the same calibration and tier bands.
    """
    from matcher import JobMatcher
    from principal_evidence import EvidenceGroundedMatcher, PrincipalEvidenceIndex

    # The role the principal's *sweep* scores against, from the one authority for
    # it (`crawl_operation.DEFAULT_ROLES`, which the wrapper mirrors). A pack does
    # not record the role it was cut under, so re-deriving it here is the only
    # honest option — and scoring against a role the sweep never uses would move
    # every score for a reason that has nothing to do with the advert.
    from crawl_operation import DEFAULT_ROLES

    role = DEFAULT_ROLES.get(profile, "general")
    profile_text = config.load_profile_text(profile, role=role)
    matcher = JobMatcher(client, profile_text, registry=None, use_history=False,
                         principal=profile)
    return EvidenceGroundedMatcher(
        matcher, PrincipalEvidenceIndex(profile_text, config.load_bio(profile)))


def review_go(state_root: Path, *, target=None, apply: bool = False, client=None,
              deadline: float = 0.0, fetcher=None, today: date | None = None,
              run_id: str = "") -> dict:
    """Re-read ONE GO: fetch the advert, re-score it against that, note what moved.

    `target` is the pack the *decision* named, and it is the pack that is worked —
    the same rule as `investigate_pack`, for the same measured reason (a receipt
    that names a decision the tick did not serve is false, 2026-10-11).

    The score is **not** written back into the pack's decision. A verdict is a
    prohibition (docs/45 §3.2): the pack's `Initial decision` is what the
    principal was told, and a re-score is new *information* about it, not an
    authority to change it. So the review is a note beside the dossier, and the
    note says so.
    """
    today = today or date.today()
    if target is None:
        # A direct invocation with no decision behind it: the honest answer is
        # that this action has no target, not a guess at one (the class fixed on
        # 2026-10-11 was exactly an action choosing its own pack).
        return {"outcome": "blocked",
                "why": "review_go needs the decision's target pack"}
    if deadline and time.monotonic() > deadline:
        # The run's clock, not this action's: a tick that has already spent its
        # minutes must not start a fetch and a model call it cannot finish.
        return {"outcome": "deferred", "why": "the run's clock is spent"}
    pack_dir = Path(target)
    if not pack_dir.is_dir():
        return {"outcome": "blocked",
                "why": f"the decision's pack is not there: {pack_dir}"}

    from application_packs import dossier_entry

    rebuilt = dossier_entry(pack_dir)
    if rebuilt is None:
        return {"outcome": "blocked",
                "why": f"{pack_dir.name} holds no LoveWork dossier",
                "pack": str(pack_dir)}
    entry, identity = rebuilt
    facts = _pack_facts(pack_dir)
    if not facts["url"]:
        return {"outcome": "blocked",
                "why": "the pack carries no advert link to re-read",
                "pack": str(pack_dir)}

    fetch_page = fetcher or _default_fetcher()
    text, provider = fetch_page(facts["url"], use_cache=False)
    if not text:
        return {"outcome": "blocked",
                "why": f"nothing could be fetched from {facts['url']}",
                "pack": str(pack_dir), "fetches": []}

    # Never re-score a page a closed redirect answers (2026-10-18, Together
    # AI): the fetch happily returns the employer's *board* — alive, job-rich,
    # and not the advert — and the re-read then wrote a confident note about
    # the wrong page (a 6.1 MAYBE scored on a 78-job index). The server-side
    # redirect is the employer's own claim that the advert is gone; record it
    # as the review's finding and leave the closing to the link-check verdict
    # and the retire pass it feeds.
    from link_check import closed_redirect

    board = closed_redirect(facts["url"])
    if board:
        record_review(state_root, {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "profile": state_root.name, "pack": pack_dir.name,
            "advert_identity": identity, "run_id": run_id,
            "scored": False, "closed": True,
            "why": f"redirected to the employer's board: {board}",
            "evidence_url": board, "evidence_provider": provider,
            "note": "",
        })
        return {"outcome": "blocked",
                "why": f"the advert has closed: redirected to {board}",
                "pack": str(pack_dir),
                "fetches": [{"url": facts["url"], "provider": provider,
                             "chars": len(text)}]}

    # What was actually fetched? A re-post, a mirror or a stub is not the
    # employer's advert, and a score computed from one is a claim about a page
    # nobody read (2026-11-03: kj 7.1 → 0.0 off 873 characters of a LinkedIn
    # job-alert redirect). Refuse to score; the pack keeps its standing and the
    # next tick retries — `resolve_advert` is what finds the employer's page.
    from application_packs import advert_link_is_board

    host = urlparse(facts["url"]).netloc.lower()
    if advert_link_is_board(facts["url"]):
        return {"outcome": "blocked",
                "why": f"the pack's link is a board page, not the employer's advert "
                       f"({host}) — not scored; a resolve_advert pass can find the real one",
                "pack": str(pack_dir),
                "fetches": [{"url": facts["url"], "provider": provider,
                             "chars": len(text)}]}
    if len(text) < MIN_ADVERT_CHARS:
        return {"outcome": "blocked",
                "why": f"only {len(text)} characters came back from {facts['url']} — "
                       f"under the {MIN_ADVERT_CHARS}-character floor for an advert, "
                       f"so it was not scored",
                "pack": str(pack_dir),
                "fetches": [{"url": facts["url"], "provider": provider,
                             "chars": len(text)}]}

    from crawler import EVIDENCE_CHARS

    advert_text = text[:EVIDENCE_CHARS]
    evidence_hash = _evidence_hash(advert_text)
    fetches = [{"url": facts["url"], "provider": provider, "chars": len(text)}]

    reviews = read_reviews(state_root)
    last = reviews.get(pack_dir.name) or {}
    if last.get("evidence_hash") == evidence_hash and last.get("note"):
        # The input is the same bytes the last review scored, and its note is
        # published: a second model call could only repeat the first. This is the
        # sharpest form of "no decision, no loop work" — the loop has nothing new
        # to say, and what it said is the principal's to read.
        return {"outcome": "needs_principal",
                "why": "the advert is unchanged since the last review; the note stands",
                "pack": str(pack_dir), "fetches": fetches,
                "verification": str(last.get("draft") or last.get("note") or "")}

    # The description is the advert, in the shape the crawl hands the matcher
    # (`enrichment.EnrichedLead.matcher_description`): the page, named, as the
    # evidence it is. The crawl's `job_evidence_text(job)` is the same idea from
    # the other side — what the matcher judges by is the advert, never a list
    # snippet (2026-10-17).
    description = (f"--- PRIMARY ADVERT PAGE ({facts['url']}) ---\n{advert_text}")
    # The model client is made here, not on entry: every refusal above is decided
    # from the page (a board link, a closed redirect, a stub under the evidence
    # floor), and needing a credential to say "not scored" made those refusals
    # untestable — and would make them fail on a host with no key (2026-11-07).
    client = client or model_client()
    matcher = _matcher_chain(state_root.name, client)
    run_id = run_id or (f"tick-{state_root.name}-"
                        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%S')}")
    from model_transcripts import active_dataset_dir

    with active_dataset_dir(loop_dir(state_root) / "transcripts"), outer_run_id(run_id):
        match = matcher.match(facts["role"] or entry.title, description, facts["org"],
                              job_url=facts["url"], location=entry.location or "")

    if (match.assessment_status or "").upper() == "UNSCORED":
        # The model did not answer: nothing was re-read, so nothing is recorded
        # and the next tick tries again (the envelope bounds the retries). The
        # alternative — recording it as a review — would make a failed call look
        # like a re-read, and the freshness window would then hide the GO for a
        # fortnight.
        return {"outcome": "blocked",
                "why": f"the re-score came back UNSCORED: "
                       f"{str(match.reasoning or '')[:120]}",
                "pack": str(pack_dir), "fetches": fetches}

    old_score = float(entry.score or 0.0)
    new_score = float(match.score or 0.0)
    delta = round(new_score - old_score, 2)
    tier_moved = (match.decision or "") != (entry.decision or "")
    moved = abs(delta) >= REVIEW_MATERIAL_DELTA or tier_moved
    model = getattr(client, "model", "") or ""
    from calibration import offset_for

    note = _render_review(facts, entry, match, old_score=old_score,
                          new_score=new_score, delta=delta, moved=moved,
                          tier_moved=tier_moved, fetches=fetches, model=model,
                          offset=offset_for(model), today=today)

    # The draft is always written: it is the action's residue and it is what the
    # receipt's `verification` points at — the same shape `investigate_pack` uses,
    # where the draft is the verification and the published copy is the effect.
    draft_file = review_draft_path(state_root, pack_dir)
    draft_file.parent.mkdir(parents=True, exist_ok=True)
    draft_tmp = draft_file.with_name(draft_file.name + ".tmp")
    draft_tmp.write_text(note, encoding="utf-8")
    os.replace(draft_tmp, draft_file)

    # **A note is what a material move produces.** LJ: *"If the score MOVES
    # materially, write a review note on the lead … if it does not move, record
    # the review date and count so it is not reviewed again immediately."* So a
    # review that did not move is recorded in the ledger and writes nothing into
    # the principal's pack.
    effect = (f"review {'note' if moved else 'recorded'} "
              f"({old_score:.1f} → {new_score:.1f}, "
              f"{'moved' if moved else 'no move'})")
    published = ""
    contended = False
    if moved and apply:
        written = _write_review_note(state_root, pack_dir, note)
        if written.get("contended"):
            contended = True
            effect += " — draft only, a writer holds the principal"
        elif written.get("written"):
            published = str(review_note_path(pack_dir))
            effect += " — in the pack"
    elif moved:
        effect += " — draft only"

    record_review(state_root, {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile": state_root.name, "pack": pack_dir.name,
        "advert_identity": identity, "run_id": run_id,
        "old_score": old_score, "new_score": new_score, "delta": delta,
        "old_decision": entry.decision, "new_decision": match.decision,
        "moved": moved, "tier_moved": tier_moved, "scored": True,
        "model": model, "offset": offset_for(model),
        # Did the ORIGINAL score see the advert's own text? The pack's dossier is
        # the authority (`primary_content_hash` is set by `enrichment.py` when the
        # matcher judged the fetched primary page), and it is the fact that made
        # this GO the next one to review.
        "scored_with_advert_before": bool(entry.primary_content_hash),
        "evidence_hash": evidence_hash, "evidence_url": facts["url"],
        "evidence_provider": provider, "evidence_chars": len(text),
        # `draft` is the residue and what a receipt verifies; `note` is the
        # reader's copy in the pack, empty when the move was not material or the
        # lease was held. A review whose note was never published is re-done when
        # the freshness window lapses — `note` empty is what says so, and the
        # short-circuit above therefore cannot fire on it.
        "draft": portable_path(draft_file),
        "note": portable_path(published) if published else "",
        "published": bool(published),
        "review_count": int(last.get("count", 0)) + 1,
    })
    if contended:
        # A lease held elsewhere is a deferral with its own exit code (75), the
        # house vocabulary — never a silent write and never a failure. The draft
        # is on disk, so the work is not lost.
        return {"outcome": "deferred", "contended": True,
                "why": "writer holds the principal",
                "pack": str(pack_dir), "effect": effect, "fetches": fetches,
                "verification": str(draft_file), "moved": moved}
    return {"outcome": "acted", "action": "review_go",
            "pack": str(pack_dir), "fetches": fetches,
            "old_score": old_score, "new_score": new_score, "delta": delta,
            "moved": moved, "tier_moved": tier_moved, "model": model,
            "effect": effect, "verification": str(draft_file),
            "note": published,
            "review_count": int(last.get("count", 0)) + 1}


def _write_review_note(state_root: Path, pack_dir: Path, note: str) -> dict:
    """Publish the review note into the pack, under the principal's writer lease.

    The one effect outside `state/<p>/loop/`, exactly as `_apply_dossier` is for
    the investigation: the lease is held for the write only, and contention is a
    deferral with its own exit code (ch43 §5), never a silent write. The write is
    atomic — a torn note beside the principal's dossier is worse than no note.
    """
    from writer_boundary import WriterBusy, standalone_writer_guard

    path = review_note_path(pack_dir)
    try:
        with standalone_writer_guard(state_root.name, mode="tick",
                                     state_root=state_root):
            tmp = path.with_name(path.name + ".tick-tmp")
            tmp.write_text(note, encoding="utf-8")
            os.replace(tmp, path)
    except WriterBusy:
        return {"contended": True}
    return {"written": True, "effect": f"review note written ({len(note)} chars)"}


def _render_review(facts: dict, entry, match, *, old_score: float, new_score: float,
                   delta: float, moved: bool, tier_moved: bool,
                   fetches: list[dict], model: str, offset: float,
                   today: date) -> str:
    """The review note: what we thought, what we now see, and on what evidence."""

    def bullets(values, empty: str) -> str:
        return "\n".join(f"- {v}" for v in values) if values else f"- {empty}"

    before = ("with the advert's own text"
              if entry.primary_content_hash else
              "**WITHOUT the advert's own text** (the title/list path, so part of "
              "that reasoning was inferred rather than read)")
    sources = [f"- {f['url']} (via {f['provider']}, {f['chars']} chars)"
               for f in fetches] or ["- no page could be fetched — nothing was verified"]
    lines = [
        f"# Review — {facts['org']} — {facts['role']}",
        "",
        f"Re-read by the LoveWork loop on {today.isoformat()}. This is a **review "
        f"note, not a re-verdict**: the pack's own decision stands unchanged, and "
        f"the principal decides what to do with the move below.",
        "",
        "## What moved",
        "",
        f"- **Scored before**: {old_score:.1f}/10 ({entry.decision}) — {before}"
        + (f", fetched {entry.primary_fetched_at}" if entry.primary_fetched_at else ""),
        f"- **Scored now**: {new_score:.1f}/10 ({match.decision}) — against the advert "
        f"itself, fetched live",
        f"- **Move**: {delta:+.1f}"
        + (f", and the tier moved {entry.decision} → {match.decision}" if tier_moved
           else ", same tier"),
        f"- **Material?** {'yes' if moved else 'no'} — the threshold is "
        f"{REVIEW_MATERIAL_DELTA:.1f} (a smaller move is inside the ±1.9 per-advert "
        f"spread measured between models, `calibration.py`); a tier change is "
        f"material whatever the delta",
        f"- **Model**: {model or 'unstated'}"
        + (f" (calibration offset {offset:+.2f})" if offset else
           " — **no measured calibration offset**, so this score is not on the same "
           "scale as the one it is compared with; read the move with that in mind"),
        "",
        "## Why the new score says this",
        "",
        (match.reasoning or "_the model gave no reasoning_").strip(),
    ]
    for label, items in (("Evidence alignment", match.alignment_matrix),
                         ("Gaps", match.gaps)):
        if items:
            lines.extend(["", f"**{label}**", "", bullets(items, "none")])
    for label, value in (("Application angle", match.application_angle),
                         ("Screening story", match.screening_story),
                         ("Likely day-to-day", match.likely_day_to_day),
                         ("Prestige-trap risk", match.prestige_trap_risk)):
        if value:
            lines.extend(["", f"**{label}**: {value}"])
    lines.extend([
        "",
        "## The evidence this review read",
        "",
        *sources,
        "",
        f"- Advert text handed to the matcher: {sum(f['chars'] for f in fetches)} chars "
        f"fetched, {len(fetches)} page(s); the bound is `crawler.EVIDENCE_CHARS`",
        "",
        "---",
        "",
        "_Produced by the LoveWork tick. The pack's dossier holds the original "
        "judgement and is unchanged; this note is what the loop saw when it "
        "re-read the advert._",
        "",
    ])
    return "\n".join(lines)


# ── paths that survive the merge ─────────────────────────────────────────

def portable_path(path, root: Path | None = None) -> str:
    """A path that means the same on both hosts: relative to the repo root.

    Receipts travel between host-b and host-a by git, so an absolute path is
    a claim only one host can check: `/Users/user/...` resolves nowhere on
    host-a and the audit there reported the draft missing.
    """
    root = Path(root or config.LOVEWORK_ROOT)
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return str(candidate)


def _exists(path: Path) -> bool:
    """`path.exists()` that treats an unreadable parent as "not here".

    Python 3.13's `exists()` re-raises `PermissionError`, so a receipt that
    records another user's home (`/home/user/...` while the runtime runs as
    `lovework`) crashed the audit loop instead of falling through to the
    re-root branch below. 2026-10-26: the first tick as the new `lovework` user
    died on exactly that.
    """
    try:
        return path.exists()
    except OSError:
        return False


def resolve_stored_path(stored: str, root: Path | None = None,
                        state_dir: Path | None = None) -> Path | None:
    """Resolve a stored path on *this* host, or return None.

    A relative path (the portable form), an absolute path that exists here, and
    an absolute path recorded on the other host — each also tried with its tail
    below `state/` re-anchored at the state dir, so a merged receipt still
    verifies its own artefact instead of looking lost.

    That re-anchoring exists because state moved OUT of the code checkout
    (2026-10-30): receipts written before the split say `state/example/loop/drafts/x.md`,
    which now lives at `$LOVEWORK_STATE_DIR/lj/...`. Resolving them against the
    checkout reported 328 healthy artefacts as missing (measured 2026-11-03).
    """
    if not stored:
        return None
    root = Path(root or config.LOVEWORK_ROOT)
    state = Path(state_dir or config.STATE_DIR)
    candidate = Path(stored)
    parts = candidate.parts

    def _under_state() -> Path | None:
        """`state/<p>/...` re-anchored at the state dir (it left the checkout)."""
        if "state" not in parts:
            return None
        return state / Path(*parts[parts.index("state") + 1:])

    probes: list[Path] = []
    if candidate.is_absolute():
        if _exists(candidate):
            return candidate
        probes.extend(p for p in (_under_state(),
                                  root / Path(*parts[parts.index("state"):])
                                  if "state" in parts else None) if p is not None)
    else:
        probes.extend(p for p in (root / candidate, _under_state()) if p is not None)
    for probe in probes:
        if _exists(probe):
            return probe
    return None


def _archive_backup(backup: Path, root: Path | None = None) -> Path:
    """Move a repair's backup out of the repo, keeping it as evidence.

    `~/.lovework/ledger-backups/` sits beside the credentials: host-local,
    outside every git tree and every quota'd sync root, and named so the file
    says what it is. Falls back to leaving it in place if the move fails — a
    failed housekeeping step must not undo a repair that verified.
    """
    home = Path(os.environ.get("LOVEWORK_LEDGER_BACKUPS")
                or Path(os.getenv("LOVEWORK_HOME") or (Path.home() / ".lovework"))
                / "ledger-backups")
    try:
        home.mkdir(parents=True, exist_ok=True)
        destination = home / f"{backup.parent.parent.name}-{backup.name}"
        os.replace(backup, destination)
        return destination
    except OSError:
        return backup


def _scan_stamp(stat: os.stat_result) -> dict:
    """What the scan marker records: identity, and where the scan stopped.

    `offset` is the byte at which the next scan may resume. It is only ever
    written after a *clean* scan, so a marker always means "everything before
    this offset was read and held no `[tick]` rows".
    """
    return {"size": stat.st_size, "mtime": round(stat.st_mtime, 3),
            "inode": stat.st_ino, "offset": stat.st_size}


def _mark_scan(marker_path: Path, stat: os.stat_result) -> None:
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps(_scan_stamp(stat)), encoding="utf-8")
    except OSError:
        pass                       # a cache that cannot be written is not a failure


def _at_line_boundary(path: Path, offset: int) -> bool:
    """May a read resume at `offset` without splitting a row?"""
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, offset - 1))
            return handle.read(1) == b"\n"
    except OSError:
        return False


def _physical_lines_from(path: Path, offset: int) -> list[str]:
    """`physical_lines` from a byte offset that is known to be a line boundary.

    Binary, split on `\\n` and nothing else — the same rule as `physical_lines`.
    Text mode is not used because a byte offset is not a valid text-mode seek
    position.
    """
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    return [part.decode("utf-8", "replace") for part in parts]


def stray_tick_rows(state_root: Path, *, force: bool = False) -> tuple[int, list[str]]:
    """How many `[tick]` rows are in the principal's crawl ledger, and which.

    One scan, shared by the repair and the audit, and **cached on the ledger's
    size and mtime**: the file is ~180 MB, and a 30-minute job must not re-read it
    three times a tick to learn that nothing changed.

    **An append is not a rewrite** (2026-10-24). The marker also carries the byte
    offset a clean scan reached, so a crawl appending rows during a sweep costs
    a read of the new tail rather than a full scan every hour. A shrunk file, a
    new inode, an unaligned offset, or a size-only change falls back to the full
    scan — the safe answer when the file's history is not simply "more bytes".
    The ledger's writers are the crawl (append) and the loop's repair (rewrite);
    the repair leaves the old marker stale, so the next scan is full and clean.
    """
    ledger = state_root / "dataset" / "model_calls.jsonl"
    if not ledger.is_file():
        return 0, []
    marker_path = loop_dir(state_root) / "ledger-scan.json"
    try:
        stat = ledger.stat()
        stamp = {"size": stat.st_size, "mtime": round(stat.st_mtime, 3)}
    except OSError:
        return 0, []
    marker: dict | None = None
    if not force and marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            marker = None
    if isinstance(marker, dict):
        if marker.get("size") == stamp["size"] and marker.get("mtime") == stamp["mtime"]:
            return 0, []          # unchanged since the last scan
        offset = marker.get("offset")
        if (isinstance(offset, int) and marker.get("size") == offset
                and marker.get("inode") in (None, stat.st_ino)
                and 0 < offset < stat.st_size
                and _at_line_boundary(ledger, offset)):
            lines = [line for line in _physical_lines_from(ledger, offset)
                     if "[tick]" in line and line.strip()]
            if not lines:
                _mark_scan(marker_path, stat)
                return 0, []
            return len(lines), lines
    lines = [line for line in physical_lines(ledger)
             if "[tick]" in line and line.strip()]
    if not lines:
        _mark_scan(marker_path, stat)
    return len(lines), lines


# ── the one-time repair, re-applied by every tick ────────────────────────
#
# A repair is host-local until it is re-applied, and the loop's state is
# git-merged between two hosts: the first live run's residue (2026-10-11) is in
# the repository, so the repair has to be idempotent and cheap enough to run on
# every tick rather than a script somebody remembers.

CORRECTION_NOTE = ("the decision_served field named the first due item, not the "
                   "pack this tick worked (defect fixed 2026-10-11; docs/43 §9)")


def _pack_identity_tokens(pack: Path) -> tuple[str, str]:
    """(organisation, role) as comparable tokens from a pack directory's name."""
    parts = pack.name.split("-")
    org = _org_token(parts[1]) if len(parts) > 1 else ""
    rest = parts[2:]
    if rest and rest[-1] == "LoveWork":
        rest = rest[:-1]
    return org, _org_token("".join(rest))


def decision_names_pack(row: dict) -> bool:
    """Does this receipt's `decision_served` name the pack it worked?

    The defect of the first live run was exactly this being false. Asking it
    from the two strings — rather than from the presence of a `target` — is what
    lets the repair and the audit share one rule: a correction is added when the
    claim is wrong, and removed when it is right. The first version annotated
    *every* acted row, including the ones the fixed code had just written
    correctly (found 2026-10-11).
    """
    pack, served = row.get("pack"), _org_token(row.get("decision_served", ""))
    if not pack or not served:
        return False
    org, role = _pack_identity_tokens(Path(pack))
    return bool((org and org in served) or (role and role in served))


def _pack_decision(pack: Path) -> str:
    """The decision a pack *is*, derived from its own name — the same string the
    decider emits for it, so a correction invents nothing."""
    parts = pack.name.split("-")
    org = parts[1].replace("_", " ") if len(parts) > 1 else pack.name
    role = " ".join(parts[2:]).replace("_", " ") if len(parts) > 2 else ""
    role = role.removesuffix("LoveWork").strip()
    principal = pack.parent.parent.name
    return (f"whether {principal} pursues {role.lower()} at {org.lower()}"
            if role else f"whether {principal} pursues {org.lower()}")


def repair_loop_residue(principal: str, *, state_dir: Path | None = None,
                        dry_run: bool = False) -> dict:
    """Bring the loop's residue back into its own directory, in place.

    Two defects of the first live run are repaired here, and both repairs are
    additive — nothing is removed:

    - **tick rows in the crawl ledger**: the tick's model-call transcripts were
      written to `dataset/model_calls.jsonl` (the crawl's cost authority, inside
      the sealed tree, and *LJ's* file whichever principal was ticking). They are
      copied to `loop/transcripts/` and the ledger is rewritten without them,
      with the original kept beside it as `.pre-loop-repair-<date>`.
    - **pre-fix receipts**: a receipt that carries no `target` gets one (the pack
      it worked) plus the disclosed correction; the original claim stays visible.
    """
    state_dir = state_dir or config.STATE_DIR
    state_root = state_dir / principal
    result = {"principal": principal, "ledger_rows_moved": 0, "receipts_corrected": 0,
              "corrections_removed": 0, "drafts_marked_unverified": 0,
              "evidence_sources_named": 0, "verifications_marked_pruned": 0,
              "duplicates_marked": 0, "dry_run": dry_run}
    if not state_root.is_dir():
        return result

    ledger = state_root / "dataset" / "model_calls.jsonl"
    if ledger.is_file():
        stray_count, stray_lines = stray_tick_rows(state_root)
        if not stray_count:
            rows, bad = [], []          # nothing to move: no need to read it
        else:
            lines = physical_lines(ledger)
            rows, bad = parse_rows(ledger)
        if bad:
            # Refuse rather than rewrite: a ledger with unreadable lines cannot
            # be re-emitted without losing or splitting them, and this file is
            # the crawl's cost authority. Reported, never guessed at.
            result["ledger_unreadable"] = bad[:5]
        else:
            # A row is identified by its own bytes, so the split is exact — no
            # `splitlines()`, which breaks rows on separators JSON allows.
            stray = stray_lines
            if stray:
                result["ledger_rows_moved"] = len(stray)
                if not dry_run:
                    transcripts = loop_dir(state_root) / "transcripts"
                    transcripts.mkdir(parents=True, exist_ok=True)
                    target = transcripts / "model_calls.jsonl"
                    known = set()
                    if target.is_file():
                        known = {line.split('"at": "', 1)[-1][:32]
                                 for line in physical_lines(target)}
                    with target.open("a", encoding="utf-8") as handle:
                        for line in stray:
                            stamp = line.split('"at": "', 1)[-1][:32]
                            if stamp in known:          # idempotent on re-run
                                continue
                            handle.write(line + "\n")
                            known.add(stamp)
                    keep = [line for line in lines if not ("[tick]" in line and line.strip())]
                    # The backup is written beside the ledger, used for the
                    # verification below, and then moved out of the repo: it is
                    # the same 126 MB file, and an untracked copy in the state
                    # tree is one `git add -A` away from the history (found
                    # 2026-10-11). Evidence is kept, but outside the tree.
                    backup = ledger.with_name(
                        f"{ledger.name}.pre-loop-repair-{date.today().isoformat()}")
                    if not backup.exists():
                        with ledger.open("rb") as source, backup.open("wb") as dest:
                            dest.write(source.read())
                    ledger.write_text("\n".join(keep) + ("\n" if keep else ""),
                                      encoding="utf-8")
                    moved_rows, moved_bad = parse_rows(ledger)
                    if moved_bad or len(moved_rows) != len(rows) - len(stray):
                        # The write did not reproduce what it promised: put the
                        # original bytes back and say so.
                        with backup.open("rb") as source:
                            ledger.write_bytes(source.read())
                        result["ledger_restored"] = True
                        result["ledger_rows_moved"] = 0
                    else:
                        result["ledger_backup"] = str(_archive_backup(backup))

    # Bound before the branch: the draft-marking step below reads it too, and a
    # variable that exists only inside an `if` is an UnboundLocalError waiting
    # for the branch not to run.
    receipt_rows: list[dict] = []
    receipts_path = loop_dir(state_root) / "receipts.jsonl"
    if receipts_path.is_file():
        receipt_rows, bad = parse_rows(receipts_path)
        if bad:
            # Same gate as the ledger: a receipt file with unreadable lines is
            # reported, not re-emitted into a lossy shape.
            result["receipts_unreadable"] = bad[:5]
            receipt_rows = []
        changed = False
        for row in receipt_rows:
            if row.get("outcome") != "acted":
                continue
            pack = row.get("pack")
            if not pack or Path(pack).name.endswith(".json"):
                continue
            # A pre-fix catch-up row: it republished a draft, so it fetched
            # nothing — and it never said where its evidence was. The row's own
            # `verification` is the draft, so the repair names it rather than
            # leaving a permanent "no evidence" alarm (2026-10-13).
            if (not row.get("fetches") and not row.get("evidence_source")
                    and "from the existing draft" in str(row.get("effect", ""))
                    and row.get("verification")):
                row["evidence_source"] = row["verification"]
                changed = True
                result["evidence_sources_named"] += 1
            if decision_names_pack(row):
                # The claim is right: any correction on this row is an
                # over-application of an earlier repair, and a false note in a
                # receipt is worse than no note.
                if row.pop("correction", None) is not None:
                    row.pop("decision_served_corrected", None)
                    changed = True
                    result["corrections_removed"] += 1
                continue
            if row.get("correction"):
                continue                       # already disclosed
            row.setdefault("target", pack)
            row["decision_served_corrected"] = _pack_decision(Path(pack))
            row["correction"] = CORRECTION_NOTE
            changed = True
            result["receipts_corrected"] += 1
        pruned = _mark_pruned_verifications(receipt_rows, state_dir)
        if pruned:
            result["verifications_marked_pruned"] = pruned
            changed = True
        named = _name_resolved_advert_evidence(receipt_rows)
        if named:
            result["evidence_sources_named"] += named
            changed = True
        if changed and not dry_run:
            receipts_path.write_text(
                "\n".join(json.dumps(r, sort_keys=True) for r in receipt_rows) + "\n",
                encoding="utf-8")

    # One advert, one pack: a tick that cuts a new pack for an advert already
    # prepared leaves a duplicate behind, and the audit reports it as a defect
    # (`pack_duplicates`). Marking is additive and idempotent, so the repair does
    # it rather than leaving the alarm to be cleared by hand (2026-11-03).
    applications = state_root / "applications"
    if applications.is_dir():
        from application_packs import mark_superseded_duplicates

        marked = mark_superseded_duplicates(applications, dry_run=dry_run)
        if marked.get("marked"):
            result["duplicates_marked"] = marked["marked"]
        if marked.get("restored"):
            result["duplicates_restored"] = marked["restored"]

    # A draft written from no evidence claims INVESTIGATED. It is not deleted —
    # it is marked, so a reader is not misled about what backs it.
    for row in receipt_rows:
        if row.get("fetches") or row.get("outcome") != "acted":
            continue
        pack = row.get("pack")
        if not pack:
            continue
        # `pack` may be the portable (repo-relative) form; the draft's name is
        # the pack directory's name either way.
        draft = draft_path(state_root, Path(pack))
        if not draft.is_file():
            continue
        text = draft.read_text(encoding="utf-8")
        if "UNVERIFIED" in text:
            continue
        result["drafts_marked_unverified"] += 1
        if not dry_run:
            draft.write_text(
                text.replace(
                    "Status: INVESTIGATED",
                    "Status: UNVERIFIED — no page could be fetched, so nothing "
                    "here is evidence (corrected 2026-10-11)", 1),
                encoding="utf-8")
    return result


# ── the loop's own detector ──────────────────────────────────────────────

def physical_lines(path: Path) -> list[str]:
    """The file's lines, split on `\n` and nothing else.

    **Not `read_text().splitlines()`.** `str.splitlines()` also breaks on the
    Unicode separators a JSON string may legally contain (`\u2028`, `\u2029`,
    `\x0b`, `\x1c`…), so a ledger row carrying one is read as two fragments and
    written back as a raw newline inside a string — invalid JSON, and a rewrite
    that corrupts the file it was repairing. Measured 2026-10-11: a 126 MB
    `model_calls.jsonl` went from 7,012 valid rows to 6,963 + 50 fragments.
    """
    with path.open(encoding="utf-8", errors="replace") as handle:
        return [line.rstrip("\n") for line in handle]


def parse_rows(path: Path) -> tuple[list[dict], list[str]]:
    """Rows and the physical lines that are not valid JSON, in order.

    The zero-defect gate for a rewrite: a caller that cannot account for every
    line must not write the file back.
    """
    rows, bad = [], []
    for number, line in enumerate(physical_lines(path), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            bad.append(f"line {number}: {exc}")
    return rows, bad


def read_receipts(state_root: Path) -> list[dict]:
    """Every receipt on file, oldest first. A malformed line is reported, not
    skipped silently — a receipt that cannot be read is a receipt that cannot
    verify anything."""
    path = loop_dir(state_root) / "receipts.jsonl"
    if not path.is_file():
        return []
    rows, bad = parse_rows(path)
    rows.extend({"at": "", "unreadable": detail} for detail in bad)
    return rows


#: Receipts written before this date may name artefacts the state split
#: (2026-10-30) moved out of the code checkout and cleared. Their dangling
#: pointers are annotated once, so the missing-evidence alarm keeps working for
#: everything written afterwards (2026-11-03).
PRUNE_CUTOFF = "2026-10-31"


#: `resolve_advert`'s effect line, which has named the resolved URL since the
#: action existed: "advert link resolved: https://…".
_ADVERT_RESOLVED = re.compile(r"advert link resolved:\s*(\S+)")


def _name_resolved_advert_evidence(rows: list[dict]) -> int:
    """Name the URL that a pre-fix `resolve_advert` receipt already carries.

    That action wrote the resolved link into the pack and left the receipt's
    evidence fields empty, so its `acted` rows read as evidence-free. The URL is
    already in the row's own `effect`: naming it as the evidence source restates
    the receipt rather than inventing anything, and only rows whose effect
    actually names a URL are touched. Rows written after the fix bring their own
    fetch and are skipped (they have `fetches`).
    """
    named = 0
    for row in rows:
        if row.get("outcome") != "acted":
            continue
        if row.get("fetches") or row.get("evidence_source"):
            continue
        match = _ADVERT_RESOLVED.search(str(row.get("effect", "")))
        if not match:
            continue
        row["evidence_source"] = match.group(1)
        named += 1
    return named


def _resolve_through_moves(verification: str, state_root: Path) -> Path | None:
    """Follow `retire`'s move map: a retired artefact lives in a pile, not nowhere.

    Retirement moves a directory whole and records old -> new in
    `cache/moves.json`; the serving layer resolves through it so links keep
    working. A receipt written before the move still names the old path, so the
    audit walks the same map rather than calling the artefact lost (2026-11-03:
    the first live tick after the loop was re-enabled flagged a pack that had
    been retired into `gone/` the evening before).
    """
    from retire import move_map

    moves = move_map(state_root)
    if not moves:
        return None
    text = str(verification).replace("\\", "/")
    for old, new in moves.items():
        marker = str(old).strip("/")
        index = text.find(marker)
        if index < 0:
            continue
        suffix = text[index + len(marker):].strip("/")
        candidate = Path(state_root) / str(new).strip("/")
        if suffix:
            candidate = candidate / suffix
        if _exists(candidate):
            return candidate
    return None


def _mark_pruned_verifications(rows: list[dict], state_dir: Path) -> int:
    """Annotate receipts whose named artefact the state split cleared.

    The audit's `receipt_verification_missing` is the alarm for evidence that
    vanished from under a receipt — the case worth waking up for. Silencing it
    globally would retire the detector, so this marks only rows written before
    PRUNE_CUTOFF, and only after confirming the artefact really is gone:

      * the original `verification` path stays visible (nothing is erased);
      * `verification_pruned` dates the annotation;
      * `verification_pruned_why` says what happened.

    Idempotent: an annotated row is left alone.
    """
    marked = 0
    for row in rows:
        verification = row.get("verification")
        if not verification or row.get("verification_pruned"):
            continue
        if str(row.get("at", "")) >= PRUNE_CUTOFF:
            continue
        if resolve_stored_path(verification, state_dir.parent, state_dir) is not None:
            continue
        row["verification_pruned"] = date.today().isoformat()
        row["verification_pruned_why"] = (
            "state split 2026-10-30 moved the corpus out of the checkout and "
            "cleared these drafts; the receipt's claim is kept, its artefact is gone")
        marked += 1
    return marked


def _org_token(text: str) -> str:
    """Lower-case alphanumerics only — the pack name and the decision text spell
    an organisation differently (`z0.ai` vs `z0.ai`, `Tacit_Labs` vs `Tacit Labs`)."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def audit_receipts(profile: str, *, state_dir: Path | None = None,
                   today: date | None = None) -> dict:
    """The detector for the loop's artefact class (ch42 §7 point 2).

    An artefact class without a detector is not admissible, and the first live
    run proved why: every receipt named a decision the tick had not served, and
    nothing could tell. The checks are the four ways a receipt can lie or a
    draft can be orphaned:

    - a receipt that names a different pack than the one it worked;
    - a receipt whose `verification` path is gone;
    - an acted receipt with no evidence fetched behind it;
    - tick rows in the principal's crawl ledger (`dataset/model_calls.jsonl`).
    """
    state_dir = state_dir or config.STATE_DIR
    state_root = state_dir / profile
    # The repo root is where a portable path is anchored; `state_dir` is always
    # `<root>/state`, so it is derivable rather than another parameter.
    root = Path(state_dir).parent
    defects: list[dict] = []
    corrected = 0
    pruned = 0
    moved = 0
    receipts = read_receipts(state_root)
    for row in receipts:
        if row.get("unreadable"):
            defects.append({"kind": "receipt_unreadable", "at": row.get("at", ""),
                            "detail": row["unreadable"]})
            continue
        if row.get("correction"):
            # A row that predates the fix, disclosed and annotated by
            # `repair_loop_residue`. Counted, never silent — the detector's job
            # is to catch a *new* occurrence of the class.
            corrected += 1
            continue
        if row.get("outcome") != "acted":
            continue
        pack, target = row.get("pack"), row.get("target")
        if pack and target and pack != target:
            defects.append({"kind": "receipt_worked_another_target",
                            "at": row.get("at", ""), "pack": pack, "target": target})
        if pack and not row.get("correction") and not decision_names_pack(row):
            defects.append({"kind": "receipt_served_another_decision",
                            "at": row.get("at", ""), "pack": pack,
                            "decision_served": row.get("decision_served", "")})
        verification = row.get("verification")
        if row.get("verification_pruned"):
            # Recorded as pruned by the repair: history, not an alarm. Anything
            # written after PRUNE_CUTOFF is still checked, so a draft that
            # disappears now still surfaces.
            pruned += 1
        elif verification and resolve_stored_path(verification, root,
                                                 state_dir) is None:
            if _resolve_through_moves(verification, state_root) is not None:
                moved += 1
            else:
                defects.append({"kind": "receipt_verification_missing",
                                "at": row.get("at", ""), "verification": verification})
        if not row.get("fetches") and not row.get("evidence_source"):
            defects.append({"kind": "acted_without_evidence", "at": row.get("at", ""),
                            "pack": pack})
    # A draft with no receipt is the crash-after-draft case: the work is on
    # disk, nothing claims it, and `draft_is_fresh` reads its existence as
    # "done" — so the pack would sit there forever with no record of why.
    receipted = {Path(str(row.get("verification") or "")).name
                 for row in receipts if row.get("outcome") == "acted"}
    drafts_dir = loop_dir(state_root) / "drafts"
    if drafts_dir.is_dir():
        for draft in sorted(drafts_dir.glob("*.md")):
            if draft.name not in receipted:
                defects.append({"kind": "draft_without_receipt",
                                "draft": draft.name})
    # One advert, one pack: duplicates are what made the loop look idle (129
    # redundant directories, 2026-10-13). Reported here so a regression is
    # visible the day it happens, not the week the queue stops moving.
    from application_packs import duplicate_packs, is_superseded

    all_duplicates = duplicate_packs(state_root / "applications")
    # A group is a defect only when it has MORE THAN ONE live pack. The chosen
    # best can be an older ready pack while the newer scaffold is marked
    # (Humand, John Wiley, 2026-11-04); testing packs[1:] flagged that correct
    # state as a defect, and missed a cycle where no pack was live at all.
    live = [(key, packs) for key, packs in all_duplicates
            if sum(1 for p in packs if not is_superseded(p)) > 1]
    if live:
        defects.append({"kind": "pack_duplicates", "groups": len(live),
                        "redundant": sum(len(packs) - 1 for _, packs in live),
                        "already_marked": sum(
                            1 for _, packs in all_duplicates for p in packs
                            if is_superseded(p)),
                        "fix": "application_packs.mark_superseded_duplicates"})
    # The ruling is in code, not in a promise: `loop` must be in the seal
    # contract, or the loop's runs stop being point-in-time the moment somebody
    # trims that tuple (LJ's ruling, 2026-10-11).
    import strata_contract

    if LOOP_DIRNAME not in strata_contract.INCLUDE_TREES:
        defects.append({"kind": "loop_not_sealed",
                        "detail": f"'{LOOP_DIRNAME}' is not in INCLUDE_TREES, so a "
                                  "seal no longer carries the loop's residue"})
    ledger = state_root / "dataset" / "model_calls.jsonl"
    if ledger.is_file():
        stray, _ = stray_tick_rows(state_root)
        if stray:
            defects.append({"kind": "tick_rows_in_the_crawl_ledger", "count": stray,
                            "ledger": str(ledger)})
    return {"profile": profile, "checked": len(receipts),
            "corrected": corrected, "pruned": pruned, "moved": moved,
            "defects": defects}


# ── the tick ─────────────────────────────────────────────────────────────

#: Items the loop may act on. `principal` items are surfaced, never acted on.
ACTIONABLE_AUTHORITIES = ("agent", "both")

def complete_pack(state_root: Path, *, target=None, apply: bool = False,
                 client=None, deadline: float = 0.0, fetcher=None,
                 today: date | None = None, run_id: str = "") -> dict:
    """The whole pack in one context: review, diligence, tailored material.

    `tasks/2026-10-03-pack-completion-workflow.md` is the contract and
    `pack_completion.py` implements it; this is the runner's adapter (LJ,
    2026-10-13: *"we should not stop at investigation and leave the tailoring for
    later … it's a good time to do that then, when things are in context"*).
    """
    import pack_completion

    if target is None:
        try:
            pack_dir = _first_uninvestigated_pack(state_root, today=today)
        except PackQueueUnavailable as exc:
            return {"outcome": "deferred",
                    "why": f"pack queue unreadable, so what is worth doing is "
                           f"unknown: {exc}"}
        if pack_dir is None:
            return {"outcome": "needs_principal",
                    "why": "no pack is pending: the queue is empty"}
    else:
        pack_dir = Path(target)
        if not pack_dir.is_dir():
            return {"outcome": "blocked",
                    "why": f"the decision's pack is not there: {pack_dir}"}
    return pack_completion.complete_pack(
        state_root, state_root.name, target=pack_dir,
        client=client or model_client(), deadline=deadline or time.monotonic() + 900,
        fetcher=fetcher, today=today, run_id=run_id, apply=apply)


#: The actions the loop implements. Adding one means adding a callable here and an
#: action name the decider can emit (ch43 §5).
ACTIONS = {"complete_pack": complete_pack, "investigate_pack": investigate_pack,
           "resolve_advert": resolve_advert, "review_go": review_go}


def tick(profile: str, *, mode: str = "status", apply: bool = False,
         today: date | None = None, state_dir: Path | None = None,
         repo_root: Path | None = None, client=None, fetcher=None,
         now: datetime | None = None, force: bool = False,
         max_actions: int = 1, run_minutes: float | None = None) -> dict:
    """One tick for one principal: the guards, then up to `max_actions` actions.

    **A run is a loop of decisions, not one decision.** LJ, 2026-10-13: *"we are
    resolving only 2 un-prepared GO-s per turn … could we be resolving 20 rather
    than 2? What's a good number?"* Each action is ~75 s, so the per-run count is
    a throughput knob bounded by the daily envelope, the per-run clock and the
    slot the cron gives us. The decision is re-read before every action — the
    pack the last action filled is no longer due, and "no decision, no loop work"
    has to hold for each action, not once per run.
    """
    import decide

    today = today or date.today()
    state_dir = state_dir or config.STATE_DIR
    repo_root = repo_root or config.LOVEWORK_ROOT
    state_root = state_dir / profile
    started = time.monotonic()
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def elapsed() -> dict:
        """Seconds and minutes — the envelope is in minutes, the receipt shows
        both, because labelling 33 seconds as 33 minutes exhausted a 30-minute
        day in one tick (2026-10-11)."""
        seconds = round(time.monotonic() - started, 2)
        return {"seconds": seconds, "minutes": round(seconds / 60, 3)}

    # The cheap guards come before the work (2026-10-24): a windowed tick used
    # to run the repair and the maintenance passes and only then defer, spending
    # ~70 s competing with the sweep it was yielding to. The decision is still
    # read because the receipt reports the due counts, and it is cheap.
    blocked: tuple[str, str] | None = None
    if mode == "act" and not force and not host_allows_acting():
        blocked = ("deferred",
                   f"host rule: the tick acts on {DEFAULT_TICK_HOSTS}, "
                   f"not {os.uname().nodename}")
    else:
        window = in_skip_window(now)
        if window:
            blocked = ("deferred",
                       f"crawl window {window}: the crawl owns this hour")
    if blocked is not None:
        decision = decide.decide(profile, today=today, state_dir=state_dir,
                                 repo_root=repo_root)
        receipt = {"at": at, "profile": profile, "mode": mode, "apply": apply,
                   "due": decision["counts"]["due"],
                   "needs_principal": decision["counts"]["needs_principal"],
                   "earliest": decision["earliest"], "outcome": blocked[0],
                   "why": blocked[1], "action": None, "effect": None,
                   "calls": 0, "actions": 0,
                   "max_actions": max(1, int(max_actions)),
                   "run_minutes": run_minutes}
        receipt.update(elapsed())
        _write_heartbeat(state_root, receipt)
        return receipt

    # Heal before adding: the first live run's residue is in the repository and
    # a host merge can bring it back, so the repair is idempotent and runs on
    # every tick rather than when somebody remembers.
    repair = repair_loop_residue(profile, state_dir=state_dir)
    repaired = {k: v for k, v in repair.items()
                if k not in ("dry_run", "principal") and v}

    # The morning's cheap passes ride the hourly tick (LJ, 2026-10-14: "the old
    # 6am-7am runs don't make much sense … transfer the functionality into the
    # regular hourly runs"). The expensive ones — self-heal at ~2 minutes, the
    # memory coarsening's model calls — run once a day by their own marker.
    import maintenance

    # The single-shot tick has its own clock too; maintenance shares it here as it
    # does in the slot (2026-10-19).
    single_deadline = (started + run_minutes * 60) if run_minutes else None
    upkept = maintenance.run(profile, state_dir=state_dir, repo_root=repo_root,
                             today=today, deadline=single_deadline)

    decision = decide.decide(profile, today=today, state_dir=state_dir,
                             repo_root=repo_root)
    receipt = {"at": at, "profile": profile, "mode": mode, "apply": apply,
               "due": decision["counts"]["due"],
               "needs_principal": decision["counts"]["needs_principal"],
               "earliest": decision["earliest"], "outcome": "quiet",
               "action": None, "effect": None, "calls": 0, "actions": 0,
               "max_actions": max(1, int(max_actions)),
               "run_minutes": run_minutes}
    if repaired:
        receipt["repair"] = repaired
    if any(upkept.get(key) for key in ("ran", "failed", "deferred", "alarms")):
        receipt["maintenance"] = {k: v for k, v in upkept.items() if v}

    def stop(outcome: str, why: str, **extra) -> dict:
        receipt["outcome"] = outcome
        receipt["why"] = why
        receipt.update(extra)
        receipt.update(elapsed())
        _write_heartbeat(state_root, receipt)
        return receipt

    if not decision["due"]:
        return stop("quiet", "nothing due")
    if mode == "status":
        return stop("surfaced",
                    "status mode: the decider answered, nothing was acted on")
    if mode != "act":
        return stop("quiet", f"mode {mode}: nothing to do")

    # One tick at a time: with `--max-actions` above one a run can last tens of
    # minutes, and two overlapping runs would investigate the same pack twice.
    try:
        with tick_run_lock(state_dir):
            receipt = _run_actions(profile, state_dir=state_dir, repo_root=repo_root,
                                   state_root=state_root, receipt=receipt, today=today,
                                   apply=apply, client=client, fetcher=fetcher,
                                   now=now, force=force, started=started,
                                   elapsed=elapsed, decide=decide)
    except AnotherTickRunning:
        return stop("deferred", "another tick holds the run lock")
    # **Every tick that ends writes its heartbeat, whatever it concluded** (2026-10-20).
    # `_run_actions` returns from several places — `needs_principal` above all — and
    # this is the one door all of them leave by. Measured the cost of missing it: the
    # hourly cron ran three principals and wrote *no* heartbeat at all, so the job
    # looked green and the liveness record was six hours stale — "quiet" and "broken"
    # indistinguishable again, which is the exact failure the heartbeat was built for.
    _write_heartbeat(state_root, receipt)
    return receipt


def _pick_actionable(decision: dict, state_root: Path, today: date, *,
                     apply: bool, blocked: set[str],
                     worked: set[str] | None = None) -> list[dict]:
    """The due items a runner can do *now*, in the decider's rank order.

    One reader for both runners — the single-action tick and the draining slot
    — so the two cannot disagree about what counts as doable. `worked` holds
    the packs this run has already completed once: a pack whose completion
    landed only partially (say the review, not the material) stays in the
    queue, and without this set the run would re-pick it forever — the
    intra-run half of the livelock the empty-block intersection used to
    prevent (2026-10-19, the day the decider began naming the whole queue).
    The queue itself re-decides on the next run, so partial progress still
    accumulates — one chunk per run, not a spin.
    """
    return [i for i in decision["due"]
            if i["authority"] in ACTIONABLE_AUTHORITIES
            and i["action"] in ACTIONS
            and str(i.get("target") or "") not in blocked
            and str(i.get("target") or "") not in (worked or set())
            and action_can_do(i, state_root, today, apply=apply)]


def _no_actionable_why(decision: dict) -> str:
    """Why nothing due is doable — the same words for both runners."""
    owned = [i for i in decision["due"]
             if i["authority"] in ACTIONABLE_AUTHORITIES
             and i["action"] in ACTIONS]
    if owned:
        return (f"{len(owned)} item(s) the agent owns are not doable now "
                "(already drafted or reviewed, or no advert link to fetch "
                "against)")
    return (f"{decision['counts']['due']} due, none the agent owns"
            if decision["counts"]["due"] else "nothing actionable")


def _run_one_action(profile: str, *, state_dir: Path, repo_root: Path,
                    state_root: Path, today: date, apply: bool, client,
                    fetcher, decision: dict, item: dict, at: str, run_id: str,
                    action_started: float, run_deadline: float = 0.0,
                    run_started: float | None = None) -> tuple[dict, dict]:
    """Execute exactly ONE decision item and leave its receipt row behind.

    The row is finalised, spent and appended here; the *policy* — bounds,
    guards, queue re-reads, fairness between principals — belongs to the
    caller. Both runners share this one body so a receipt cannot mean
    different things depending on which runner wrote it (the "two paths, one
    updated" defect this loop has already paid for twice).

    Returns `(row, result)`: the receipt row as appended, and the action's own
    result dict (`outcome` is the vocabulary the caller switches on).
    """
    # The action's **own** duration, not the run's elapsed time. Charging each
    # action the run's cumulative seconds made a six-action run spend six
    # times its real duration: lj's day was charged 162 minutes for ~55
    # minutes of work, and since the daily envelope is enforced in minutes
    # the loop stopped hours before it needed to (measured 2026-10-13).
    row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "profile": profile, "mode": "act", "apply": apply,
           "due": decision["counts"]["due"],
           "needs_principal": decision["counts"]["needs_principal"],
           "earliest": decision["earliest"], "outcome": "quiet",
           "action": None, "effect": None, "calls": 0,
           # The target travels from the decision to the action. Without
           # this the action chose its own pack and the receipt named a
           # decision the tick had not served — false for every tick of the
           # first live run (2026-10-11).
           "target": item.get("target"),
           "decision_served": item["decision"],
           # Which runner wrote this row (slot/postcrawl/tick): the audit and
           # the receipts' readers can tell a draining run from a single shot.
           "run_id": run_id}
    left = budget_left(state_root, today)
    # The envelope bounds this action; the run's own clock may bound it harder
    # (a slot must not start a fetch and a model call it cannot finish) — and a
    # **per-action ceiling** bounds it hardest.
    #
    # Without the ceiling one slow action eats the whole run: measured 2026-10-19, a
    # `complete_pack` sat in a socket poll (`do_poll`) holding `tick.lock` and the
    # 40-minute slot, so an hourly tick with `--max-actions 3` produced one action's
    # worth of work in an hour — and the receipt could not say so, because it carried
    # no duration. The rule the class states: **a bounded run must bound each step
    # inside it**, or the bound describes an intention rather than a limit.
    deadline = action_deadline(run_deadline, left["minutes_left"])
    started_action = time.monotonic()
    try:
        result = ACTIONS[item["action"]](
            state_root, target=item.get("target"), apply=apply,
            client=client or model_client(), deadline=deadline,
            fetcher=fetcher, today=today, run_id=run_id)
    except Exception as exc:                # never silently swallow
        if _is_provider_outage(exc):
            # The provider is down (or throttling us): defer, do not fail. The
            # receipt and the tick report name the outage; the next hour retries.
            result = {"outcome": "deferred",
                      "why": f"provider unavailable: {type(exc).__name__}: {exc}"[:300],
                      "error": f"{type(exc).__name__}: {exc}"}
        else:
            result = {"outcome": "failed", "error": f"{type(exc).__name__}: {exc}"}
    row.update(result)
    # …and how long it took, so "which action ate the slot" is answerable from the
    # receipt instead of from `ps`. The row is the record; a duration is part of it.
    row.setdefault("seconds", round(time.monotonic() - started_action, 1))
    row["action"] = item["action"]
    row["calls"] = 1 if result.get("outcome") == "acted" else 0
    row["seconds"] = round(time.monotonic() - action_started, 2)
    row["minutes"] = round(row["seconds"] / 60, 3)
    if run_started is not None:
        # Context, never charged: computed after the action, so it covers it.
        row["run_seconds"] = round(time.monotonic() - run_started, 2)
    _finalise_row(row, repo_root)
    if result.get("outcome") == "acted":
        spend(state_root, today, calls=row["calls"], minutes=row["minutes"])
        append_receipt(state_root, row)
    else:
        if result.get("outcome") == "failed":
            # A failed attempt still spent a call and the wall clock. Counting
            # it only on success would let a retry loop run unbounded.
            spend(state_root, today, calls=1, minutes=row["minutes"])
        if result.get("contended") or result.get("outcome") == "failed":
            # A lease held elsewhere is a deferral with its own exit code (75),
            # the house vocabulary — never a silent write and never a failure.
            append_receipt(state_root, row)
    return row, result


def _run_actions(profile: str, *, state_dir: Path, repo_root: Path,
                 state_root: Path, receipt: dict, today: date, apply: bool,
                 client, fetcher, now, force: bool, started: float,
                 elapsed, decide) -> dict:
    """The action loop of one tick: up to `receipt["max_actions"]` actions.

    Each action is receipted on its own row — one row per action, so every action
    keeps its own verification and the audit's per-row checks still mean
    something — and the returned receipt is the run's summary. The body of one
    action lives in `_run_one_action`, shared with the slot runner below.
    """
    done: list[dict] = []
    blocked: set[str] = set()
    worked: set[str] = set()
    run_minutes = receipt.get("run_minutes")
    for _step in range(receipt["max_actions"]):
        if run_minutes is not None and (time.monotonic() - started) / 60 >= run_minutes:
            receipt["stopped_because"] = f"run clock: {run_minutes} minutes"
            break
        if not force:
            # Re-checked per action, not once per run: a crawl that starts
            # mid-run must stop the loop, and a job that overran its hours is
            # exactly what a window cannot see (LJ, 2026-10-11).
            busy = lease_holder(state_root) or busy_processes()
            if busy:
                receipt["lock"] = busy
                if not done:
                    receipt["outcome"] = "deferred"
                    receipt["why"] = f"busy: {busy}"
                    receipt.update(elapsed())
                    return receipt
                receipt["stopped_because"] = f"busy: {busy}"
                break
        left = budget_left(state_root, today)
        if left["exhausted"]:
            if not done:
                receipt["outcome"] = "deferred"
                receipt["why"] = (f"daily envelope spent ({left['bound']} bound: "
                                  f"{DEFAULT_DAILY_CALLS} calls / "
                                  f"{DEFAULT_DAILY_MINUTES} minutes)")
                receipt.update(elapsed())
                return receipt
            receipt["stopped_because"] = "daily envelope spent"
            break

        # Re-read the decision: the world moved under the last action.
        decision = decide.decide(profile, today=today, state_dir=state_dir,
                                 repo_root=repo_root)
        receipt["due"] = decision["counts"]["due"]
        receipt["needs_principal"] = decision["counts"]["needs_principal"]
        receipt["earliest"] = decision["earliest"]
        actionable = _pick_actionable(decision, state_root, today,
                                      apply=apply, blocked=blocked, worked=worked)
        if not actionable:
            why = _no_actionable_why(decision)
            if not done:
                receipt["outcome"] = "needs_principal"
                receipt["why"] = why
                receipt.update(elapsed())
                return receipt
            receipt["stopped_because"] = why
            break

        item = actionable[0]
        action_started = time.monotonic()
        row, result = _run_one_action(
            profile, state_dir=state_dir, repo_root=repo_root,
            state_root=state_root, today=today, apply=apply, client=client,
            fetcher=fetcher, decision=decision, item=item, at=receipt["at"],
            run_id=f"tick-{profile}-{receipt['at'][:19].replace(':', '')}",
            action_started=action_started, run_started=started)
        done.append(row)
        # **The liveness record is written as the work happens, not only when the run
        # ends** (2026-10-20). A tick is killed at its wall clock by the wrapper's
        # `timeout` when a slot is too small — and a kill before the final write loses the
        # heartbeat entirely, which is exactly when it matters: measured, lj's heartbeat
        # was **7 hours stale while its receipts showed 29 acted actions**, because its
        # 15-minute slot ran out mid-run every hour. A record that survives the crash it
        # documents is the only kind worth having.
        if result.get("outcome") in ("acted", "failed", "blocked"):
            receipt.update({"actions": len([r for r in done
                                            if r.get("outcome") == "acted"]),
                            "last_action": result.get("outcome"),
                            "last_action_at": row.get("at")})
            # Mid-run: the heartbeat answers "did it act recently?"; the report
            # gets one section when the run closes, not one per action.
            _write_heartbeat(state_root, receipt, report=False)
        if result.get("outcome") == "acted" and item["action"] == "complete_pack":
            # Once per run per pack: a partially-landed completion stays in
            # the queue, and the run must move on to the next pack rather
            # than spin (the queue re-decides next run).
            worked.add(str(item.get("target") or ""))
        if result.get("outcome") == "blocked":
            # A blocked pack must not stall the run: an advert whose page will not
            # fetch is blocked *permanently*, and if it is the newest pending pack
            # it would stall every run from here on (measured 2026-10-13: a
            # digitaljobs URL that neither provider could fetch stood at the head
            # of lj's queue). Skip it, keep going, and say so.
            blocked.add(str(item.get("target") or ""))
            receipt["blocked"] = sorted(b for b in blocked if b)
            continue
        if result.get("outcome") == "failed":
            receipt["stopped_because"] = f"failed: {result.get('error', '')}"
            break
        if result.get("outcome") != "acted":
            if len(done) == 1:
                receipt.update(result)
                receipt["action"] = item["action"]
                receipt["calls"] = row["calls"]
                receipt.update(elapsed())
                return receipt
            receipt["stopped_because"] = f"last action: {result.get('outcome')}"
            break

    # The run's summary. `outcome` is the last action's, so a run that ended in
    # a refusal says so; `actions` is how many it did.
    acted = [r for r in done if r.get("outcome") == "acted"]
    receipt["actions"] = len(acted)
    receipt["calls"] = sum(r.get("calls", 0) for r in done)
    if acted:
        last = acted[-1]
        receipt["outcome"] = "acted"
        for key in ("action", "effect", "pack", "verification", "target",
                    "decision_served"):
            receipt[key] = last.get(key)
        receipt["packs"] = [r.get("pack") for r in acted]
        # …and the last action's detail (fetches, confidence, unknown) so the
        # summary answers the same questions a single-action receipt did. The
        # summary's own fields win: `calls` is the run's total.
        for key, value in last.items():
            receipt.setdefault(key, value)
    else:
        last = done[-1] if done else None
        receipt["outcome"] = (last or {}).get("outcome", "quiet")
        for key, value in (last or {}).items():
            receipt.setdefault(key, value)
    if blocked:
        # Always visible in the summary, whatever path the loop left by.
        receipt["blocked"] = sorted(b for b in blocked if b)
    receipt.update(elapsed())
    # A multi-action run leaves one row per action *and* one summary row, because
    # "why did this run do three and not six?" is a question about the run, not
    # about any action in it (the per-action rows cannot answer it: measured
    # 2026-10-13, vj and kj each did 3 of 6 with 10 doable items in the decider).
    # `outcome: "run"` is not `acted`, so the audit's per-row checks ignore it.
    if len(done) > 1:
        summary = {"at": receipt["at"], "profile": profile, "mode": "act",
                   "apply": apply, "outcome": "run",
                   "actions": receipt["actions"], "calls": receipt["calls"],
                   "seconds": receipt["seconds"], "minutes": receipt["minutes"],
                   "due": receipt.get("due"),
                   "needs_principal": receipt.get("needs_principal"),
                   "stopped_because": receipt.get("stopped_because", ""),
                   "packs": receipt.get("packs", [])}
        append_receipt(state_root, summary)
    return receipt


# ── the slot: the draining form of the run (LJ, 2026-10-18) ──────────────

def slot_deadline(now: datetime | None = None, *,
                  anchor: float | None = None) -> float:
    """This slot's monotonic deadline: the next hourly boundary, less margin.

    The tick cron fires at :05; a run started at :05:30 owns the GPU until the
    next :05 approaches (less `SLOT_MARGIN_MINUTES`, so the next tick starts
    with the run lock free and the receipts already written). The slot, not a
    fixed action count, is the quantum — the kernel-scheduler rule.
    """
    now = now or datetime.now()
    anchor = time.monotonic() if anchor is None else anchor
    boundary = now.replace(minute=SLOT_BOUNDARY_MINUTE % 60, second=0,
                           microsecond=0)
    if (now - boundary).total_seconds() >= 0:
        boundary += timedelta(hours=1)
    return anchor + max((boundary - now).total_seconds()
                        - SLOT_MARGIN_MINUTES * 60, 0.0)


def _wait_out_busy(state_root: Path, seconds: float, *,
                   step: float = BUSY_WAIT_STEP_SECONDS) -> str:
    """Sleep in steps until the busy signal clears, or the budget is spent.

    Returns the last busy signal ("" when it cleared). The post-crawl pass
    only: an hourly tick *yields* a busy host to the next tick, but nothing
    comes behind a post-crawl run, so it waits — bounded, and it reports the
    wait it could not outlast rather than ending a five-hour pass over a
    thirty-second watchdog (the Saturday-night jobs, 22:30/22:35).
    """
    busy = lease_holder(state_root) or busy_processes()
    deadline = time.monotonic() + max(seconds, 0.0)
    while busy and time.monotonic() < deadline:
        time.sleep(step)
        busy = lease_holder(state_root) or busy_processes()
    return busy


def _close_slot_principal(st: dict, *, started: float,
                          stopped_because: str | None = None) -> None:
    """Fold one principal's slot work into its receipt + heartbeat + summary.

    The same summary shape `_run_actions` writes, so a slot receipt answers the
    questions a tick receipt does (and the audit's per-row checks keep their
    meaning: the summary row is `outcome: "run"`, which they ignore).
    """
    receipt, done = st["receipt"], st["done"]
    if stopped_because is not None:
        receipt["stopped_because"] = stopped_because
    acted = [r for r in done if r.get("outcome") == "acted"]
    receipt["actions"] = len(acted)
    receipt["calls"] = sum(r.get("calls", 0) for r in done)
    if acted:
        receipt["outcome"] = "acted"
        last = acted[-1]
        for key in ("action", "effect", "pack", "verification", "target",
                    "decision_served"):
            receipt[key] = last.get(key)
        receipt["packs"] = [r.get("pack") for r in acted]
        for key, value in last.items():
            receipt.setdefault(key, value)
    elif done:
        last = done[-1]
        receipt["outcome"] = last.get("outcome", "quiet")
        for key, value in last.items():
            receipt.setdefault(key, value)
    # `done` empty: the outcome/why the guard already wrote stand.
    if st["blocked"]:
        receipt["blocked"] = sorted(b for b in st["blocked"] if b)
    receipt["seconds"] = round(time.monotonic() - started, 2)
    receipt["minutes"] = round(receipt["seconds"] / 60, 3)
    if not receipt.get("why") and receipt.get("stopped_because"):
        # The heartbeat answers "why this outcome"; a run that ended on its
        # clock or a guard has its reason in stopped_because, and the
        # watchdog's history should read it there as `why`.
        receipt["why"] = receipt["stopped_because"]
    if len(done) > 1:
        summary = {"at": receipt["at"], "profile": receipt["profile"],
                   "mode": "act", "apply": receipt.get("apply"),
                   "runner": receipt.get("runner"), "outcome": "run",
                   "actions": receipt["actions"], "calls": receipt["calls"],
                   "seconds": receipt["seconds"], "minutes": receipt["minutes"],
                   "due": receipt.get("due"),
                   "needs_principal": receipt.get("needs_principal"),
                   "stopped_because": receipt.get("stopped_because", ""),
                   "packs": receipt.get("packs", [])}
        append_receipt(st["state_root"], summary)
    _write_heartbeat(st["state_root"], receipt)


def tick_slot(profiles: list[str], *, mode: str = "act", apply: bool = False,
              today: date | None = None, state_dir: Path | None = None,
              repo_root: Path | None = None, client=None, fetcher=None,
              now: datetime | None = None, force: bool = False,
              post_crawl: bool = False, max_actions: int = 0,
              run_minutes: float | None = None,
              deadline: float | None = None) -> list[dict]:
    """The draining run (LJ, 2026-10-18): keep processing the queue until it is
    empty or the slot is about to expire; then stop and yield the time to the
    next tick — *"like scheduling the cpu by the os kernel scheduler."*

    Principals take turns, **one action per principal per cycle**, so a
    principal with a deep queue cannot starve the others and a quiet principal
    costs nothing. Maintenance and repair run once per principal per slot (the
    hourly cadence the cheap passes already assume), not once per action.

    `post_crawl=True` (the crawl wrapper's invitation after a sealed sweep)
    keeps every lock and lease guard but drops the skip-window check — the
    crawl is demonstrably done — and waits out (bounded) a busy host instead
    of yielding to a next tick that does not exist behind it.
    """
    import decide
    import maintenance

    today = today or date.today()
    state_dir = state_dir or config.STATE_DIR
    repo_root = repo_root or config.LOVEWORK_ROOT
    now = now or datetime.now()
    started = time.monotonic()
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if deadline is None:
        if run_minutes is not None:
            deadline = started + run_minutes * 60
        elif post_crawl:
            deadline = started + POST_CRAWL_MINUTES * 60
        else:
            deadline = slot_deadline(now, anchor=started)
    runner = "post-crawl" if post_crawl else "slot"

    # ── per-principal prepare: repair, maintenance, decide, the cheap guards ──
    active: dict[str, dict] = {}
    receipts: list[dict] = []
    for profile in profiles:
        state_root = state_dir / profile
        receipt = {"at": at, "profile": profile, "mode": mode, "apply": apply,
                   "runner": runner, "outcome": "quiet", "action": None,
                   "effect": None, "calls": 0, "actions": 0,
                   "max_actions": max_actions, "run_minutes": run_minutes}
        receipts.append(receipt)

        def defer(outcome: str, why: str) -> None:
            receipt["outcome"] = outcome
            receipt["why"] = why
            receipt["seconds"] = round(time.monotonic() - started, 2)
            receipt["minutes"] = round(receipt["seconds"] / 60, 3)
            _write_heartbeat(state_root, receipt)

        # Ask the cheap guards before the hour's work (2026-10-24). A windowed
        # tick used to run repair + maintenance and only then defer: measured
        # ~70 s per tick, spent competing with the sweep it was yielding to.
        # The decision is still read because the report line carries the due
        # counts, and it is cheap (lj 4.7 s, vj 2.8 s, kj 1.5 s on 2026-10-24).
        host_blocked = mode == "act" and not force and not host_allows_acting()
        window = "" if post_crawl else in_skip_window(now)
        if host_blocked or window:
            decision = decide.decide(profile, today=today, state_dir=state_dir,
                                     repo_root=repo_root)
            receipt["due"] = decision["counts"]["due"]
            receipt["needs_principal"] = decision["counts"]["needs_principal"]
            receipt["earliest"] = decision["earliest"]
            if host_blocked:
                defer("deferred",
                      f"host rule: the tick acts on {DEFAULT_TICK_HOSTS}, "
                      f"not {os.uname().nodename}")
            else:
                defer("deferred",
                      f"crawl window {window}: the crawl owns this hour")
            continue

        repair = repair_loop_residue(profile, state_dir=state_dir)
        repaired = {k: v for k, v in repair.items()
                    if k not in ("dry_run", "principal") and v}
        if repaired:
            receipt["repair"] = repaired
        # The maintenance phase shares the slot's clock (2026-10-19): it runs before
        # the actions, so an unbounded self-heal could spend the whole slot here and
        # leave the queue untouched — which is also how a run overran its wrapper's
        # `timeout` and was killed (exit 124, three runs in a row).
        upkept = maintenance.run(profile, state_dir=state_dir,
                                 repo_root=repo_root, today=today,
                                 deadline=deadline)
        if any(upkept.get(key) for key in ("ran", "failed", "deferred", "alarms")):
            receipt["maintenance"] = {k: v for k, v in upkept.items() if v}
        decision = decide.decide(profile, today=today, state_dir=state_dir,
                                 repo_root=repo_root)
        receipt["due"] = decision["counts"]["due"]
        receipt["needs_principal"] = decision["counts"]["needs_principal"]
        receipt["earliest"] = decision["earliest"]

        if not decision["due"]:
            defer("quiet", "nothing due")
            continue
        if mode == "status":
            defer("surfaced",
                  "status mode: the decider answered, nothing was acted on")
            continue
        if mode != "act":
            defer("quiet", f"mode {mode}: nothing to do")
            continue
        active[profile] = {"profile": profile, "state_root": state_root,
                           "receipt": receipt, "decision": decision,
                           "done": [], "blocked": set(), "worked": set()}

    if active and mode == "act":
        # An unreachable relay (network, expired key, quota) owes its caller one
        # honest deferral, not a queue's worth of failed actions.
        ok, detail = model_reachable()
        if not ok:
            for st in active.values():
                st["receipt"]["outcome"] = "deferred"
                st["receipt"]["why"] = f"model unreachable: {detail}"
                _write_heartbeat(st["state_root"], st["receipt"])
            active.clear()

    if active:
        # One run lock for the whole slot: the round-robin holds it, so an
        # hourly tick that fires mid-slot defers to the work already in
        # flight (its maintenance and briefs run before it takes this lock).
        lock_wait = POST_CRAWL_BUSY_WAIT_MINUTES * 60 if post_crawl else 0.0
        lock_deadline = time.monotonic() + lock_wait
        while True:
            try:
                with tick_run_lock(state_dir):
                    _drain_slot(active, profiles_order=list(active), today=today,
                                state_dir=state_dir, repo_root=repo_root,
                                apply=apply, client=client, fetcher=fetcher,
                                force=force, post_crawl=post_crawl,
                                max_actions=max_actions, deadline=deadline,
                                started=started, at=at, decide=decide)
                break
            except AnotherTickRunning:
                if time.monotonic() >= lock_deadline:
                    for st in active.values():
                        st["receipt"]["outcome"] = "deferred"
                        st["receipt"]["why"] = "another tick holds the run lock"
                        _write_heartbeat(st["state_root"], st["receipt"])
                    active.clear()
                    break
                time.sleep(BUSY_WAIT_STEP_SECONDS)

    yield_why = ("post-crawl clock spent" if post_crawl
                 else "slot: yielded the time to the next tick")
    for st in active.values():
        _close_slot_principal(st, started=started, stopped_because=yield_why)

    _refresh_boards(receipts, state_dir=state_dir)
    return receipts


#: The tier rank of a principal's next actionable item: GO pack first, then MAYBE pack,
#: then everything else. Read from the decision the slot already holds — no extra work.
TIER_KINDS = ("go_pack", "maybe_pack")


def _tier_rank(state: dict | None) -> int:
    """0 for a pending GO pack, 1 for a MAYBE pack, 2 for anything else."""
    if not state:
        return len(TIER_KINDS)
    kinds = [item.get("kind") for item in (state.get("decision") or {}).get("due", [])]
    for rank, kind in enumerate(TIER_KINDS):
        if kind in kinds:
            return rank
    return len(TIER_KINDS)


def _drain_slot(active: dict[str, dict], *, profiles_order: list[str],
                today: date, state_dir: Path, repo_root: Path, apply: bool,
                client, fetcher, force: bool, post_crawl: bool,
                max_actions: int, deadline: float, started: float, at: str,
                decide) -> int:
    """The slot's inner loop: round-robin one action per principal per cycle.

    Runs under the run lock. Deactivates a principal (closing its receipt)
    when its queue is empty, its envelope is spent, a guard stops it, or a
    non-acted outcome ends its turn; the slot itself ends when every principal
    is closed, the deadline passes, or `max_actions` acted actions are done.
    Returns the number of actions acted.
    """
    total_acted = 0
    while active and time.monotonic() < deadline:
        if max_actions and total_acted >= max_actions:
            break
        cycled = False
        # **Tier before principal** (LJ, 2026-10-20: *"Have ordering GO {vj,kj,lj}, then
        # MAYBE {vj,kj,lj}"*). The round-robin alone gives vj-GO, vj-MAYBE, kj-GO … — the
        # opposite of the ruling when an hour has room for two actions. Ranking the
        # principals by the tier of the item each would take next keeps the priority order
        # *inside* a tier (the list is already in `PRINCIPAL_ORDER`) and puts every GO ahead
        # of every MAYBE.
        for profile in sorted(profiles_order, key=lambda name: _tier_rank(active.get(name))):
            st = active.get(profile)
            if st is None:
                continue                        # already closed this slot
            if time.monotonic() >= deadline:
                break
            if max_actions and total_acted >= max_actions:
                break
            state_root = st["state_root"]
            receipt = st["receipt"]
            if not force:
                # Re-checked per action, not once per slot: a crawl that starts
                # mid-slot must stop the loop (LJ, 2026-10-11).
                busy = lease_holder(state_root) or busy_processes()
                if busy and post_crawl:
                    busy = _wait_out_busy(state_root,
                                          POST_CRAWL_BUSY_WAIT_MINUTES * 60)
                if busy:
                    receipt["lock"] = busy
                    if not st["done"]:
                        receipt["outcome"] = "deferred"
                        receipt["why"] = f"busy: {busy}"
                    del active[profile]
                    _close_slot_principal(st, started=started,
                                          stopped_because=None
                                          if not st["done"]
                                          else f"busy: {busy}")
                    continue
            left = budget_left(state_root, today)
            if left["exhausted"]:
                if not st["done"]:
                    receipt["outcome"] = "deferred"
                    receipt["why"] = (
                        f"daily envelope spent ({left['bound']} bound: "
                        f"{DEFAULT_DAILY_CALLS} calls / "
                        f"{DEFAULT_DAILY_MINUTES} minutes)")
                del active[profile]
                _close_slot_principal(st, started=started,
                                      stopped_because=None
                                      if not st["done"]
                                      else "daily envelope spent")
                continue

            # Re-read the decision: the world moved under the last action.
            decision = decide.decide(profile, today=today, state_dir=state_dir,
                                     repo_root=repo_root)
            receipt["due"] = decision["counts"]["due"]
            receipt["needs_principal"] = decision["counts"]["needs_principal"]
            receipt["earliest"] = decision["earliest"]
            actionable = _pick_actionable(decision, state_root, today,
                                          apply=apply, blocked=st["blocked"],
                                          worked=st["worked"])
            if not actionable:
                why = _no_actionable_why(decision)
                if not st["done"]:
                    receipt["outcome"] = "needs_principal"
                    receipt["why"] = why
                del active[profile]
                _close_slot_principal(st, started=started,
                                      stopped_because=None
                                      if not st["done"] else why)
                continue

            item = actionable[0]
            action_started = time.monotonic()
            row, result = _run_one_action(
                profile, state_dir=state_dir, repo_root=repo_root,
                state_root=state_root, today=today, apply=apply, client=client,
                fetcher=fetcher, decision=decision, item=item, at=at,
                run_id=f"{'postcrawl' if post_crawl else 'slot'}-{profile}-"
                       f"{at[:19].replace(':', '')}",
                action_started=action_started, run_deadline=deadline,
                run_started=started)
            st["done"].append(row)
            if result.get("outcome") == "acted":
                total_acted += 1
                if item["action"] == "complete_pack":
                    # Once per run per pack, same rule as the single-action
                    # tick: partial completions get their next chunk next run.
                    st["worked"].add(str(item.get("target") or ""))
                continue          # this principal stays in the rotation
            if result.get("outcome") == "blocked":
                # The same rule the single-action tick follows: a permanently
                # blocked pack must not stall the rotation.
                st["blocked"].add(str(item.get("target") or ""))
                continue
            # deferred / needs_principal / failed end this principal's slot.
            del active[profile]
            _close_slot_principal(st, started=started,
                                  stopped_because=f"last action: "
                                  f"{result.get('outcome')}")
    return total_acted


def _refresh_boards(receipts: list[dict], *, state_dir: Path) -> None:
    """Regenerate each acted principal's refined-GO board (a projection).

    A failure is reported on the receipt, never swallowed: the board is the
    run's most readable output and its absence must be visible, but it must
    not fail a run whose actions already completed.
    """
    from hone_board import write_board

    for receipt in receipts:
        state_root = state_dir / receipt["profile"]
        if not (state_root / "wiki").is_dir():
            continue
        try:
            path = write_board(state_root, receipt["profile"])
            receipt["board"] = portable_path(path, state_dir.parent.parent)
        except Exception as exc:
            receipt["board_error"] = f"{type(exc).__name__}: {exc}"


def _finalise_row(row: dict, repo_root: Path) -> None:
    """Portable paths on the row that travels between hosts by git."""
    for field in ("pack", "target", "verification"):
        if row.get(field):
            row[field] = portable_path(row[field], repo_root)


def _line(receipt: dict) -> str:
    parts = [f"{receipt['profile']}: {receipt['outcome']}",
             f"due {receipt['due']}"]
    if receipt["needs_principal"]:
        parts.append(f"needs principal: {receipt['needs_principal']}")
    # How many actions this run did: with `--max-actions` above one, "acted" is
    # not enough to know whether the run did one pack or six.
    if receipt.get("actions", 0) > 1:
        parts.append(f"{receipt['actions']} actions")
    if receipt.get("action"):
        parts.append(receipt["action"])
    if receipt.get("effect"):
        parts.append(str(receipt["effect"])[:60])
    if receipt.get("why"):
        parts.append(str(receipt["why"])[:80])
    if receipt.get("error"):
        parts.append(f"ERROR {receipt['error'][:80]}")
    return " · ".join(parts)


def _all_principals() -> list:
    """The principals with state, in the declared priority order — one authority.

    Five call sites held their own `["lj", "vj", "kj"]` — the receipts audit, the
    residue repair, the retire CLI, the state email and apply-by-email — so LJ's
    2026-10-19 re-ordering (vj, kj, lj) reached none of them, and the state email
    would have drained lj first on the very evening the rule about lj's packs
    mattered.
    """
    return list(config.principal_names(config.STATE_DIR))


def _pin_dataset(profile: str) -> None:
    """File this process's model calls under the principal it is ticking.

    A tick knows whose queue it drains, and the wrapper runs one process per
    principal — but the recorder's fallback is `config.DATASET_DIR`, which is lj's,
    so vj's and kj's calls were attributed to lj with `run_id: unattributed`
    (measured 2026-10-21). No directory, no pin: an unknown profile keeps the
    fallback rather than inventing a ledger.
    """
    dataset = config.STATE_DIR / profile / "dataset"
    if not dataset.is_dir():
        return
    import model_transcripts

    model_transcripts.set_active_dataset(dataset)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj",
                        help="a principal, or `all` to cycle every one")
    parser.add_argument("--mode", default="status", choices=("status", "act"))
    parser.add_argument("--apply", action="store_true",
                        help="write effects (default: draft only)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--today", default=None, help="YYYY-MM-DD (testing)")
    parser.add_argument("--audit", action="store_true",
                        help="check the receipts against what they claim, then exit")
    parser.add_argument("--repair", action="store_true",
                        help="repair the loop's residue in place, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --repair: say what would change, change nothing")
    parser.add_argument("--force", action="store_true",
                        help="act on a host the loop is not assigned to")
    parser.add_argument("--max-actions", type=int, default=0,
                        help="actions per principal per run; N>=1 is the "
                             "single-shot form. 0 (the default) is SLOT MODE: "
                             "drain the queue until empty or the hourly slot "
                             "ends, round-robin across principals")
    parser.add_argument("--run-minutes", type=float, default=None,
                        help="wall-clock budget for the whole run")
    parser.add_argument("--post-crawl", action="store_true",
                        help="the crawl wrapper's after-seal pass: no skip-window "
                             "check (the crawl is done), a longer clock "
                             "(LOVEWORK_TICK_POST_CRAWL_MINUTES), busy is waited "
                             "out rather than yielded to")
    parser.add_argument("--board", action="store_true",
                        help="regenerate the refined-GO board (loop/hone-board.md) "
                             "for each principal, then exit")
    parser.add_argument("--report", action="store_true",
                        help="print today's continual tick report "
                             "(state/<p>/loop/tick-report.md) and exit")
    args = parser.parse_args(argv)

    # **Whose ledger do the model calls belong to?** The recorder falls back to
    # `config.DATASET_DIR` (lj), so vj's and kj's loop calls were landing in lj's
    # `model_calls.jsonl` with `run_id: unattributed` (measured 2026-10-21). The
    # wrapper runs one process per principal, so one pin is enough; `--profile all`
    # (a single round-robin process) keeps the fallback.
    if args.profile != "all":
        _pin_dataset(args.profile)
    config.set_active_profile(args.profile)

    if args.report:
        for profile in (_all_principals() if args.profile == "all" else [args.profile]):
            path = loop_dir(config.STATE_DIR / profile) / TICK_REPORT_NAME
            if path.is_file():
                print(path.read_text(encoding="utf-8").rstrip() + "\n")
            else:
                print(f"{profile}: no tick report yet ({path})\n")
        return 0

    today = date.fromisoformat(args.today) if args.today else None
    if args.repair:
        for profile in (_all_principals() if args.profile == "all" else [args.profile]):
            outcome = repair_loop_residue(profile, dry_run=args.dry_run)
            print(f"{profile}: {outcome}")
        return 0
    if args.audit:
        # A detector, not an authority: it reports and exits non-zero, and the
        # tick itself never refuses work because the past is inconsistent.
        reports = []
        for profile in (_all_principals() if args.profile == "all" else [args.profile]):
            reports.append(audit_receipts(profile))
        if args.json:
            print(json.dumps(reports, indent=2, sort_keys=True))
        else:
            for report in reports:
                line = (f"{report['profile']}: {report['checked']} receipt(s), "
                        f"{len(report['defects'])} defect(s)"
                        + (f", {report['corrected']} corrected"
                           if report.get("corrected") else ""))
                print(line)
                for defect in report["defects"]:
                    print(f"  {defect['kind']}: "
                          f"{ {k: v for k, v in defect.items() if k != 'kind'} }")
        return 1 if any(r["defects"] for r in reports) else 0
    if args.profile == "all":
        # Principals are the profiles that exist, filtered to those with state:
        # `state/` also holds `.writer-locks/`, `idx/` and scratch dirs.
        from situation import _principal_profiles

        profiles = [p for p in _principal_profiles(config.LOVEWORK_ROOT)
                    if (config.STATE_DIR / p).is_dir()]
    else:
        profiles = [args.profile]

    if args.board:
        # The refined-GO board on demand; the draining runs refresh it anyway.
        from hone_board import write_board

        for profile in profiles:
            path = write_board(config.STATE_DIR / profile, profile)
            print(f"{profile}: board {path}")
        return 0

    receipts = []
    if args.max_actions == 0 or args.post_crawl:
        # Slot mode — the default (LJ, 2026-10-18): drain, then yield.
        try:
            receipts = tick_slot(profiles, mode=args.mode, apply=args.apply,
                                 today=today, force=args.force,
                                 post_crawl=args.post_crawl,
                                 max_actions=args.max_actions,
                                 run_minutes=args.run_minutes)
        except Exception as exc:
            receipts = [{"profile": p, "outcome": "failed",
                         "error": f"{type(exc).__name__}: {exc}",
                         "due": 0, "needs_principal": 0} for p in profiles]
    else:
        for profile in profiles:
            try:
                receipts.append(tick(profile, mode=args.mode, apply=args.apply,
                                     today=today, force=args.force,
                                     max_actions=args.max_actions,
                                     run_minutes=args.run_minutes))
            except Exception as exc:
                receipts.append({"profile": profile, "outcome": "failed",
                                 "error": f"{type(exc).__name__}: {exc}",
                                 "due": 0, "needs_principal": 0})
    if args.json:
        print(json.dumps(receipts, indent=2, sort_keys=True))
    else:
        for receipt in receipts:
            print(_line(receipt))
        # The audit rides along on the printed line, not on the exit code: a
        # defect in the past must not stop today's work, and it must not be
        # invisible either.
        for receipt in receipts:
            defects = (audit_receipts(receipt["profile"]) if receipt.get("outcome")
                       != "failed" else {"defects": []})["defects"]
            if defects:
                print(f"  audit: {len(defects)} defect(s) in "
                      f"{receipt['profile']}'s receipts — run --audit")
    if any(r.get("contended") for r in receipts):
        return 75                       # a writer holds the principal
    return 0 if all(r["outcome"] != "failed" for r in receipts) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
