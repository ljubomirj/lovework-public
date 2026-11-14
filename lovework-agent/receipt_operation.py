#!/usr/bin/env python3
"""Late receipts as **child operations** (docs/28 §WP1b box ⑥).

A run's email receipt — and any later link recheck or heal — happens *after* its
stratum is sealed, and a sealed stratum is never reopened (strata_contract
rule 3). Those facts are therefore recorded as child operations: their own
manifest, ``operation_kind="child"``, ``child_of=<parent run id>``, capturing
their own artifacts only (no state copy — the state belongs to the parent and is
already sealed).

**Best-effort by contract.** Delivery must never fail because its evidence could
not be sealed, and a failed seal must never be reported as a success: the
function returns ``None`` on failure and the caller says so separately, keeping
the run ledger's own receipt as the watchdog-visible record it already is.

Usage from a caller that has just delivered something:

    from receipt_operation import seal_receipt
    bundle = seal_receipt(principal="kj", parent_run_id="full-kj-…",
                          kind="email", facts={"status": "sent",
                                               "message_id": "1a0b…"})
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
import strata_contract as sc
from operation import OperationOutcome, run_operation

logger = logging.getLogger("lovework.receipt")

RECEIPT_KINDS = ("email", "link_recheck", "heal")


def receipt_path(principal: str, kind: str, parent_run_id: str,
                 state_root: Path | None = None) -> Path:
    """Where the human-readable receipt lives before it is sealed.

    Under the principal's `cache/receipts/` so it is ordinary state: inspectable,
    captured by later outer runs, and never inside the parent's sealed bundle.
    """
    root = Path(state_root) if state_root else config.STATE_DIR / principal
    return root / "cache" / "receipts" / f"{kind}-{parent_run_id}.json"


def seal_receipt(*, principal: str, parent_run_id: str, kind: str,
                 facts: dict[str, Any], state_root: Path | None = None) -> Path | None:
    """Seal *facts* as a child operation of *parent_run_id*.

    Returns the child bundle path, or ``None`` when the receipt could not be
    sealed (no strata store, a held lock, an engine error). Never raises into
    the delivery path.
    """
    if kind not in RECEIPT_KINDS:
        raise ValueError(f"unknown receipt kind: {kind}")
    root = Path(state_root) if state_root else config.STATE_DIR / principal
    parent = root / "strata" / parent_run_id / "manifest.json"
    if not parent.is_file():
        # A receipt attaches to a *sealed* parent; without one there is nothing
        # to be a child of (and a typo'd run id would otherwise create a stray
        # orphan bundle).
        logger.info("no sealed parent %s for %s; receipt not sealed",
                    parent_run_id, principal)
        return None

    path = receipt_path(principal, kind, parent_run_id, state_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": kind,
        "principal": principal,
        "parent_run_id": parent_run_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **facts,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")

    # `mode="attempt"` is the schema's word for a child operation, and `role`
    # must be non-empty: a receipt has no profile role, so the kind names it.
    identity = sc.RunIdentity(
        run_id=f"receipt-{kind}-{parent_run_id}",
        principal=principal,
        role=kind,
        mode="attempt",
        host=os.uname().nodename,
        operation_kind="child",
        child_of=parent_run_id,
    )

    def work(ctx) -> OperationOutcome:
        ctx.record_attempt(source=f"receipt:{kind}", scope=parent_run_id,
                           outcome="complete",
                           notes=str(facts.get("status") or kind))
        return OperationOutcome(
            execution="succeeded",
            capture="none",              # a child captures artifacts only
            report_path=path,            # registered as the original output
            delivery=str(facts.get("status") or "not_applicable"),
        )

    try:
        result = run_operation(root, identity, work)
    except Exception as e:  # busy lock, engine error — never break delivery
        logger.warning("receipt %s for %s not sealed: %s", kind, parent_run_id, e)
        return None
    logger.info("sealed %s receipt as child operation %s", kind, result.bundle_path)
    return result.bundle_path
