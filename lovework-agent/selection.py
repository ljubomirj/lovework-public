#!/usr/bin/env python3
"""AS0 — shared capability-qualified selectors (docs/34 §AS0).

One result object, five selectors, one lineage ledger. The problem this
solves is stated in the AS0 gate:

    * a state bundle followed by a receipt, a failed acquisition, and a
      consumer ingest must yield **correct, different answers** to
      "latest result", "latest world observation" and "was it delivered";
    * receipt insertion cannot alter state selection or manufacture fresh
      acquisition;
    * a missing alias is **unknown**, never "unsealed".

The five selectors and the distinction the gate turns on:

``select_latest_result``      the newest bundle that *carries assessments* —
                             what the pipeline last produced. A receipt
                             child (email delivery, link recheck) creates a
                             bundle with no acquisition and no assessments:
                             it must NOT advance this selector.
``select_world_observation``  the newest bundle whose coverage shows a real
                             *acquisition* attempt (a crawl or consume, not
                             a child receipt and not a failed preflight).
``select_state_snapshot``     the newest bundle whose manifest actually
                             claims ``capabilities.state_snapshot`` **and**
                             whose primary assets are on disk — the
                             docs/34 §2 audit found status.py picked the
                             newest manifest irrespective of capability;
                             this selector pins the capability explicitly.
``select_execution``          the latest run record with independent
                             execution/capture/delivery states, plus stale-
                             running reconciliation against the writer
                             lease (a record saying "running" with no lease
                             holder and no live process is
                             ``running_stale``, not running).
``select_owner_corpus``       the shared wrapper over consume's sealed-owner
                             resolution, so every reader answers "which
                             corpus may a consumer read" identically.

**Lineage (box 2).** Pipeline rows in ``runs.jsonl`` and operation manifests
carry IDs in two families. ``bind_lineage`` records an evidence-supported
alias (append-only ``dataset/run_lineage.jsonl``); ``lineage_of`` returns
``bound`` / ``unknown`` — never a guess and never "unsealed". New runs
bound at creation via crawl_operation already share one id end-to-end; the
ledger is the reconciliation layer for everything older or direct-run.

**Capability qualification (box 5).** Every selection carries a
``qualification`` block: implementation (schema version, reader version),
assets (the files the selection's claim depends on, checked on disk),
integrity (manifest/inventory digest agreement — non-deep by default), and
dependency closure (inputs pinned?). A partial clone (assets missing) and
an old run (schema unknown) are first-class: the selection reports what it
can and cannot claim, with the reason.

No LLM, no network; reads only. CLI for humans and for the cockpit to call.
"""

from __future__ import annotations
import jsonl_io

import argparse
import json
import logging
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import strata_contract as sc

logger = logging.getLogger("selection")

SCHEMA_VERSION = 1
LINEAGE_NAME = "run_lineage.jsonl"

#: bundle assets each selector's claim depends on (existence-checked)
_RESULT_ASSETS = ("state/cache/jobs.csv", "outputs")

#: Coverage `source` prefixes that mean "we went and looked at the world"
#: (the operation-level markers a seal records for a crawl-like run).
OBSERVATION_MARKER_SOURCES = ("crawl", "consume", "reassess")

#: The complement: coverage `source` prefixes that describe the *run*, not the
#: market — a preflight check, a notification or token check, a receipt. Their
#: completion says nothing about what we last observed, so no reader may report
#: them as an observation of the world (2026-10-07: `status.py`'s "read the
#: world" column said `yes` for `preflight:notify-token`, which only became
#: visible once the receipt child stopped being read as the newest seal).
NON_OBSERVATION_PREFIXES = ("preflight", "receipt", "notify", "delivery", "seal")

#: attempt outcomes that mean "we went and looked" (a failed fetch is still an
#: attempted observation; skipped / not_due / preflight are not)
ACQUISITION_OUTCOMES = ("complete", "partial", "empty", "failed")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── the shared result object ─────────────────────────────────────────────


@dataclass
class Selection:
    """The one result object every selector returns (AS0 gate: 'all
    interfaces share the same result object')."""
    question: str                     # "latest_result" | "world_observation" | …
    principal: str
    at: str
    status: str                       # ok | unknown | not_applicable
    selected_id: str | None = None    # bundle/run id when ok
    reason: str = ""                  # always filled — why this status
    qualification: dict = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _bundle_manifests(strata_root: Path) -> list[dict]:
    """All sealed bundles' manifests, newest-seal-first, idempotent order."""
    out = []
    if not strata_root.is_dir():
        return out
    for bundle in strata_root.iterdir():
        if (not bundle.is_dir() or bundle.is_symlink()
                or bundle.name.startswith((".", "history-index"))):
            continue
        path = bundle / "manifest.json"
        if not path.is_file():
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sealed = ((manifest.get("clock") or {}).get("sealed_at")) or ""
        out.append({"run_id": bundle.name, "sealed_at": sealed,
                    "manifest": manifest, "bundle": bundle})
    out.sort(key=lambda b: (b["sealed_at"], b["run_id"]), reverse=True)
    return out


