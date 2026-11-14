#!/usr/bin/env python3
"""Phase 2: executable internal-health actions.

`health.py` observes; this module changes one thing and measures the same
thing again. It is deliberately small and serial:

    observe -> precondition -> take the writer lease -> repair
            -> observe again -> accept or reject -> receipt

Every action names one target metric and one guard. The action is accepted only
when the target error fell and the guard did not worsen by more than that fall.
An action that does nothing is not retried here; the Phase 3 controller owns the
no-effect guard and the budget.

The repairs are the existing authorities, not copies:
`selfheal.run_pass` / `selfheal.selfheal`, `application_packs.prepare_missing_packs`,
`application_packs.mark_superseded_duplicates`, `leads.prepare_missing_leads`,
and `tick.audit_receipts`.

`repo.publish` is listed but blocked: the house rule is no agent commit or push.
The scheduled `scripts/cron/repo.sh` owns the principal repo's commits.

Exit codes: 0 accepted / healthy / check clean; 1 rejected or repair error;
2 unknown or blocked; 3 no principal state; 75 writer busy.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import health
from writer_boundary import WriterBusy, standalone_writer_guard

EPS = 1e-9
LOCAL_GUARD = "work_auth.location_evidence"
#: Location values at or above the repair's own threshold, or that carry a
#: work-authorisation / work-mode token, are the evidence the guard protects.
WORK_AUTH_RE = re.compile(
    r"(?i)(visa|sponsor|work[ -]?auth|right to work|relocat|remote|hybrid|"
    r"on[ -]?site|permit|citizen|eligible|\bUK\b|\bUS\b|\bEU\b|"
    r"\bEEA\b|\bCanada\b|\bIreland\b|\bGermany\b|\bNetherlands\b|"
    r"\bSwitzerland\b|\bJapan\b)")


@dataclass(frozen=True)
class Action:
    name: str
    metric: str
    guard: str | None
    owner: str
    authority: str
    description: str
    cost_seconds: float
    requires: tuple[str, ...]
    requires_kind: str | None = None
    read_only: bool = False
    executable: bool = True
    blocked_reason: str = ""
    expected_reduction: float = 1.0


ACTIONS: tuple[Action, ...] = (
    Action(
        name="selfheal.pass",
        metric="wiki.defects",
        guard=LOCAL_GUARD,
        owner="agent",
        authority="docs/14, docs/45 §5, docs/51 §6",
        description="the ordered self-healing pass: names, links, shapes, packs, leads, index",
        cost_seconds=120.0,
        requires=("wiki.defects",),
    ),
    Action(
        name="packs.prepare_missing",
        metric="packs.go_without_pack",
        guard="wiki.defects",
        owner="agent",
        authority="docs/45 §5, docs/51 §6",
        description="cut a CUT pack for every current GO with no live pack",
        cost_seconds=30.0,
        requires=("packs.go_without_pack",),
    ),
    Action(
        name="packs.reconcile_duplicates",
        metric="receipts.audit_defects",
        guard="packs.go_without_pack",
        owner="agent",
        authority="docs/45 §5, docs/51 §6",
        description="mark duplicate packs and break a supersession cycle",
        cost_seconds=5.0,
        requires=("receipts.audit_defects",),
        requires_kind="pack_duplicates",
    ),
    Action(
        name="leads.prepare_missing",
        metric="leads.missing_page",
        guard="wiki.defects",
        owner="agent",
        authority="docs/45 §5, docs/51 §6",
        description="write a lead page for every judged advert with none",
        cost_seconds=30.0,
        requires=("leads.missing_page",),
    ),
    Action(
        name="receipts.audit",
        metric="receipts.audit_defects",
        guard=None,
        owner="agent",
        authority="tick.audit_receipts, docs/43 §7, docs/51 §6",
        description="read-only receipt integrity check",
        cost_seconds=2.0,
        requires=("receipts.audit_defects",),
        read_only=True,
    ),
    Action(
        name="repo.publish",
        metric="repo.hub_lag",
        guard=None,
        owner="external",
        authority="scripts/cron/repo.sh, docs/15",
        description="commit/push the principal repo — owned by the scheduled wrapper",
        cost_seconds=5.0,
        requires=("repo.hub_lag",),
        executable=False,
        blocked_reason="no agent commit/push; the scheduled scripts/cron/repo.sh owns this",
    ),
)

ACTION_BY_NAME = {a.name: a for a in ACTIONS}


def registry_problems() -> list[str]:
    """Static validation of the action contract. Empty means well formed."""
    problems: list[str] = []
    seen: set[str] = set()
    for action in ACTIONS:
        if action.name in seen:
            problems.append(f"duplicate action name: {action.name}")
        seen.add(action.name)
        if action.metric not in health.METRIC_BY_NAME:
            problems.append(f"{action.name}: unknown target metric {action.metric}")
        if action.guard and action.guard not in health.METRIC_BY_NAME \
                and action.guard != LOCAL_GUARD:
            problems.append(f"{action.name}: unknown guard {action.guard}")
        if action.owner not in ("agent", "principal", "external"):
            problems.append(f"{action.name}: invalid owner {action.owner!r}")
        if not action.authority.strip():
            problems.append(f"{action.name}: empty authority")
        if action.cost_seconds <= 0:
            problems.append(f"{action.name}: cost must be positive")
        if action.expected_reduction <= 0:
            problems.append(f"{action.name}: expected reduction must be positive")
        for name in action.requires:
            if name not in health.METRIC_BY_NAME:
                problems.append(f"{action.name}: unknown required metric {name}")
        if action.read_only and action.executable and action.guard:
            # A read-only check does not repair, so a guard is meaningless.
            problems.append(f"{action.name}: read-only action cannot have a guard")
        if not action.executable and not action.blocked_reason:
            problems.append(f"{action.name}: non-executable action needs a reason")
    return problems


def _metric_row(snapshot: dict, name: str) -> dict:
    row = (snapshot.get("metrics") or {}).get(name) or {}
    return {
        "name": name,
        "value": row.get("value"),
        "error": float(row.get("error", 1.0)),
        "status": row.get("status", "unknown"),
        "observed": bool(row.get("observed", False)),
        "detail": row.get("detail", ""),
    }


def precondition(action: Action, snapshot: dict) -> tuple[bool, str]:
    """The cheap gate: is there anything this action can repair now?"""
    if not action.executable:
        return False, action.blocked_reason or "not executable"
    for name in action.requires:
        row = _metric_row(snapshot, name)
        if not row["observed"]:
            return False, f"{name} unknown"
        if row["error"] <= EPS:
            return False, f"{name} healthy"
    if action.requires_kind:
        row = _metric_row(snapshot, "receipts.audit_defects")
        if action.requires_kind not in str(row.get("detail", "")):
            return False, f"no {action.requires_kind} defect"
    return True, ""


def _lease(principal: str):
    state_root = Path(config.STATE_DIR) / principal
    return standalone_writer_guard(principal, mode="health",
                                    state_root=state_root)


def _repair(action: Action, principal: str, *, dry_run: bool) -> dict:
    """Call the one existing authority for this action."""
    if action.name == "packs.prepare_missing":
        from application_packs import prepare_missing_packs
        return prepare_missing_packs(principal, dry_run=dry_run)
    if action.name == "packs.reconcile_duplicates":
        from application_packs import mark_superseded_duplicates
        apps = Path(config.STATE_DIR) / principal / "applications"
        if not apps.is_dir():
            return {"error": f"no applications at {apps}"}
        return mark_superseded_duplicates(apps, dry_run=dry_run)
    if action.name == "leads.prepare_missing":
        from leads import prepare_missing_leads
        return prepare_missing_leads(principal, dry_run=dry_run)
    if action.name == "receipts.audit":
        import tick
        return tick.audit_receipts(principal, state_dir=Path(config.STATE_DIR))
    raise ValueError(f"no repair for action {action.name}")


def _repair_error(repair: Any) -> str:
    if not isinstance(repair, dict):
        return ""
    if repair.get("busy"):
        return str(repair.get("error") or "writer busy")
    return str(repair.get("error") or "")


def _wiki_root(principal: str) -> Path:
    return Path(config.STATE_DIR) / principal / "wiki"


def location_evidence_values(principal: str) -> list[dict]:
    """The Location values the guard protects, before the action.

    This is not a defect detector: it reads the same `Location:` field the wiki
    repair reads and keeps the long/prose or work-authorisation-bearing values.
    The repair moves such a value into a Note; the guard checks it is still on
    the page, not that the field still exists.
    """
    from wiki_audit import LOCATION_LINE_RE
    from wiki_store import org_page_paths

    root = _wiki_root(principal)
    if not root.is_dir():
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for page in org_page_paths(root):
        try:
            text = page.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in LOCATION_LINE_RE.finditer(text):
            value = match.group(1).strip()
            if len(value) > 90 or WORK_AUTH_RE.search(value):
                key = (page.name, value)
                if key not in seen:
                    seen.add(key)
                    out.append({"page": page.name, "value": value})
    return out


def location_evidence_preserved(principal: str, values: list[dict]) -> dict:
    """How many of the captured values are still anywhere in the wiki."""
    from wiki_store import org_page_paths

    root = _wiki_root(principal)
    texts: list[str] = []
    if root.is_dir():
        for page in org_page_paths(root):
            try:
                texts.append(page.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
    joined = "\n".join(texts)
    preserved = sum(1 for row in values
                    if row["value"] in joined
                    or html.unescape(row["value"]) in joined)
    total = len(values)
    lost = total - preserved
    return {"total": total, "preserved": preserved, "lost": lost,
            "error": (lost / total) if total else 0.0}


def _capture_guard(action: Action, principal: str, snapshot: dict) -> dict:
    if not action.guard:
        return {"name": "", "error": 0.0, "value": None}
    if action.guard == LOCAL_GUARD:
        values = location_evidence_values(principal)
        return {"name": LOCAL_GUARD, "error": 0.0, "value": len(values),
                "total": len(values), "values": values}
    row = _metric_row(snapshot, action.guard)
    return {"name": action.guard, "error": row["error"], "value": row["value"],
            "status": row["status"], "observed": row["observed"]}


def _measure_guard(action: Action, principal: str, snapshot: dict,
                   captured: dict) -> dict:
    if not action.guard:
        return {"name": "", "error": 0.0, "value": None}
    if action.guard == LOCAL_GUARD:
        result = location_evidence_preserved(principal, captured.get("values") or [])
        return {"name": LOCAL_GUARD, "error": result["error"],
                "value": result["preserved"], **result}
    row = _metric_row(snapshot, action.guard)
    return {"name": action.guard, "error": row["error"], "value": row["value"],
            "status": row["status"], "observed": row["observed"]}


def _base(principal: str, action: Action, at: str, before: dict | None,
          apply: bool = False) -> dict:
    return {
        "principal": principal,
        "action": action.name,
        "at": at,
        "apply": apply,
        "before": before,
        "after": None,
        "target": None,
        "guard": None,
        "variance": None,
        "repair": None,
        "accepted": None,
        "reason": "",
        "exit_code": 0,
        "seconds": 0.0,
    }


def run_action(principal: str, name: str, *, apply: bool = False,
               write: bool = False, now: datetime | None = None) -> dict:
    """Run one action, measure it, and return its receipt. Never raises."""
    action = ACTION_BY_NAME.get(name)
    started = time.monotonic()
    at = health._iso(now)
    if action is None:
        return {"principal": principal, "action": name, "at": at,
                "error": f"unknown action {name!r}", "exit_code": 2,
                "seconds": round(time.monotonic() - started, 2)}
    before = health.collect(principal, write=False, now=now)
    if before.get("missing_state"):
        result = _base(principal, action, at, before, apply)
        result.update(error=f"no state at {before['missing_state']}", exit_code=3,
                      seconds=round(time.monotonic() - started, 2))
        return result

    if not action.executable:
        result = _base(principal, action, at, before, apply)
        result.update(blocked=action.blocked_reason, exit_code=2,
                      seconds=round(time.monotonic() - started, 2))
        if write:
            write_receipt(result)
        return result

    ok, why = precondition(action, before)
    if not ok:
        result = _base(principal, action, at, before, apply)
        result.update(skipped=why,
                      exit_code=2 if "unknown" in why else 0,
                      seconds=round(time.monotonic() - started, 2))
        if write:
            write_receipt(result)
        return result

    guard_before = _capture_guard(action, principal, before)
    repair: Any = None
    try:
        if action.name == "selfheal.pass":
            import selfheal

            if apply:
                # `selfheal.selfheal` owns its lease and its baseline.
                repair = selfheal.selfheal(principal, apply=True)
            else:
                # A report-only pass must not advance the selfheal baseline.
                with _lease(principal):
                    repair = selfheal.run_pass(principal, apply=False)
        elif action.read_only:
            repair = _repair(action, principal, dry_run=True)
        else:
            with _lease(principal):
                repair = _repair(action, principal, dry_run=not apply)
    except WriterBusy as busy:
        result = _base(principal, action, at, before, apply)
        result.update(busy=True, error=str(busy), exit_code=75,
                      seconds=round(time.monotonic() - started, 2))
        if write:
            write_receipt(result)
        return result
    except Exception as exc:                  # report, never hide, the failure
        result = _base(principal, action, at, before, apply)
        result.update(error=f"{type(exc).__name__}: {exc}", exit_code=1,
                      seconds=round(time.monotonic() - started, 2))
        if write:
            write_receipt(result)
        return result

    repair_error = _repair_error(repair)
    if isinstance(repair, dict) and repair.get("busy"):
        result = _base(principal, action, at, before, apply)
        result.update(busy=True, error=repair_error, repair=repair, exit_code=75,
                      seconds=round(time.monotonic() - started, 2))
        if write:
            write_receipt(result)
        return result

    if action.read_only:
        result = _base(principal, action, at, before, apply)
        defects = (repair or {}).get("defects") if isinstance(repair, dict) else []
        result.update(read_only=True, repair=repair,
                      exit_code=1 if defects else 0,
                      reason=f"{len(defects or [])} receipt defect(s)" if defects else "clean",
                      seconds=round(time.monotonic() - started, 2))
        if write:
            write_receipt(result)
        return result

    # A dry-run changes nothing; re-reading the full wiki would only pay the
    # audit cost and return the same numbers.
    after = before if not apply else health.collect(
        principal, deep=True, refresh=True, write=False)
    target_before = _metric_row(before, action.metric)
    target_after = _metric_row(after, action.metric)
    guard_after = _measure_guard(action, principal, after, guard_before)
    target_delta = target_before["error"] - target_after["error"]
    guard_delta = guard_after["error"] - guard_before["error"]
    variance_before = float((before.get("variance") or {}).get("total", 0.0))
    variance_after = float((after.get("variance") or {}).get("total", 0.0))
    result = _base(principal, action, at, before, apply)
    result.update(
        after=after,
        target={"name": action.metric, "before": target_before,
                "after": target_after, "delta": round(target_delta, 6)},
        guard={"name": guard_before.get("name", ""),
               "before": guard_before, "after": guard_after,
               "delta": round(guard_delta, 6)},
        variance={"before": variance_before, "after": variance_after,
                  "delta": round(variance_before - variance_after, 6)},
        repair=repair,
    )
    if repair_error:
        result.update(accepted=False, reason=f"repair failed: {repair_error}",
                      exit_code=75 if "busy" in repair_error.lower() else 1)
    elif not apply:
        result.update(accepted=None, reason="dry-run: no repair applied",
                      exit_code=0)
    elif not target_after["observed"]:
        result.update(accepted=False, reason="target is unknown after the action",
                      exit_code=2)
    elif target_delta <= EPS:
        result.update(accepted=False, reason="target did not fall",
                      exit_code=1)
    elif guard_delta > target_delta + EPS:
        result.update(
            accepted=False,
            reason=(f"guard worsened by {guard_delta:.3f}, more than the "
                    f"target improvement {target_delta:.3f}"),
            exit_code=1)
    else:
        result.update(accepted=True,
                      reason="target fell and the guard stayed within the fall",
                      exit_code=0)
    result["seconds"] = round(time.monotonic() - started, 2)
    if write:
        if apply and result.get("after") is not None:
            health.write_snapshot(result["after"])
        write_receipt(result)
    return result


def _compact_receipt(result: dict) -> dict:
    guard = result.get("guard") or {}
    def _guard_side(side: dict | None) -> dict:
        side = side or {}
        keys = ("name", "value", "error", "status", "observed", "total",
                "preserved", "lost")
        return {k: side.get(k) for k in keys if k in side}
    row = {
        "at": result.get("at"),
        "principal": result.get("principal"),
        "action": result.get("action"),
        "apply": result.get("apply"),
        "accepted": result.get("accepted"),
        "reason": result.get("reason"),
        "exit_code": result.get("exit_code"),
        "seconds": result.get("seconds"),
        "skipped": result.get("skipped"),
        "blocked": result.get("blocked"),
        "busy": result.get("busy"),
        "error": result.get("error"),
        "read_only": result.get("read_only"),
        "target": result.get("target"),
        "guard": {"name": guard.get("name"),
                  "before": _guard_side(guard.get("before")),
                  "after": _guard_side(guard.get("after")),
                  "delta": guard.get("delta")} if guard else None,
        "variance": result.get("variance"),
        "repair": _repair_summary(result.get("repair")),
    }
    return {k: v for k, v in row.items() if v is not None}


def _repair_summary(repair: Any) -> Any:
    if not isinstance(repair, dict):
        return repair
    if "classes" in repair:                   # selfheal report
        return {
            "total": repair.get("total"),
            "classes": repair.get("classes"),
            "gaps": repair.get("gaps"),
            "links": repair.get("links"),
            "packs": repair.get("packs"),
            "leads": repair.get("leads"),
            "shapes": repair.get("shapes"),
            "index_rebuilt": repair.get("index_rebuilt"),
            "rose": repair.get("rose"),
            "busy": repair.get("busy"),
            "error": repair.get("error"),
        }
    if "defects" in repair:                   # receipt audit
        return {
            "checked": repair.get("checked"),
            "defects": [d.get("kind") if isinstance(d, dict) else str(d)
                        for d in (repair.get("defects") or [])],
            "corrected": repair.get("corrected"),
            "pruned": repair.get("pruned"),
            "moved": repair.get("moved"),
        }
    return repair


def write_receipt(result: dict) -> Path:
    """Append the compact receipt to `state/<p>/health/actions.jsonl`."""
    root = Path(config.STATE_DIR) / str(result.get("principal")) / "health"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "actions.jsonl"
    row = _compact_receipt(result)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    return path


def render_action(result: dict) -> str:
    if result.get("error") and not result.get("before"):
        return f"{result.get('principal')}: {result.get('action')} — {result['error']}"
    if result.get("busy"):
        return f"{result.get('principal')}: {result.get('action')} — [LOCK] {result.get('error')}"
    if result.get("blocked"):
        return f"{result.get('principal')}: {result.get('action')} — blocked: {result['blocked']}"
    if result.get("error") and result.get("target") is None:
        return f"{result.get('principal')}: {result.get('action')} — {result['error']}"
    if result.get("skipped"):
        return f"{result.get('principal')}: {result.get('action')} — skipped: {result['skipped']}"
    if result.get("read_only"):
        count = len((result.get("repair") or {}).get("defects") or [])
        return (f"{result.get('principal')}: {result.get('action')} — "
                f"read-only check, {count} defect(s)")
    target = result.get("target") or {}
    before = target.get("before") or {}
    after = target.get("after") or {}
    guard = result.get("guard") or {}
    gb = guard.get("before") or {}
    ga = guard.get("after") or {}
    variance = result.get("variance") or {}
    lines = [
        f"{result.get('principal')}: {result.get('action')} — "
        f"{'applied' if result.get('apply') else 'dry-run'} in "
        f"{result.get('seconds', 0):.1f}s",
        f"  target {target.get('name')}: {before.get('value')} -> {after.get('value')} "
        f"(error {before.get('error', 0):.2f} -> {after.get('error', 0):.2f})",
    ]
    if guard.get("name"):
        lines.append(f"  guard {guard.get('name')}: "
                     f"error {gb.get('error', 0):.2f} -> {ga.get('error', 0):.2f}")
    if variance:
        lines.append(f"  variance {variance.get('before', 0):.3f} -> "
                     f"{variance.get('after', 0):.3f}")
    verdict = result.get("accepted")
    lines.append(f"  {'accepted' if verdict else 'not accepted' if verdict is False else 'dry-run'}"
                 f": {result.get('reason')}")
    if result.get("repair"):
        lines.append("  repair: " + json.dumps(_repair_summary(result["repair"]),
                                               sort_keys=True, default=str)[:600])
    return "\n".join(lines)


def _print_list() -> None:
    print(f"{'action':28s} {'metric':28s} {'guard':28s} {'owner':9s} executable")
    for action in ACTIONS:
        print(f"{action.name:28s} {action.metric:28s} "
              f"{(action.guard or '-'):28s} {action.owner:9s} "
              f"{'yes' if action.executable else 'no'}")
        print(f"  {action.description}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--list", action="store_true", help="list the action registry")
    parser.add_argument("--action", choices=sorted(ACTION_BY_NAME))
    parser.add_argument("--apply", action="store_true",
                        help="perform the repair (default: dry-run)")
    parser.add_argument("--write", action="store_true",
                        help="write the after snapshot and actions.jsonl")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        _print_list()
        return 0
    if not args.action:
        parser.error("--action is required unless --list")
    config.set_active_profile(args.profile)
    result = run_action(args.profile, args.action, apply=args.apply,
                        write=args.write)
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True, default=str))
    else:
        print(render_action(result))
    return int(result.get("exit_code", 1))


if __name__ == "__main__":
    sys.exit(main())
