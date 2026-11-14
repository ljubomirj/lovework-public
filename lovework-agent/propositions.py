#!/usr/bin/env python3
"""The Shoulder W4 — propositions, the learned layer (docs/35; schema docs/18).

A **proposition** is a learned claim about the principal — the output of
reflection (``reflect.py``), the input to persona updates. This module owns
the ledger and the state machine; the discipline is ch18's, enforced in code:

* **Append-only transitions.** A proposition row is created once; every
  status change is a ``proposition_transition`` event appended to the same
  ledger. Readers fold the latest transition per id — a prior status or
  rejection is never overwritten, and the history is recoverable.
* **Evidence is reactions, not model beliefs.** Evidence refs must exist in
  ``reactions.jsonl``; prior assessments and repeated crawls are not
  independent corroboration (docs/18: they can identify a question, never
  confirm the model's own belief).
* **Support and contradictions are counted on both sides.** A contested
  proposition is surfaced, not averaged away.
* **Rejected propositions persist.** Re-proposing an unchanged rejected
  claim is refused unless material new evidence refs are supplied.
* **Acceptance is not activation.** ``render_matcher_block()`` renders only
  ``accepted`` propositions, bounded, with omission and conflict counts —
  but the matcher does not load it yet; activation is a versioned policy
  decision (docs/18: activation ≠ acceptance), deliberately not taken here.

Usage::

    ../venv/bin/python3 propositions.py list --profile lj [--status accepted]
    ../venv/bin/python3 propositions.py transition --profile lj \\
        --id pr_0001 --to accepted --note "reviewed with LJ 2026-10-01"
    ../venv/bin/python3 propositions.py render --profile lj
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reactions import _read_jsonl  # noqa: E402 — shared JSONL reader

logger = logging.getLogger("propositions")

SCHEMA_VERSION = 1
LEDGER_NAME = "propositions.jsonl"

CATEGORIES = ("taste", "reach", "fit", "action", "constraint", "trajectory")
STATUSES = ("hypothesis", "proposed", "accepted", "rejected", "superseded")
CONFIDENCES = ("low", "medium", "high")
AXES = ("fit", "reach", "flourish", "action")
DIRECTIONS = ("up", "down", "unclear")

# legal transitions (docs/18 state machine + the single-statement rule:
# one explicit principal statement can establish an authored preference,
# so hypothesis → accepted is legal when the principal decides it)
_TRANSITIONS = {
    ("hypothesis", "proposed"), ("hypothesis", "accepted"),
    ("hypothesis", "rejected"),
    ("proposed", "accepted"), ("proposed", "rejected"),
    ("accepted", "superseded"), ("accepted", "rejected"),
    ("hypothesis", "superseded"), ("proposed", "superseded"),
}

_MAX_CLAIM_LEN = 240


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_claim(claim: str) -> str:
    return re.sub(r"\s+", " ", claim.strip().lower().rstrip("."))


# ── the ledger ───────────────────────────────────────────────────────────


def _ledger_path(state_root: Path) -> Path:
    return state_root / "dataset" / LEDGER_NAME


def _append(state_root: Path, row: dict) -> None:
    path = _ledger_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def fold(state_root: Path) -> list[dict]:
    """All propositions with their latest status (creation + transitions)."""
    latest: dict[str, dict] = {}
    for row in _read_jsonl(_ledger_path(state_root)):
        kind = row.get("event_type")
        if kind == "proposition":
            latest[row["proposition_id"]] = dict(row)
        elif kind == "proposition_transition":
            pid = row.get("proposition_id")
            if pid in latest:
                latest[pid]["status"] = row["to"]
                latest[pid]["decided_at"] = row.get("at")
                latest[pid]["decided_by"] = row.get("decided_by")
                if row.get("decision_note"):
                    latest[pid]["decision_note"] = row["decision_note"]
                if row.get("superseded_by"):
                    latest[pid]["superseded_by"] = row["superseded_by"]
    return sorted(latest.values(), key=lambda r: r.get("proposed_at", ""))


# ── propose ──────────────────────────────────────────────────────────────


def propose(
    state_root: Path,
    *,
    claim: str,
    category: str,
    scope: dict,
    evidence_refs: list[str],
    contradictions: list[dict] | None = None,
    confidence: str = "low",
    support_detail: str = "",
    proposed_by: str = "reflect",
    policy_effect: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Create one proposition row (status: hypothesis or proposed — decided
    by the support rule: ≥2 distinct reaction refs ⇒ proposed, else
    hypothesis; docs/18's state machine)."""
    now = now or datetime.now(timezone.utc)
    claim = claim.strip()
    if not claim or len(claim) > _MAX_CLAIM_LEN:
        raise ValueError(
            f"claim must be one declarative sentence (≤{_MAX_CLAIM_LEN} chars)")
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}")
    if confidence not in CONFIDENCES:
        raise ValueError(f"confidence must be one of {CONFIDENCES}")
    if not scope or not scope.get("domain"):
        raise ValueError("scope.domain is required — a claim without scope "
                         "collapses contradictions into overwrites (docs/18)")

    reaction_ids = {r.get("reaction_id")
                    for r in _read_jsonl(state_root / "dataset"
                                         / "reactions.jsonl")}
    valid_refs, invalid = [], []
    for ref in evidence_refs:
        (valid_refs if ref in reaction_ids else invalid).append(ref)
    if invalid:
        raise ValueError(
            f"evidence refs not found in reactions.jsonl: {invalid} "
            "(prior assessments and model beliefs are not independent "
            "corroboration — docs/18)")
    distinct = len(set(valid_refs))
    if not distinct:
        raise ValueError("a proposition needs at least one reaction ref")

    # re-propose guard: identical claim already rejected → refuse
    for existing in fold(state_root):
        if (existing.get("status") == "rejected"
                and _normalize_claim(existing.get("claim", ""))
                == _normalize_claim(claim)):
            raise ValueError(
                f"claim was rejected ({existing['proposition_id']}); "
                "re-proposing an unchanged rejected claim is refused — "
                "supply material new evidence as a new claim (docs/18)")

    evidence = [{"type": "reaction", "ref": ref} for ref in valid_refs]
    status = "proposed" if distinct >= 2 else "hypothesis"
    counter = _counter(state_root)
    row = {
        "schema_version": SCHEMA_VERSION,
        "event_type": "proposition",
        "proposition_id": f"pr_{counter:04d}",
        "claim": claim,
        "category": category,
        "scope": scope,
        "evidence": evidence,
        "support": distinct,
        "support_detail": support_detail or (
            f"{distinct} distinct reaction(s); verify common cause and scope"
            if distinct >= 2 else
            "single expression — hypothesis until corroborated (docs/18)"),
        "contradictions": contradictions or [],
        "confidence": confidence,
        "status": status,
        "proposed_at": now.isoformat(timespec="seconds"),
        "proposed_by": proposed_by,
        "decided_at": None,
        "decided_by": None,
        "decision_note": "",
        "proposed_policy_effect": policy_effect or {},
    }
    _append(state_root, row)
    return row