def _qualify(entry: dict, *, asset_paths: tuple[str, ...]) -> dict:
    """Capability qualification for one bundle: implementation, assets,
    integrity, dependency closure. What it cannot check non-deep, it says
    so — a qualification block is claims, not proofs."""
    manifest = entry["manifest"]
    bundle: Path = entry["bundle"]
    caps = manifest.get("capabilities") or {}
    assets = {}
    for rel in asset_paths:
        target = bundle / rel
        if rel == "outputs":
            assets["outputs"] = sum(
                1 for o in (manifest.get("artifacts") or {}).get("outputs", [])
                if (bundle / o.get("path", "_")).is_file())
        else:
            assets[rel] = target.is_file()
    inputs_pinned = bool((manifest.get("inputs") or []))
    return {
        "implementation": {
            "manifest_schema": manifest.get("schema_version"),
            "reader": SCHEMA_VERSION,
            "known_schema": manifest.get("schema_version") == 1,
        },
        "assets": assets,
        "integrity": {
            "inventory_present": (bundle / "inventory.jsonl").is_file(),
            "deep_verified": False,       # deep verify is an explicit act
        },
        "dependency_closure": {
            "inputs_pinned": inputs_pinned,
            "note": "" if inputs_pinned else "no pinned inputs on this "
            "bundle — input-complete replay unavailable",
        },
        "capability_claims": caps,
    }


#: (the outcome tuple now sits with OBSERVATION_MARKER_SOURCES above)


#: Is this sealed bundle an operation child (a receipt)? The predicate lives in
#: `strata_contract` — the module that defines `operation_kind` and the child
#: rule — so storage, selectors and the cockpit all ask the same question.
def _is_child(entry: dict) -> bool:
    return sc.is_child_manifest(entry["manifest"])


def _has_acquisition(entry: dict) -> bool:
    """Did this bundle's operation attempt a real acquisition? Receipt
    children and preflight-only bundles did not — the gate's 'receipt
    insertion cannot manufacture fresh acquisition'. A *failed* fetch is
    still an attempted observation (the world question asks what we last
    went and looked at, not what succeeded)."""
    if _is_child(entry):
        return False
    for attempt in (entry["manifest"].get("coverage") or []):
        source = str(attempt.get("source", ""))
        if (source.startswith(OBSERVATION_MARKER_SOURCES)
                and attempt.get("outcome") in ACQUISITION_OUTCOMES):
            return True
    return False


def _carries_assessments(entry: dict) -> bool:
    """Does this bundle carry judgment output of its OWN — an original
    report/artifact? A receipt child has an output but is bookkeeping, not
    a result; and every bundle's ledger_copies freeze the *cumulative*
    ledger, which is history, not this run's production (the gate's
    failed-acquisition case: capture partial, no report, ledger freeze
    present — not a result)."""
    if _is_child(entry):
        return False
    outputs = (entry["manifest"].get("artifacts") or {}).get("outputs") or []
    return any(o.get("original") for o in outputs)


# ── the selectors ────────────────────────────────────────────────────────


def select_latest_result(state_root: Path, *,
                         now: str | None = None) -> Selection:
    """Newest bundle carrying assessments — what the pipeline produced."""
    principal = state_root.name
    for entry in _bundle_manifests(state_root / "strata"):
        if not _carries_assessments(entry):
            continue
        qual = _qualify(entry, asset_paths=_RESULT_ASSETS)
        return Selection(
            question="latest_result", principal=principal,
            at=now or _utc_now_iso(), status="ok",
            selected_id=entry["run_id"],
            reason="newest sealed bundle carrying assessment output",
            qualification=qual,
            evidence_refs=[f"strata/{entry['run_id']}/manifest.json"])
    return Selection(
        question="latest_result", principal=principal,
        at=now or _utc_now_iso(), status="unknown",
        reason="no sealed bundle carries assessments (receipts and preflight "
               "failures do not count as results)")


