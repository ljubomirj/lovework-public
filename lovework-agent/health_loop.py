#!/usr/bin/env python3
"""Phase 3: the internal-health controller.

Observe once, rank the executable actions by expected variance reduction per
second, run one action, re-observe from that action's fresh after-snapshot, and
stop on any of the explicit boundaries:

* the slot budget (`--max-actions`, `--max-minutes`);
* variance 0 with no unknown required metric;
* no eligible action;
* the same no-effect action twice;
* the same state after a different action;
* a writer-busy lease, a repair error, a guard rejection, or an unknown target.

The controller is serial. Each action is the Phase 2 `health_actions.run_action`
with its own target/guard and its own writer lease. The controller does not run
in the hourly tick's principal queue; Phase 4 will place it in the maintenance
window.

Dry-run is the default: it returns the ranked plan and runs nothing. `--apply`
runs the plan; `--write` records the controller row and lets each action write
its own receipt and after-snapshot.

Exit codes: 0 healthy or clean; 1 defects remain; 2 unknown required metric;
3 no principal state; 75 writer busy.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import health
import health_actions

EPS = 1e-9
DEFAULT_MAX_ACTIONS = 3
DEFAULT_MAX_MINUTES = 8.0


def _summary(snapshot: dict) -> dict:
    variance = snapshot.get("variance") or {}
    metrics = snapshot.get("metrics") or {}
    return {
        "variance": variance.get("total", 0.0),
        "coverage": variance.get("coverage", {}),
        "errors": {name: row.get("error") for name, row in metrics.items()
                   if row.get("applicable")},
        "values": {name: row.get("value") for name, row in metrics.items()
                   if row.get("applicable")},
    }


def state_signature(snapshot: dict) -> tuple:
    """A timestamp-free identity of the measured state for repeated-state stops."""
    rows = []
    for name in sorted((snapshot.get("metrics") or {})):
        row = snapshot["metrics"][name]
        if not row.get("applicable"):
            continue
        rows.append((name, row.get("value"),
                     round(float(row.get("error", 1.0)), 6),
                     bool(row.get("observed", False))))
    return tuple(rows)


def health_stop(snapshot: dict) -> dict | None:
    """Stop before acting when the measured state already says so."""
    variance = float((snapshot.get("variance") or {}).get("total", 0.0))
    coverage = (snapshot.get("variance") or {}).get("coverage") or {}
    if variance <= EPS:
        if coverage.get("unknown"):
            return {"reason": "unknown",
                    "detail": f"variance 0 but {coverage['unknown']} required metric(s) unknown",
                    "exit_code": 2}
        return {"reason": "healthy", "detail": "variance 0 and coverage complete",
                "exit_code": 0}
    return None


def defect_exit(snapshot: dict) -> int:
    variance = float((snapshot.get("variance") or {}).get("total", 0.0))
    coverage = (snapshot.get("variance") or {}).get("coverage") or {}
    # Unknown is blindness, not a known defect; 2 outranks 1.
    if coverage.get("unknown"):
        return 2
    return 1 if variance > EPS else 0


def rank_actions(snapshot: dict, costs: dict[str, float] | None = None) -> list[dict]:
    """Expected variance reduction per second, with the current state's error.

    The estimate is the target metric's contribution to the weighted variance
    if it reached its target:

        weight * target_error / total_applicable_weight

    The action's `expected_reduction` is a multiplier (1.0 in this slice). The
    cost is the action's measured duration after its first run, else its
    registry estimate. The score is the estimate divided by that cost.
    """
    costs = costs or {}
    metrics = snapshot.get("metrics") or {}
    total_weight = sum(
        health.METRIC_BY_NAME[m.name].weight for m in health.METRICS
        if (metrics.get(m.name) or {}).get("applicable"))
    if total_weight <= EPS:
        return []
    plan: list[dict] = []
    for action in health_actions.ACTIONS:
        if not action.executable or action.read_only:
            continue
        ok, _why = health_actions.precondition(action, snapshot)
        if not ok:
            continue
        row = metrics.get(action.metric) or {}
        error = max(0.0, min(1.0, float(row.get("error", 0.0))))
        if error <= EPS:
            continue
        metric = health.METRIC_BY_NAME[action.metric]
        expected = (metric.weight * error / total_weight) * action.expected_reduction
        cost = max(0.1, float(costs.get(action.name, action.cost_seconds)))
        plan.append({
            "action": action.name,
            "target": action.metric,
            "target_error": round(error, 6),
            "weight": metric.weight,
            "expected_reduction": round(expected, 6),
            "cost_seconds": round(cost, 2),
            "score": round(expected / cost, 8),
        })
    plan.sort(key=lambda row: (-row["score"], row["action"]))
    return plan


def _stop(reason: str, detail: str, exit_code: int) -> dict:
    return {"reason": reason, "detail": detail, "exit_code": exit_code}


def _step_compact(step: dict) -> dict:
    return health_actions._compact_receipt(step)


def run_controller(principal: str, *, apply: bool = False,
                   max_actions: int = DEFAULT_MAX_ACTIONS,
                   max_minutes: float = DEFAULT_MAX_MINUTES,
                   write: bool = False, deep: bool = False,
                   now: datetime | None = None) -> dict:
    """The serial controller. Returns its receipt; never raises."""
    started = time.monotonic()
    at = health._iso(now)
    snapshot = health.collect(principal, deep=deep, write=False, now=now)
    result: dict = {
        "principal": principal,
        "at": at,
        "apply": apply,
        "max_actions": max_actions,
        "max_minutes": max_minutes,
        "before": _summary(snapshot) if not snapshot.get("missing_state") else None,
        "after": None,
        "plan": [],
        "steps": [],
        "stop": None,
        "actions_run": 0,
        "seconds": 0.0,
        "exit_code": 0,
    }
    if snapshot.get("missing_state"):
        result["stop"] = _stop("missing_state",
                               f"no state at {snapshot['missing_state']}", 3)
        result["exit_code"] = 3
        result["seconds"] = round(time.monotonic() - started, 2)
        return result

    initial_stop = health_stop(snapshot)
    if initial_stop:
        result["stop"] = initial_stop
        result["exit_code"] = initial_stop["exit_code"]
        result["seconds"] = round(time.monotonic() - started, 2)
        if write:
            write_controller_receipt(result)
        return result

    initial_plan = rank_actions(snapshot)
    result["plan"] = initial_plan
    if not initial_plan:
        result["after"] = _summary(snapshot)
        result["stop"] = _stop(
            "no_action",
            "variance is not zero but no action has an eligible target", 
            defect_exit(snapshot))
        result["exit_code"] = result["stop"]["exit_code"]
        result["seconds"] = round(time.monotonic() - started, 2)
        if write:
            write_controller_receipt(result)
        return result

    if not apply:
        result["after"] = _summary(snapshot)
        result["stop"] = _stop(
            "dry_run",
            f"{len(initial_plan)} action(s) planned; no repair applied", 
            defect_exit(snapshot))
        result["exit_code"] = result["stop"]["exit_code"]
        result["seconds"] = round(time.monotonic() - started, 2)
        if write:
            write_controller_receipt(result)
        return result

    if max_actions <= 0:
        result["after"] = _summary(snapshot)
        result["stop"] = _stop("max_actions", "max_actions is 0", defect_exit(snapshot))
        result["exit_code"] = result["stop"]["exit_code"]
        result["seconds"] = round(time.monotonic() - started, 2)
        if write:
            write_controller_receipt(result)
        return result

    state_sig = state_signature(snapshot)
    previous_action: str | None = None
    no_effect: dict[str, int] = {}
    costs: dict[str, float] = {}

    while True:
        if result["actions_run"] >= max_actions:
            result["stop"] = _stop(
                "max_actions", f"reached {max_actions} action(s)", defect_exit(snapshot))
            break
        if (time.monotonic() - started) >= max_minutes * 60.0:
            result["stop"] = _stop(
                "slot_budget", f"reached {max_minutes:g} minute(s)",
                defect_exit(snapshot))
            break

        plan = rank_actions(snapshot, costs)
        if not plan:
            result["stop"] = _stop(
                "no_action",
                "variance is not zero but no action has an eligible target",
                defect_exit(snapshot))
            break
        choice = plan[0]
        step = health_actions.run_action(
            principal, choice["action"], apply=True, write=write)
        result["steps"].append({"choice": choice, "receipt": _step_compact(step)})
        result["actions_run"] += 1
        measured = float(step.get("seconds") or choice["cost_seconds"])
        costs[choice["action"]] = max(0.1, measured)

        if step.get("busy"):
            result["stop"] = _stop("writer_busy", str(step.get("error", "writer busy")), 75)
            break
        if step.get("error"):
            result["stop"] = _stop("action_error", str(step["error"]),
                                   2 if "unknown" in str(step["error"]).lower() else 1)
            break
        after = step.get("after")
        if after is None:
            result["stop"] = _stop("action_error", "the action returned no after-snapshot", 1)
            break
        result["after"] = _summary(after)

        accepted = step.get("accepted")
        if accepted is False:
            target = step.get("target") or {}
            after_target = target.get("after") or {}
            if not after_target.get("observed", True):
                result["stop"] = _stop(
                    "target_unknown", f"{choice['action']} left its target unknown", 2)
                break
            target_delta = float(target.get("delta") or 0.0)
            guard_delta = float((step.get("guard") or {}).get("delta") or 0.0)
            if target_delta <= EPS:
                count = no_effect.get(choice["action"], 0) + 1
                no_effect[choice["action"]] = count
                if count >= 2:
                    result["stop"] = _stop(
                        "no_effect_twice",
                        f"{choice['action']} did not reduce its target twice", 1)
                    break
                before_sig = state_signature(step.get("before") or snapshot)
                if (previous_action and previous_action != choice["action"]
                        and before_sig == state_sig):
                    result["stop"] = _stop(
                        "state_repeated",
                        f"the state did not change after {choice['action']} "
                        f"(previous action: {previous_action})", 1)
                    break
                previous_action = choice["action"]
                state_sig = state_signature(after)
                snapshot = after
                continue
            if guard_delta > target_delta + EPS:
                result["stop"] = _stop(
                    "guard_rejected",
                    f"guard worsened by {guard_delta:.3f} > target improvement "
                    f"{target_delta:.3f}", 1)
                break
            result["stop"] = _stop(
                "rejected", f"{choice['action']} was not accepted", 1)
            break

        if accepted is not True:
            result["stop"] = _stop(
                "not_accepted", f"{choice['action']} returned accepted={accepted!r}", 1)
            break

        # Accepted: the action's own after-snapshot is the next observation.
        no_effect.clear()
        previous_action = choice["action"]
        state_sig = state_signature(after)
        snapshot = after
        stop_healthy = health_stop(snapshot)
        if stop_healthy:
            result["stop"] = stop_healthy
            break

    if result.get("after") is None:
        result["after"] = _summary(snapshot)
    if result.get("stop") is None:
        result["stop"] = _stop("no_action", "controller ended without a stop", 1)
    result["exit_code"] = int(result["stop"].get("exit_code", 1))
    result["seconds"] = round(time.monotonic() - started, 2)
    if write:
        write_controller_receipt(result)
    return result


def write_controller_receipt(result: dict) -> Path:
    """Append the controller decision/outcome row to health/controller.jsonl."""
    root = Path(config.STATE_DIR) / str(result.get("principal")) / "health"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "controller.jsonl"
    row = {
        "at": result.get("at"),
        "principal": result.get("principal"),
        "apply": result.get("apply"),
        "max_actions": result.get("max_actions"),
        "max_minutes": result.get("max_minutes"),
        "before": result.get("before"),
        "after": result.get("after"),
        "plan": result.get("plan"),
        "steps": result.get("steps"),
        "stop": result.get("stop"),
        "actions_run": result.get("actions_run"),
        "seconds": result.get("seconds"),
        "exit_code": result.get("exit_code"),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return path


def render_controller(result: dict) -> str:
    if result.get("stop", {}).get("reason") == "missing_state":
        return f"{result.get('principal')}: health controller — {result['stop']['detail']}"
    before = result.get("before") or {}
    after = result.get("after") or {}
    lines = [
        f"{result.get('principal')}: health controller — "
        f"{'apply' if result.get('apply') else 'dry-run'} · "
        f"variance {before.get('variance', 0):.3f} -> {after.get('variance', 0):.3f} · "
        f"{result.get('actions_run', 0)} action(s) · {result.get('seconds', 0):.1f}s",
    ]
    plan = result.get("plan") or []
    if plan:
        lines.append("  plan:")
        for row in plan[:6]:
            lines.append(
                f"    {row['action']:26s} error {row['target_error']:.2f}  "
                f"cost {row['cost_seconds']:>5.1f}s  score {row['score']:.6f}")
    for step in result.get("steps") or []:
        choice = step.get("choice") or {}
        receipt = step.get("receipt") or {}
        verdict = receipt.get("accepted")
        mark = "accepted" if verdict is True else "rejected" if verdict is False else "no verdict"
        lines.append(
            f"  ran {choice.get('action')}: {mark} "
            f"({receipt.get('reason', '')}) in {receipt.get('seconds', 0)}s")
    stop = result.get("stop") or {}
    lines.append(f"  stop: {stop.get('reason')} — {stop.get('detail')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--apply", action="store_true",
                        help="run the ranked plan (default: dry-run plan only)")
    parser.add_argument("--max-actions", type=int, default=DEFAULT_MAX_ACTIONS)
    parser.add_argument("--max-minutes", type=float, default=DEFAULT_MAX_MINUTES)
    parser.add_argument("--deep", action="store_true",
                        help="force a fresh wiki/receipt audit for the first observation")
    parser.add_argument("--write", action="store_true",
                        help="write health/controller.jsonl; --apply also writes action receipts")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config.set_active_profile(args.profile)
    result = run_controller(args.profile, apply=args.apply,
                            max_actions=args.max_actions,
                            max_minutes=args.max_minutes,
                            write=args.write, deep=args.deep)
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True, default=str))
    else:
        print(render_controller(result))
    return int(result.get("exit_code", 1))


if __name__ == "__main__":
    sys.exit(main())