def _counter(state_root: Path) -> int:
    n = 0
    for row in _read_jsonl(_ledger_path(state_root)):
        if row.get("event_type") == "proposition":
            n += 1
    return n + 1


# ── transition ───────────────────────────────────────────────────────────


def transition(
    state_root: Path,
    *,
    proposition_id: str,
    to: str,
    decided_by: str = "principal",
    note: str = "",
    superseded_by: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Append a transition event (never overwrite). Enforces the machine."""
    now = now or datetime.now(timezone.utc)
    if to not in STATUSES:
        raise ValueError(f"to must be one of {STATUSES}")
    current = {p["proposition_id"]: p for p in fold(state_root)}
    if proposition_id not in current:
        raise ValueError(f"unknown proposition: {proposition_id}")
    frm = current[proposition_id]["status"]
    if frm == to:
        raise ValueError(f"already {to}")
    if (frm, to) not in _TRANSITIONS:
        raise ValueError(f"illegal transition {frm} → {to} (docs/18 machine)")
    if to == "superseded" and not superseded_by:
        raise ValueError("superseded needs superseded_by")
    if to == "rejected" and not note:
        raise ValueError("rejection persists for the record — a reason is "
                         "required (docs/18)")
    row = {
        "schema_version": SCHEMA_VERSION,
        "event_type": "proposition_transition",
        "proposition_id": proposition_id,
        "from": frm,
        "to": to,
        "at": now.isoformat(timespec="seconds"),
        "decided_by": decided_by,
        "decision_note": note,
        "superseded_by": superseded_by,
    }
    _append(state_root, row)
    return row


# ── render: the join back to scoring (not wired — acceptance ≠ activation) ──


def render_matcher_block(state_root: Path, *, budget: int = 12) -> str:
    """Compact block of accepted, applicable propositions for the matcher
    prompt (docs/18's join-back shape). Bounded: at most ``budget`` claims,
    with omission and conflict counts stated — never every belief in every
    prompt. The matcher does not load this yet; wiring it is an activation
    decision requiring baseline + regression results first."""
    rows = [p for p in fold(state_root) if p.get("status") == "accepted"]
    conflicts = sum(1 for p in rows if p.get("contradictions"))
    lines = [f"[principal propositions: {min(len(rows), budget)} active"
             + (f", {len(rows) - budget} omitted" if len(rows) > budget else "")
             + (f", {conflicts} contested" if conflicts else "") + "]"]
    for p in rows[:budget]:
        flag = " [contested]" if p.get("contradictions") else ""
        lines.append(f"- ({p['category']}{flag}) {p['claim']} "
                     f"{{scope: {p['scope'].get('domain', '?')}}}")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="The Shoulder W4 — propositions (docs/18 stream 2)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list")
    p.add_argument("--profile", required=True)
    p.add_argument("--status", choices=STATUSES)
    p = sub.add_parser("transition")
    p.add_argument("--profile", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--to", required=True, choices=STATUSES)
    p.add_argument("--note", default="")
    p.add_argument("--by", default="principal")
    p.add_argument("--superseded-by")
    p = sub.add_parser("render")
    p.add_argument("--profile", required=True)
    args = parser.parse_args(argv)

    import config

    state_root = config.STATE_DIR / args.profile
    if not state_root.is_dir():
        print(f"no state dir: {state_root}", file=sys.stderr)
        return 66

    if args.command == "list":
        rows = fold(state_root)
        if args.status:
            rows = [r for r in rows if r["status"] == args.status]
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0

    if args.command == "transition":
        from writer_boundary import WriterBusy, standalone_writer_guard
        try:
            with standalone_writer_guard(
                    args.profile, mode="proposition", state_root=state_root,
                    run_id=f"prop-{uuid.uuid4().hex[:8]}"):
                row = transition(state_root, proposition_id=args.id,
                                 to=args.to, decided_by=args.by,
                                 note=args.note,
                                 superseded_by=args.superseded_by)
        except WriterBusy:
            print("[proposition] deferred — writer busy")
            return 75
        print(json.dumps(row, indent=2, sort_keys=True))
        return 0

    if args.command == "render":
        print(render_matcher_block(state_root))
        return 0
    return 64


if __name__ == "__main__":
    raise SystemExit(_main())