def select_world_observation(state_root: Path, *,
                             now: str | None = None) -> Selection:
    """Newest bundle whose operation actually attempted acquisition — the
    latest evidence about the world. A receipt child inserted after must
    not change this answer (the gate)."""
    principal = state_root.name
    for entry in _bundle_manifests(state_root / "strata"):
        if not _has_acquisition(entry):
            continue
        qual = _qualify(entry, asset_paths=("state/cache/jobs.csv",))
        return Selection(
            question="world_observation", principal=principal,
            at=now or _utc_now_iso(), status="ok",
            selected_id=entry["run_id"],
            reason="newest sealed bundle with an acquisition attempt "
                   "(crawl/consume/reassess coverage, non-child)",
            qualification=qual,
            evidence_refs=[f"strata/{entry['run_id']}/manifest.json"])
    return Selection(
        question="world_observation", principal=principal,
        at=now or _utc_now_iso(), status="unknown",
        reason="no sealed bundle shows an acquisition attempt")


def select_state_snapshot(state_root: Path, *,
                          now: str | None = None) -> Selection:
    """Newest bundle that *claims* state_snapshot AND has the assets on
    disk — capability-pinned, unlike newest-manifest-wins."""
    principal = state_root.name
    for entry in _bundle_manifests(state_root / "strata"):
        caps = (entry["manifest"].get("capabilities") or {})
        if not caps.get("state_snapshot"):
            continue
        qual = _qualify(entry, asset_paths=("state/cache/jobs.csv",
                                            "state/wiki/index.md"))
        if not qual["assets"].get("state/cache/jobs.csv"):
            # claims the capability but the asset is missing (partial
            # clone): first-class case — report, don't crash
            return Selection(
                question="state_snapshot", principal=principal,
                at=now or _utc_now_iso(), status="ok",
                selected_id=entry["run_id"],
                reason="capability claimed; registry asset missing on this "
                       "clone — snapshot readable except lifecycle",
                qualification=qual,
                evidence_refs=[f"strata/{entry['run_id']}/manifest.json"])
        return Selection(
            question="state_snapshot", principal=principal,
            at=now or _utc_now_iso(), status="ok",
            selected_id=entry["run_id"],
            reason="newest state_snapshot-qualified bundle with assets",
            qualification=qual,
            evidence_refs=[f"strata/{entry['run_id']}/manifest.json"])
    return Selection(
        question="state_snapshot", principal=principal,
        at=now or _utc_now_iso(), status="unknown",
        reason="no sealed bundle claims the state_snapshot capability")


