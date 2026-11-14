#!/usr/bin/env python3
"""maintenance.py — the cheap passes the loop runs every hour.

LJ, 2026-10-14: *"Now we are running every 1 hour, there is lots more traffic and
lots more activity. The old 6am - 7am [shoulder] runs don't make much sense to me
… Whatever they did — we should transfer the functionality they did in the
lovework tick-ing regular hourly runs."*

He is right about the shape: those passes are **cheap, idempotent and
deterministic**, and they kept a day's worth of staleness because they only ran
once a morning. Hourly, the state the brief and the wiki read is at most an hour
old.

What runs here, in this order (the order is the contract, ch14):

1. **case ingest** — `snapshot.py --case-outcomes`: what the principal did with a
   pack becomes an outcome row (history.py reads those to refuse a re-apply).
2. **watch** — `watch.py`: the ten deterministic watches → `dataset/events.jsonl`.
3. **self-heal** — `selfheal.py --apply`: the wiki's immune system, daily by its
   marker; idempotent. The health observation follows it so readers see the
   healed state.
4. **retire** and **packs** — the closing piles and a CUT pack for every current GO
   or MAYBE.
5. **health observe** every tick; **health control** and **health deep** daily;
   **health trend** weekly (docs/51 §8).
6. **brief** — the watch speaking, hourly when there are events; it reads the
   stored health line.
7. **memory coarsening** — `consolidate.py --max 2 --apply`: the one pass here that
   costs model calls, so it runs **once a day** (a dated marker).

What deliberately stays at 06:00: the **world pass** (the GO link re-check and the
application-email scan/drafts — it needs the network and is slower) and the
inference report. One pass, one authority, one schedule: nothing runs in both
places.

Each pass is a subprocess, so a broken pass cannot take the loop with it; a
failure is recorded in the tick's receipt rather than swallowed, and a writer
holding the principal (exit 75) is a deferral, not a failure.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

logger = logging.getLogger(__name__)

#: The passes, in order, with what each costs — **measured**, because "move the
#: morning passes into the hourly tick" is right for the cheap ones and wrong for
#: the expensive ones (2026-10-14):
#:
#:   case-ingest   ~1 s     hourly
#:   watch         0.13 s   hourly
#:   retire        ~1 s     hourly  ← the closing piles (docs/45): a registry read,
#:                                    a link-verdict read, and a few moves
#:   consolidate   model calls, ≤2 compressions   daily
PASSES: tuple[dict, ...] = (
    {"name": "case-ingest", "argv": ["snapshot.py", "--case-outcomes"], "daily": False},
    {"name": "watch", "argv": ["watch.py"], "daily": False},
    {"name": "self-heal", "argv": ["selfheal.py", "--apply"], "daily": True},
    {"name": "retire", "argv": ["retire.py"], "daily": False},
    # **Every GO has a pack, every hour** (LJ, 2026-10-14): *"the hourly jobs should
    # be prioritising the GO ads, making sure our information is correct there, and
    # making sure those that are a GO have packs. So the principal can actually
    # apply to them. This is the most important thing, this is a major reason for
    # existing of lovework. Create connections - and that happens via the GO ads
    # and packs."*
    #
    # It sat inside the daily self-heal, where it was invisible: a GO discovered at
    # 07:00 waited up to 24 h for its pack. Measured cost of the step on its own:
    # **0.6 s (kj) / 2.1 s (lj)** — 500× cheaper than the self-heal it hid in, so
    # the honest unit was never "daily". It runs after `retire` (a retired advert
    # must not get a fresh pack) and stays GO-only by construction.
    # GO **and MAYBE** (LJ, 2026-10-20: *"Let's make the MAYBE-s also prepared for
    # application … Add the requirement for preparing application packs for MAYBE-s too"*).
    # Cutting is free (no model calls); the loop's `maybe_pack` queue is what fills them,
    # and it runs only after every GO pack in every principal is done.
    {"name": "packs", "argv": ["application_packs.py", "--prepare-missing",
                               "--decisions", "GO,MAYBE"],
     "daily": False},
    # **Health observes every tick, controls once a day** (docs/51 §8). The
    # observation runs after the wiki/pack repairs so the brief and monitor read
    # the state that exists now. The controller is daily: its actions are
    # internal, serial and lease-bound, and it must not compete with the
    # principal queue on every tick.
    {"name": "health-observe", "argv": ["health.py", "--write"],
     "daily": False, "timeout_s": 120},
    {"name": "health-control",
     "argv": ["health_loop.py", "--apply", "--write",
              "--max-actions", "3", "--max-minutes", "3"],
     "daily": True, "timeout_s": 240},
    {"name": "health-deep", "argv": ["health.py", "--deep", "--write"],
     "daily": True, "timeout_s": 120},
    {"name": "health-trend", "argv": ["health.py", "--trend", "--write"],
     "weekly": True, "timeout_s": 60},
    # **The brief, hourly** (LJ, 2026-10-15: *"let us make it more frequent, through
    # the day. Whatever it did - why not make it be done every hour?"*). The watch
    # has run hourly since it moved into the tick; the *brief* stayed at 06:00, which
    # is the part that made "someone watching over you" read wrong — a watcher who
    # checks at seven and then sleeps.
    #
    # It costs nothing on a quiet hour: the brief writes and sends **only when there
    # are events since its cursor** ("0 events since cursor — quiet day, no brief
    # written"), so the model call happens when there is something to say. Measured
    # before the change: 54 brief calls across all principals, i.e. one a day each.
    {"name": "brief", "argv": ["brief.py", "--send"], "daily": False},
    {"name": "consolidate", "argv": ["consolidate.py", "--max", "2", "--apply"],
     "daily": True},
)

#: **The wiki self-heal is daily, not hourly.** It sits in PASSES with
#: ``daily=True``: measured 1 m 52 s on vj's wiki and **still running at 3 m 17 s**
#: on lj's (1,960 org pages), where it also holds the principal's writer lease for
#: the whole time. Running it every tick would cost the loop an hour of packs a day
#: and block a crawl that starts in the window; the daily marker keeps it in the
#: maintenance window. *The cheap passes moved hourly; the measurement says which
#: is which.*

#: Exit codes that are *information*, not failure. `selfheal.py` returns 1 when it
#: found defects (that is what it is for) and 2 when a defect class is rising (an
#: alarm, and the receipt says so). Treating 1 as a failure would have marked every
#: successful heal as broken (measured 2026-10-14).
EXPECTED_EXITS: dict[str, tuple[int, ...]] = {
    "self-heal": (0, 1, 2),
    # Health exit 1 = defects remain, 2 = a required metric unknown. Both are
    # information from the pass, not a broken process; 75 is handled as deferral
    # by the runner before this table is consulted.
    "health-observe": (0, 1, 2),
    "health-control": (0, 1, 2),
    "health-deep": (0, 1, 2),
}
ALARM_EXIT = 2

#: Per-pass wall-clock ceiling. A pass that hangs must not hold the loop's hour.
PASS_TIMEOUT_S = int(os.getenv("LOVEWORK_MAINTENANCE_TIMEOUT", "240"))


def _marker(state_root: Path, name: str, today: date,
            *, weekly: bool = False) -> Path:
    """The period's "already ran" stamp: `state/<p>/cache/<pass>-<period>`.

    LJ, 2026-10-19: *"there is no point to these dots … in the names"*. The
    marker is state a person reads in an `ls` — a leading dot hid a file whose
    whole job is to be seen, and bought nothing: it is not a dotfile, and git
    tracked it anyway. (`run_ledger`/`writer_boundary` keep their `.<name>.tmp`
    dots: those are atomic-write staging files, genuinely transient, and hidden
    is what they should be.)
    """
    stamp = (f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
             if weekly else today.isoformat())
    return state_root / "cache" / f"{name}-{stamp}"


def _python() -> str:
    return sys.executable


#: The least time a pass is worth starting with, in seconds. A pass that has less
#: than this left cannot finish, and starting it would spend the run's last minutes
#: on a process the tick is about to be killed inside.
MIN_PASS_SECONDS = 45.0


def run(profile: str, *, state_dir: Path | None = None, repo_root: Path | None = None,
        today: date | None = None, only: tuple[str, ...] | None = None,
        deadline: float | None = None) -> dict:
    """Run this hour's passes for one principal. Never raises.

    Returns `{"ran": [...], "skipped": [...], "failed": [...], "deferred": [...]}`
    — the names, so the tick's receipt says what maintenance did without a log.

    **`deadline` shares the caller's clock** (2026-10-19). Each pass has its own
    `PASS_TIMEOUT_S`, but the *sum* had no bound — and the maintenance phase runs
    before the actions, so a self-heal day could spend the whole slot here and leave
    the queue untouched. The tick now passes its slot deadline; a pass that cannot
    fit is deferred by name rather than started, so the receipt says what was skipped
    and why. (The kill it prevents: `timeout` at the wrapper, exit 124, "failed for
    lj" — three runs in a row, from a budget that bounded everything except the phase
    that came first.)
    """
    import time as _time

    _now = _time.monotonic
    state_dir = state_dir or config.STATE_DIR
    repo_root = repo_root or config.LOVEWORK_ROOT
    state_root = state_dir / profile
    today = today or date.today()
    agent = repo_root / "lovework-agent"
    result: dict = {"ran": [], "skipped": [], "failed": [], "deferred": []}
    if not state_root.is_dir():
        return result

    for entry in PASSES:
        name = entry["name"]
        if only is not None and name not in only:
            continue
        if deadline is not None and deadline - _now() < MIN_PASS_SECONDS:
            result["deferred"].append(f"{name} (no time left in the run)")
            continue
        weekly = bool(entry.get("weekly"))
        marker = _marker(state_root, name, today, weekly=weekly)
        if (entry.get("daily") or weekly) and marker.is_file():
            result["skipped"].append(name)
            continue
        argv = [_python(), str(agent / entry["argv"][0]),
                *[a for a in entry["argv"][1:]], "--profile", profile]
        pass_timeout = float(entry.get("timeout_s", PASS_TIMEOUT_S))
        if deadline is not None:
            pass_timeout = max(MIN_PASS_SECONDS,
                               min(pass_timeout, deadline - _now()))
        try:
            completed = subprocess.run(argv, capture_output=True, text=True,
                                       timeout=pass_timeout, cwd=str(agent))
        except subprocess.TimeoutExpired:
            result["failed"].append(f"{name} (timeout {int(pass_timeout)}s)")
            continue
        except OSError as exc:
            result["failed"].append(f"{name} ({type(exc).__name__})")
            continue
        if completed.returncode == 75:
            result["deferred"].append(name)          # a writer holds the principal
            continue
        expected = EXPECTED_EXITS.get(name, (0,))
        if completed.returncode not in expected:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            result["failed"].append(f"{name} (exit {completed.returncode}"
                                    + (f": {detail[-1][:80]}" if detail else "") + ")")
            continue
        result["ran"].append(name)
        if completed.returncode == ALARM_EXIT:
            result.setdefault("alarms", []).append(name)
        if entry.get("daily") or weekly:
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("ran\n", encoding="utf-8")
            except OSError:
                pass                                  # a marker that cannot be written
                                                      # only costs a repeat
    return result


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--only", default="", help="comma-separated pass names")
    args = parser.parse_args()
    outcome = run(args.profile, only=tuple(x for x in args.only.split(",") if x) or None)
    if args.json:
        import json

        print(json.dumps(outcome, indent=2, sort_keys=True))
        return 0
    print(f"{args.profile}: ran {', '.join(outcome['ran']) or 'nothing'}"
          + (f" · skipped {', '.join(outcome['skipped'])}" if outcome["skipped"] else "")
          + (f" · deferred {', '.join(outcome['deferred'])}" if outcome["deferred"] else "")
          + (f" · FAILED {', '.join(outcome['failed'])}" if outcome["failed"] else ""))
    return 1 if outcome["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(_cli())
