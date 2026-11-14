#!/usr/bin/env python3
"""Strata contract v1 — the frozen interface between WP0 and WP1a/WP2a.

This module IS the WP0 deliverable "freeze manifest v1, RunContext
ownership, exact allowlist" (docs/28 §WP0). It defines *contracts only*:
typed manifest builders, schema validation, the capture allowlist and the
``RunContext`` protocol. The seal engine (WP1a: ``strata.py``,
``run_context.py``) implements these; readers (WP2a: ``asof.py``) consume
them. Nothing here performs I/O on live state.

Ownership rules (binding on every implementation):

1. One **outer operation** owns the run context and the writer lock for a
   principal. Pipeline source calls, incremental aggregation and
   post-processing are child attempts inside that operation.
2. Nested Python calls reuse the ambient context — they never acquire a
   second independently-held lock and never seal.
3. Late receipts (notification, link rechecks, heals) are **child
   operations** with their own manifest, ``operation_kind="child"``,
   ``child_of=<outer run>``. A sealed stratum is never reopened.
4. Execution reads pinned inputs through injected loaders; it must not
   reload live profile files (a principal lock cannot stop manual edits
   elsewhere — captured bytes are the only truth).
5. IDs from other systems (wrapper ``full-lj-<ts>-<pid>``, pipeline UUID)
   join only via explicit ``run.aliases`` — never by nearby timestamps.
6. ``lineage.previous_manifest`` is the storage-generation predecessor
   (link-dest target); causal parentage is ``run.parent_run_id``. They
   need not agree.
7. A manifest never contains its own digest; the digest is recorded by the
   next manifest and by ``history-index/strata.jsonl``.

Usage::

    from strata_contract import Manifest, RunIdentity, ...
    m = Manifest.build(...)
    m.validate()          # JSON Schema + invariants
    m.digest()            # sha256:... over canonical JSON
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

SCHEMA_VERSION = 1
MIN_READER_VERSION = 1

SCHEMA_PATH = Path(__file__).resolve().parent / "strata_manifest.schema.json"

# ── Capture allowlist v1 ─────────────────────────────────────────────────
#
# What a stratum may capture from state/<principal>/ at seal time, and what
# it must never capture. Frozen here so WP1a, the inventory and reviewers
# share one definition. `history_inventory.WALKED_TREES` walks the same
# five include roots.

CAPTURE_ALLOWLIST_VERSION = 1

INCLUDE_TREES: tuple[str, ...] = (
    "cache", "wiki", "dataset", "sources", "logs",
    # The agentic loop's own residue (LJ's ruling, 2026-10-11): the tick's
    # receipts, envelopes and drafts are evidence of what the loop did, and
    # "what did the tick do as of date D" must be answerable from a seal like
    # every other point-in-time question. It is the loop's *claims* that are
    # sealed here, not its effects: a filled pack lives in `applications/`,
    # which stays out of the bundle — the seal records what the tick concluded,
    # the pack remains a living artifact.
    "loop",
)

EXCLUDE_RULES: tuple[tuple[str, str], ...] = (
    ("cache/archive", "legacy recursive snapshot; inventoried separately as paleo-strata"),
    ("cache/crawl.lock", "transient process state"),
    ("cache/runs", "mutable wrapper records; their facts enter the manifest instead"),
    ("cache/.DS_Store", "platform noise"),
    # A host-local cache: its content is the ledger's size and mtime, which says
    # nothing about the loop and would make two identical states differ.
    ("loop/ledger-scan.json", "host-local scan cache"),
    (".DS_Store", "platform noise"),
    ("__pycache__", "build artifact"),
    ("*.pyc", "build artifact"),
    ("*.partial", "staging"),
    ("*.tmp", "staging"),
)

_CREDENTIAL_HINTS = ("credentials", ".env", "google_client_secret", "token.json")


#: `run.operation_kind` for a late child operation — a receipt (notification,
#: link recheck, heal) with its own manifest and `child_of` pointing at the
#: outer run (rule 3 above). A child is bookkeeping, never a result.
CHILD_OPERATION_KIND = "child"


def manifest_operation_kind(manifest: Any) -> str | None:
    """A bundle's `run.operation_kind`, from a manifest dict *or* an object.

    Both shapes are in use — `selection` walks raw JSON, `status.py` reads
    `strata.parse_manifest()` objects — and one reader keeps them from
    disagreeing about which bundles are results.
    """
    run = (manifest.get("run") if isinstance(manifest, dict)
           else getattr(manifest, "run", None))
    if run is None:
        return None
    if isinstance(run, dict):
        return run.get("operation_kind")
    return getattr(run, "operation_kind", None)


def is_child_manifest(manifest: Any) -> bool:
    """True when this sealed bundle is a child operation (bookkeeping).

    One authority for one fact (docs/27 §4b). The 2026-10-07 defect it closes:
    AS0 fixed "receipt children counted as results" inside `selection`, but
    `status.py` kept its own bundle enumeration — so the cockpit reported a
    receipt child as the newest seal, its seal count included children, and the
    "Source health (newest seal)" table described `receipt:email` instead of the
    crawl.
    """
    return manifest_operation_kind(manifest) == CHILD_OPERATION_KIND


def is_capturable(rel_path: str) -> tuple[bool, str | None]:
    """Return ``(capturable, exclusion_reason)`` for a state-relative path."""
    posix = PurePosixPath(rel_path)
    if posix.parts[0] not in INCLUDE_TREES:
        return False, "outside include trees"
    lowered = str(posix)
    parts = set(posix.parts)
    for hint in _CREDENTIAL_HINTS:
        if hint in lowered:
            return False, "credential-like path"
    for pattern, reason in EXCLUDE_RULES:
        if pattern.startswith("*"):
            if lowered.endswith(pattern[1:]):
                return False, reason
        elif lowered == pattern or lowered.startswith(pattern + "/") \
                or pattern in parts:
            return False, reason
    return True, None


# ── Path confinement ─────────────────────────────────────────────────────

#: Characters that make a bundle-relative path unsafe to trust. A *blocklist*,
#: not a character whitelist: real state filenames legitimately contain
#: characters a whitelist rejects — `+` in
#: `…/full-2026-07-25T09-00-00+01-00-notification_unresolved.json` — and the
#: first live VJ seal failed deep verification with 8 false "unconfined" verdicts
#: on exactly that (2026-09-20). Safety comes from the structural checks below
#: (no absolute path, no `..` component, no NUL/newline/backslash), which is what
#: traversal actually needs.
_FORBIDDEN_IN_PATH = ("\x00", "\n", "\r", "\\")


def confined(path: str) -> bool:
    """True when *path* is a safe relative path inside the bundle."""
    if not path or path.startswith("/"):
        return False
    if any(bad in path for bad in _FORBIDDEN_IN_PATH):
        return False
    parts = PurePosixPath(path).parts
    return bool(parts) and ".." not in parts


# ── Manifest builders (typed, but dumb: they hold data, not behavior) ────


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode()).hexdigest()


@dataclass
class RunIdentity:
    run_id: str
    aliases: dict[str, str] = field(default_factory=dict)
    parent_run_id: str | None = None
    operation_kind: str = "outer"          # outer | child
    child_of: str | None = None
    host: str = ""
    principal: str = ""
    role: str = ""
    mode: str = "live"                     # live|incremental|replay|consume|reassess|attempt

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "aliases": dict(self.aliases),
            "parent_run_id": self.parent_run_id,
            "operation_kind": self.operation_kind,
            "child_of": self.child_of,
            "host": self.host,
            "principal": self.principal,
            "role": self.role,
            "mode": self.mode,
        }


@dataclass
class ClockWindow:
    started_at: str
    sealed_at: str
    finished_at: str | None = None
    timezone: str = "UTC"
    observation_from: str | None = None
    observation_to: str | None = None
    input_cutoff: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "sealed_at": self.sealed_at,
            "timezone": self.timezone,
            "observation_window": {"from": self.observation_from,
                                   "to": self.observation_to},
            "input_cutoff": self.input_cutoff,
        }


@dataclass
class StatusSet:
    execution: str = "running"             # running|succeeded|failed|interrupted|recovered
    capture: str = "none"                  # complete|partial|failed|none
    qa: str = "unknown"                    # passed|failed|unknown|not_applicable
    delivery: str = "not_applicable"       # sent|failed|pending|not_applicable

    def to_dict(self) -> dict[str, Any]:
        return {"execution": self.execution, "capture": self.capture,
                "qa": self.qa, "delivery": self.delivery}


@dataclass
class SealedRef:
    run_id: str
    digest: str
    principal: str | None = None           # owner manifests only

    def to_dict(self) -> dict[str, Any]:
        d = {"run_id": self.run_id, "digest": self.digest}
        if self.principal is not None:
            d["principal"] = self.principal
        return d


@dataclass
class InputRef:
    role: str
    path: str
    origin: str
    sha256: str
    bytes: int
    origin_kind: str = "allowlisted_file"  # allowlisted_file|generated|declared_literal

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "path": self.path, "origin": self.origin,
                "origin_kind": self.origin_kind, "sha256": self.sha256,
                "bytes": self.bytes}


@dataclass
class CoverageAttempt:
    source: str
    scope: str
    attempt_id: str
    outcome: str                           # complete|partial|empty|failed|skipped|authwall|rate_limited|not_due
    started_at: str | None = None
    ended_at: str | None = None
    cache_use: str = "unknown"             # fresh|mixed|cached_only|unknown
    parser_version: str | None = None
    missing_dependencies: list[str] = field(default_factory=list)
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "scope": self.scope,
            "attempt_id": self.attempt_id, "outcome": self.outcome,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "cache_use": self.cache_use, "parser_version": self.parser_version,
            "missing_dependencies": list(self.missing_dependencies),
            "notes": self.notes,
        }


@dataclass
class Gap:
    kind: str                              # missing_input|coverage|capture|identity|other
    reason: str
    evidence_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason,
                "evidence_ref": self.evidence_ref}


@dataclass
class StateArtifact:
    path: str
    file_count: int
    logical_bytes: int
    copied_bytes: int
    linked_files: int
    excluded: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "file_count": self.file_count,
            "logical_bytes": self.logical_bytes, "copied_bytes": self.copied_bytes,
            "linked_files": self.linked_files, "excluded": list(self.excluded),
        }


@dataclass
class OutputRef:
    role: str
    path: str
    sha256: str
    bytes: int
    original: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "path": self.path, "sha256": self.sha256,
                "bytes": self.bytes, "original": self.original}


@dataclass
class LedgerCopy:
    path: str
    sha256: str
    rows: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "rows": self.rows}


@dataclass
class Capabilities:
    state_snapshot: bool = False
    recorded_output: bool = False
    observation_history: bool = False
    input_complete: bool = False
    reconstructable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_snapshot": self.state_snapshot,
            "recorded_output": self.recorded_output,
            "observation_history": self.observation_history,
            "input_complete": self.input_complete,
            "reconstructable": self.reconstructable,
        }


@dataclass
class Manifest:
    run: RunIdentity
    clock: ClockWindow
    status: StatusSet = field(default_factory=StatusSet)
    previous_manifest: SealedRef | None = None
    owner_manifests: list[SealedRef] = field(default_factory=list)
    inputs: list[InputRef] = field(default_factory=list)
    coverage: list[CoverageAttempt] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    state: StateArtifact | None = None
    outputs: list[OutputRef] = field(default_factory=list)
    events: list[dict[str, str]] = field(default_factory=list)
    ledger_copies: list[LedgerCopy] = field(default_factory=list)
    capabilities: Capabilities = field(default_factory=Capabilities)
    file_inventory_count: int = 0
    file_inventory_digest: str = "sha256:" + "0" * 64

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run": self.run.to_dict(),
            "clock": self.clock.to_dict(),
            "status": self.status.to_dict(),
            "lineage": {
                "previous_manifest": (self.previous_manifest.to_dict()
                                      if self.previous_manifest else None),
                "owner_manifests": [o.to_dict() for o in self.owner_manifests],
            },
            "inputs": [i.to_dict() for i in self.inputs],
            "coverage": [c.to_dict() for c in self.coverage],
            "gaps": [g.to_dict() for g in self.gaps],
            "artifacts": {
                "state": self.state.to_dict() if self.state else {
                    "path": "state", "file_count": 0, "logical_bytes": 0,
                    "copied_bytes": 0, "linked_files": 0, "excluded": []},
                "outputs": [o.to_dict() for o in self.outputs],
                "events": list(self.events),
                "ledger_copies": [l.to_dict() for l in self.ledger_copies],
            },
            "capabilities": self.capabilities.to_dict(),
            "integrity": {
                "file_inventory_count": self.file_inventory_count,
                "file_inventory_digest": self.file_inventory_digest,
            },
            "reader_versions": {"min_reader": MIN_READER_VERSION},
            "capture_allowlist_version": CAPTURE_ALLOWLIST_VERSION,
        }

    def digest(self) -> str:
        """Digest of this manifest's canonical JSON. Recorded by the NEXT
        manifest and the index — never inside this one."""
        return digest_of(self.to_dict())

    def validate(self) -> list[str]:
        """Validate against the JSON Schema plus contract invariants.

        Returns a list of problems (empty == valid). Never raises for
        content problems; schema loading failures raise.
        """
        problems: list[str] = []
        doc = self.to_dict()
        problems.extend(jsonschema_problems(doc))
        problems.extend(_invariant_problems(self))
        return problems


# ── Validation ───────────────────────────────────────────────────────────

_SCHEMA_CACHE: dict[str, Any] = {}


def _load_schema() -> Any:
    if "schema" not in _SCHEMA_CACHE:
        _SCHEMA_CACHE["schema"] = json.loads(SCHEMA_PATH.read_text())
    return _SCHEMA_CACHE["schema"]


def jsonschema_problems(doc: Any) -> list[str]:
    import jsonschema

    validator = jsonschema.Draft202012Validator(_load_schema())
    return [
        f"schema: {'/'.join(str(p) for p in e.absolute_path) or '<root>'}: "
        f"{e.message}"
        for e in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    ]


def _invariant_problems(m: Manifest) -> list[str]:
    problems: list[str] = []
    for ref in (m.previous_manifest, *m.owner_manifests):
        if ref is not None and not confined(f"x/{ref.run_id}") and "/" in ref.run_id:
            problems.append(f"lineage run_id not confined: {ref.run_id}")
    for i in m.inputs:
        if not confined(i.path):
            problems.append(f"input path not confined: {i.path}")
    for o in m.outputs:
        if not confined(o.path):
            problems.append(f"output path not confined: {o.path}")
    for e in m.events:
        if not confined(e.get("path", "")):
            problems.append(f"event path not confined: {e.get('path')}")
    for l in m.ledger_copies:
        if not confined(l.path):
            problems.append(f"ledger copy path not confined: {l.path}")
    if m.run.operation_kind == "child" and not m.run.child_of:
        problems.append("child operation must set run.child_of")
    if m.run.operation_kind == "outer" and m.run.child_of:
        problems.append("outer operation must not set run.child_of")
    if m.capabilities.reconstructable and not m.capabilities.input_complete:
        problems.append("capabilities.reconstructable requires input_complete")
    if m.capabilities.recorded_output and not any(
            o.original for o in m.outputs):
        problems.append("capabilities.recorded_output requires an original output")
    # State capture applies to outer operations; a child operation (late
    # receipt, recheck, heal) captures its own artifacts only.
    if (m.run.operation_kind == "outer"
            and m.status.capture in ("complete", "partial")
            and m.state is None):
        problems.append("capture claimed but no state artifact recorded")
    return problems


# ── The RunContext contract (WP1a implements; frozen here) ───────────────


class RunContext(Protocol):
    """The single owner of a run's identity, clock, inputs and capture.

    WP1a's ``run_context.py`` implements this protocol. The rules in the
    module docstring are binding; the signatures below are the frozen
    surface WP2a/WP3a code may rely on.
    """

    @property
    def principal(self) -> str: ...

    @property
    def role(self) -> str: ...

    @property
    def mode(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def aliases(self) -> dict[str, str]: ...

    @property
    def parent_run_id(self) -> str | None: ...

    def now(self) -> str:
        """Current UTC ISO timestamp from the run's pinned decision clock
        (never a live wall-clock read inside execution)."""
        ...

    def pin_input(self, role: str, origin: Path | bytes,
                  origin_kind: str = "allowlisted_file") -> InputRef:
        """Copy effective input bytes into the bundle at run START and
        return the reference. Execution resolves inputs through
        ``resolve_input`` only — live files are never reloaded."""
        ...

    def resolve_input(self, role: str) -> bytes:
        """Return the pinned bytes for *role*; raise KeyError with the
        missing-dependency reason when absent (never fall back to live)."""
        ...

    def register_artifact(self, role: str, path: str, *,
                          original: bool = False) -> OutputRef:
        """Register an output/event/ledger artifact produced by this
        operation (path relative to the bundle)."""
        ...

    def record_attempt(self, source: str, scope: str, outcome: str, *,
                       attempt_id: str | None = None,
                       started_at: str | None = None,
                       ended_at: str | None = None,
                       cache_use: str = "unknown",
                       parser_version: str | None = None,
                       missing_dependencies: list[str] | None = None,
                       notes: str | None = None) -> CoverageAttempt:
        """Record one scoped source attempt at acquisition time."""
        ...

    def record_gap(self, kind: str, reason: str,
                   evidence_ref: str | None = None) -> Gap:
        ...

    def seal(self, *, execution: str, capture: str,
             qa: str = "unknown", delivery: str = "not_applicable",
             previous: SealedRef | None = None,
             owner_manifests: list[SealedRef] | None = None) -> Manifest:
        """Freeze and atomically publish the bundle; returns the sealed
        manifest. Called exactly once, by the outer operation."""
        ...
