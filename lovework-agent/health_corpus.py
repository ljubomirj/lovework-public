#!/usr/bin/env python3
"""Phase 5: the curated health case corpus.

The corpus is data, not prose parsing. Each YAML case names its source anchor
(`LEARNINGS.md` or `MEMORY.md`), the metric values before and after a repair, the
action and guard, and the expected outcome. The loader validates the anchor
exists, the action and metric are registered, and then the test runs the pure
health decisions on the fixture. The external repair is not called here; the
case is a contract for the decision and the evidence it must preserve.

Case schema (see `health/cases/*.yaml`):

    id: stable case id
    source: {file: LEARNINGS.md, anchor: "an exact phrase in the file"}
    action: a registered action name, or "none"
    before: {metric.name: value | {value: ..., observed: ..., detail: ...}}
    after:  same shape; the default is a healthy snapshot + these overrides
    gaps:   optional evidence-limit counts for the before snapshot
    guard:  optional target guard metric or the local guard
    guard_total: / guard_lost:  for work_auth.location_evidence
    expect: accepted | rejected | guard_rejected | skipped | blocked |
            no_action | healthy | unknown
    note: one sentence

The test suite also owns the simulated week for the controller; the YAML cases
cover the one-action decisions and the false-positive classes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import health
import health_actions
import health_loop

CASES_ROOT = Path(__file__).resolve().parent.parent / "health" / "cases"
REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_FILES = ("LEARNINGS.md", "MEMORY.md")
EXPECT = {"accepted", "rejected", "guard_rejected", "skipped", "blocked",
          "no_action", "healthy", "unknown"}
LOCAL_GUARD = health_actions.LOCAL_GUARD
EPS = 1e-9


def load_cases(root: Path | None = None) -> list[dict]:
    root = Path(root) if root is not None else CASES_ROOT
    cases: list[dict] = []
    for path in sorted(root.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if isinstance(payload, dict):
            payload = payload.get("cases", [])
        if not isinstance(payload, list):
            raise ValueError(f"{path}: expected a list or a cases: list")
        for raw in payload:
            if not isinstance(raw, dict):
                raise ValueError(f"{path}: a case must be a mapping")
            case = dict(raw)
            case["_path"] = str(path)
            cases.append(case)
    return cases


def validate_case(case: dict, *, repo_root: Path | None = None) -> list[str]:
    problems: list[str] = []
    case_id = case.get("id")
    if not case_id:
        problems.append("missing id")
    source = case.get("source")
    if not isinstance(source, dict) or not source.get("file") or not source.get("anchor"):
        problems.append(f"{case_id or '?'}: source must name file and anchor")
    else:
        if source["file"] not in SOURCE_FILES:
            problems.append(f"{case_id}: source must be one of {SOURCE_FILES}")
        root = Path(repo_root) if repo_root is not None else REPO_ROOT
        path = root / str(source["file"])
        if not path.is_file():
            problems.append(f"{case_id}: source file not found: {source['file']}")
        elif str(source["anchor"]).casefold() not in path.read_text(
                encoding="utf-8", errors="ignore").casefold():
            problems.append(f"{case_id}: source anchor not found: {source['anchor']!r}")
    action_name = case.get("action", "none")
    if action_name != "none" and action_name not in health_actions.ACTION_BY_NAME:
        problems.append(f"{case_id}: unknown action {action_name!r}")
    if case.get("expect") not in EXPECT:
        problems.append(f"{case_id}: unknown expectation {case.get('expect')!r}")
    for key in ("before", "after"):
        values = case.get(key) or {}
        if not isinstance(values, dict):
            problems.append(f"{case_id}: {key} must be a mapping")
            continue
        for metric in values:
            if metric not in health.METRIC_BY_NAME:
                problems.append(f"{case_id}: unknown metric {metric!r} in {key}")
    guard = case.get("guard")
    if guard and guard not in health.METRIC_BY_NAME and guard != LOCAL_GUARD:
        problems.append(f"{case_id}: unknown guard {guard!r}")
    if guard == LOCAL_GUARD:
        for key in ("guard_total", "guard_lost"):
            if not isinstance(case.get(key), int) or case.get(key) < 0:
                problems.append(f"{case_id}: {key} must be a non-negative integer")
    return problems


def corpus_problems(root: Path | None = None) -> list[str]:
    cases = load_cases(root)
    problems: list[str] = []
    seen: set[str] = set()
    for case in cases:
        case_id = case.get("id", "?")
        if case_id in seen:
            problems.append(f"duplicate case id: {case_id}")
        seen.add(case_id)
        problems.extend(validate_case(case))
    return problems


def _readings(raw: dict | None) -> dict:
    out: dict = {}
    for name, value in (raw or {}).items():
        out[name] = dict(value) if isinstance(value, dict) else {"value": value}
    return out


def _default_readings() -> dict:
    readings: dict = {}
    for metric in health.METRICS:
        if metric.name == "deploy.stamp_matches":
            value = True
        elif metric.name == "loop.heartbeat_age_min":
            value = 100.0
        else:
            value = metric.target
        readings[metric.name] = {"value": value}
    return readings


def snapshot_for(case: dict, key: str = "before") -> dict:
    readings = _default_readings()
    readings.update(_readings(case.get(key) or {}))
    gaps = case.get("gaps") if key == "before" else None
    return health.build_snapshot("lj", readings, gaps=gaps or {})


def _row(snapshot: dict, name: str) -> dict:
    return health_actions._metric_row(snapshot, name)


def simulate(case: dict) -> dict:
    """Run the one-action decision the case describes; return its outcome."""
    before = snapshot_for(case, "before")
    after = snapshot_for(case, "after")
    expect = case.get("expect")
    action_name = case.get("action", "none")

    if expect in ("healthy", "unknown"):
        stop = health_loop.health_stop(before)
        actual = stop["reason"] if stop else "action_available"
        return {"id": case.get("id"), "actual": actual, "expected": expect,
                "detail": stop or {"plan": health_loop.rank_actions(before)}}

    if expect == "no_action":
        stop = health_loop.health_stop(before)
        if stop:
            return {"id": case.get("id"), "actual": stop["reason"],
                    "expected": expect, "detail": stop}
        plan = health_loop.rank_actions(before)
        return {"id": case.get("id"),
                "actual": "no_action" if not plan else "action_available",
                "expected": expect,
                "detail": {"plan": plan, "variance": before["variance"]["total"]}}

    if action_name == "none":
        return {"id": case.get("id"), "actual": "no_action",
                "expected": expect, "detail": "no action named"}

    action = health_actions.ACTION_BY_NAME[action_name]
    if not action.executable:
        return {"id": case.get("id"), "actual": "blocked",
                "expected": expect, "detail": action.blocked_reason}

    target_before = _row(before, action.metric)
    if target_before["error"] <= EPS:
        return {"id": case.get("id"), "actual": "skipped",
                "expected": expect, "detail": f"{action.metric} healthy"}

    target_after = _row(after, action.metric)
    target_delta = target_before["error"] - target_after["error"]
    guard = case.get("guard") or action.guard
    if guard == LOCAL_GUARD:
        total = int(case.get("guard_total", 0))
        lost = int(case.get("guard_lost", 0))
        guard_before_error = 0.0
        guard_after_error = (lost / total) if total else 0.0
    elif guard:
        guard_before_error = _row(before, guard)["error"]
        guard_after_error = _row(after, guard)["error"]
    else:
        guard_before_error = guard_after_error = 0.0
    guard_delta = guard_after_error - guard_before_error

    if not target_after["observed"]:
        actual = "unknown"
    elif target_delta <= EPS:
        actual = "rejected"
    elif guard_delta > target_delta + EPS:
        actual = "guard_rejected"
    else:
        actual = "accepted"
    return {"id": case.get("id"), "actual": actual, "expected": expect,
            "target_delta": round(target_delta, 6),
            "guard_delta": round(guard_delta, 6),
            "detail": {"target": action.metric, "guard": guard}}


def by_id(cases: list[dict] | None = None) -> dict[str, dict]:
    return {case["id"]: case for case in (cases or load_cases())}