def select_execution(state_root: Path, *,
                     now: str | None = None) -> Selection:
    """Latest terminal run record with independent states, plus
    stale-running reconciliation against the writer lease."""
    principal = state_root.name
    runs_dir = state_root / "cache" / "runs"
    best = None
    if runs_dir.is_dir():
        for path in sorted(runs_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            finished = record.get("finished_at") or ""
            started = record.get("started_at") or ""
            key = finished or started
            if best is None or key > best[0]:
                best = (key, record, path.name)

    lease_held = (state_root / "strata" / "writer.lock").exists() and \
        _lease_has_holder(state_root)

    if best is None:
        return Selection(question="execution", principal=principal,
                         at=now or _utc_now_iso(), status="unknown",
                         reason="no run records")
    _, record, filename = best
    status = record.get("status", "unknown")
    if status == "running" and not lease_held:
        execution = "running_stale"
        reason = ("record says running but the writer lease has no holder "
                  "and no attempt is active — interrupted, never closed")
    elif status == "running":
        execution = "running_confirmed"
        reason = "record running and the writer lease is held"
    else:
        execution = status
        reason = f"terminal record ({status})"
    return Selection(
        question="execution", principal=principal,
        at=now or _utc_now_iso(),
        status="ok" if execution != "unknown" else "unknown",
        selected_id=record.get("run_id", filename),
        reason=reason,
        qualification={
            "execution": execution,
            "capture": record.get("capture_status"),
            "delivery": (record.get("notification") or {}).get("status"),
            "exit_code": record.get("exit_code"),
            "lease_held": lease_held,
        },
        evidence_refs=[f"cache/runs/{filename}"])


def _lease_has_holder(state_root: Path) -> bool:
    """Best-effort: does any process hold the principal's writer flock?
    We take the lock non-blockingly ourselves — success means nobody else
    holds it (flock is exclusive); failure means someone does."""
    import fcntl
    lock_path = state_root / "strata" / "writer.lock"
    if not lock_path.is_file():
        return False
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        os.close(fd)
        return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def select_owner_corpus(owner_state_root: Path, *,
                        now: str | None = None) -> Selection:
    """Which sealed corpus may a consumer read? Wraps consume's resolver so
    every interface agrees; blocked is honest, live fallback never."""
    principal = owner_state_root.name
    try:
        from consume import resolve_owner_corpus
        corpus = resolve_owner_corpus(principal,
                                      state_root=owner_state_root)
    except Exception as exc:                         # noqa: BLE001
        return Selection(question="owner_corpus", principal=principal,
                         at=now or _utc_now_iso(), status="unknown",
                         reason=f"resolver failed: {exc}")
    if corpus is None:
        return Selection(
            question="owner_corpus", principal=principal,
            at=now or _utc_now_iso(), status="unknown",
            reason="no eligible owner seal — consume defers (never a live "
                   "fallback); this is 'no corpus yet', not 'unsealed'")
    return Selection(
        question="owner_corpus", principal=principal,
        at=now or _utc_now_iso(), status="ok",
        selected_id=corpus.run_id,
        reason="newest eligible sealed owner corpus (state_snapshot + "
               "registry asset)",
        qualification={"manifest_digest": corpus.manifest_digest,
                       "registry": str(corpus.registry_csv),
                       "enrichment": str(corpus.enrichment_dir)},
        evidence_refs=[f"strata/{corpus.run_id}/manifest.json"])


# ── lineage (box 2) ──────────────────────────────────────────────────────


def _lineage_path(state_root: Path) -> Path:
    return state_root / "dataset" / LINEAGE_NAME


def bind_lineage(state_root: Path, *, pipeline_run_id: str,
                 operation_run_id: str, evidence: str,
                 now: str | None = None) -> dict:
    """Record an evidence-supported alias (append-only). Refuses to
    re-bind a pipeline id to a *different* operation — a conflict is a
    signal, not an update."""
    rows = _read_lineage(state_root)
    for row in rows:
        if row.get("pipeline_run_id") == pipeline_run_id:
            if row.get("operation_run_id") == operation_run_id:
                return row                      # idempotent
            raise ValueError(
                f"lineage conflict: {pipeline_run_id} already bound to "
                f"{row.get('operation_run_id')} (recorded "
                f"{row.get('bound_at')}); refusing to re-bind to "
                f"{operation_run_id} without new evidence")
    row = {
        "schema_version": SCHEMA_VERSION,
        "binding_id": uuid.uuid4().hex,
        "pipeline_run_id": pipeline_run_id,
        "operation_run_id": operation_run_id,
        "evidence": evidence,
        "bound_at": now or _utc_now_iso(),
    }
    path = _lineage_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return row


def _read_lineage(state_root: Path) -> list[dict]:
    rows = []
    path = _lineage_path(state_root)
    if not path.is_file():
        return rows
    for line in jsonl_io.lines(path):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def lineage_of(state_root: Path, pipeline_run_id: str) -> dict:
    """bound | unknown — a missing alias is unknown, never 'unsealed'."""
    for row in _read_lineage(state_root):
        if row.get("pipeline_run_id") == pipeline_run_id:
            return {"status": "bound", **{
                k: v for k, v in row.items() if k != "pipeline_run_id"}}
    # evidence-supported quick path: an identical id in both families
    manifest_ids = {e["run_id"] for e in
                    _bundle_manifests(state_root / "strata")}
    if pipeline_run_id in manifest_ids:
        return {"status": "bound", "operation_run_id": pipeline_run_id,
                "evidence": "same id in both families (created bound)"}
    return {"status": "unknown",
            "reason": "no lineage row and no shared id — unknown, not "
                      "unsealed and not missing"}


# ── CLI ──────────────────────────────────────────────────────────────────


QUESTIONS = {
    "result": select_latest_result,
    "world": select_world_observation,
    "snapshot": select_state_snapshot,
    "execution": select_execution,
    "corpus": select_owner_corpus,
}


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AS0 — capability-qualified selectors (docs/34)")
    parser.add_argument("--profile", required=True)
    parser.add_argument("question", choices=sorted(QUESTIONS))
    parser.add_argument("--lineage", metavar="RUN_ID",
                        help="lineage status for a pipeline run id")
    parser.add_argument("--bind", nargs=2, metavar=("PIPELINE", "OP"),
                        help="record an evidence-supported alias")
    parser.add_argument("--evidence", default="manual")
    args = parser.parse_args(argv)

    import config

    state_root = config.STATE_DIR / args.profile
    if not state_root.is_dir():
        print(f"no state dir: {state_root}", file=sys.stderr)
        return 66

    if args.bind:
        row = bind_lineage(state_root, pipeline_run_id=args.bind[0],
                           operation_run_id=args.bind[1],
                           evidence=args.evidence)
        print(json.dumps(row, indent=2, sort_keys=True))
        return 0
    if args.lineage:
        print(json.dumps(lineage_of(state_root, args.lineage),
                         indent=2, sort_keys=True))
        return 0

    selection = QUESTIONS[args.question](state_root)
    print(json.dumps(selection.to_dict(), indent=2, sort_keys=True))
    return 0 if selection.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
