#!/usr/bin/env python3
"""RunContext — the frozen RunContext protocol's reference implementation.

One instance owns ONE operation's identity, decision clock, pinned inputs,
coverage/gap records and artifact registry, handing everything to
``StrataEngine.seal`` at the end. The ownership rules in
``strata_contract``'s docstring are binding: nested calls reuse the ambient
context; only the outer operation seals; execution resolves inputs through
``resolve_input`` (pinned bytes), never by re-reading live files.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import strata_contract as sc


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_identity(*, principal: str, mode: str, host: str = "",
                     role: str = "general", run_id: str = "",
                     aliases: dict[str, str] | None = None,
                     parent_run_id: str | None = None,
                     operation_kind: str = "outer",
                     child_of: str | None = None) -> sc.RunIdentity:
    """Convenience builder. Callers that already own IDs (wrapper, pipeline)
    pass them explicitly so aliases join the systems honestly."""
    if not run_id:
        import uuid
        run_id = f"{host or 'local'}-{uuid.uuid4().hex[:12]}" \
            if host else uuid.uuid4().hex
    return sc.RunIdentity(
        run_id=run_id, aliases=dict(aliases or {}),
        parent_run_id=parent_run_id, operation_kind=operation_kind,
        child_of=child_of, host=host, principal=principal, role=role,
        mode=mode)


class RunContext:
    """Implements ``strata_contract.RunContext``. Not user-constructed:
    ``StrataEngine.begin()`` creates it so staging and attempt records stay
    consistent."""

    def __init__(self, engine, run: sc.RunIdentity, staging: Path, *,
                 started_at: str, clock=None, timezone_name: str = "UTC"):
        self._engine = engine
        self.run = run
        self.staging = Path(staging)
        self.started_at = started_at
        self._clock = clock or _utc_now_iso
        self.timezone = timezone_name
        self.observation_from: str | None = None
        self.observation_to: str | None = None
        self.input_cutoff: str | None = None

        self.inputs: list[sc.InputRef] = []
        self.coverage: list[sc.CoverageAttempt] = []
        self.gaps: list[sc.Gap] = []
        self.outputs: list[sc.OutputRef] = []
        self.events: list[dict[str, str]] = []
        self._pinned: dict[str, Path] = {}
        self._sealed = False

    # -- identity (protocol) ---------------------------------------------

    @property
    def principal(self) -> str:
        return self.run.principal

    @property
    def role(self) -> str:
        return self.run.role

    @property
    def mode(self) -> str:
        return self.run.mode

    @property
    def run_id(self) -> str:
        return self.run.run_id

    @property
    def aliases(self) -> dict[str, str]:
        return dict(self.run.aliases)

    @property
    def parent_run_id(self) -> str | None:
        return self.run.parent_run_id

    def now(self) -> str:
        """Decision clock: injected in tests, wall UTC in production."""
        return self._clock()

    @property
    def sealed(self) -> bool:
        return self._sealed

    def abandon(self, reason: str) -> None:
        """Mark this operation's staging as interrupted (lease exited
        without a seal). The staging attempt record flips from ``running``
        to ``interrupted`` so the on-disk state is honest; bytes stay for
        ``StrataEngine.recover``. No-op after a successful seal."""
        if self._sealed:
            return
        import json
        record_path = self.staging / "attempt.json"
        record: dict = {}
        if record_path.is_file():
            try:
                record = json.loads(record_path.read_text())
            except (ValueError, OSError):
                record = {}
        record["status"] = "interrupted"
        record["abandoned_at"] = self.now()
        record["abandon_reason"] = reason
        record_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = record_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        import os as _os
        _os.replace(tmp, record_path)

    # -- inputs (protocol) -------------------------------------------------

    def _input_path(self, role: str) -> Path:
        # "profile:soul" → inputs/profile/soul (role grammar allows ':')
        return self.staging / "inputs" / role.replace(":", "/")

    def pin_input(self, role: str, origin: Path | bytes,
                  origin_kind: str = "allowlisted_file") -> sc.InputRef:
        """Copy effective input bytes into the bundle at run START."""
        if role in self._pinned:
            raise ValueError(f"input already pinned: {role}")
        import hashlib

        if isinstance(origin, bytes):
            payload, origin_desc = origin, "(declared literal)"
            origin_kind = "declared_literal" \
                if origin_kind == "allowlisted_file" else origin_kind
        else:
            origin = Path(origin)
            payload = origin.read_bytes()
            origin_desc = str(origin)
        dest = self._input_path(role)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        ref = sc.InputRef(
            role=role, path=dest.relative_to(self.staging).as_posix(),
            origin=origin_desc, origin_kind=origin_kind,
            sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
            bytes=len(payload))
        self.inputs.append(ref)
        self._pinned[role] = dest
        return ref

    def resolve_input(self, role: str) -> bytes:
        """Pinned bytes only — never a live fallback."""
        path = self._pinned.get(role)
        if path is None or not path.is_file():
            raise KeyError(
                f"input not pinned: {role} (missing dependency — no "
                f"live fallback is permitted)")
        return path.read_bytes()

    # -- artifacts / coverage / gaps (protocol) -----------------------------

    def register_artifact(self, role: str, path: str, *,
                          original: bool = False) -> sc.OutputRef:
        import hashlib

        if not sc.confined(path):
            raise ValueError(f"artifact path not confined to bundle: {path}")
        full = self.staging / path
        if not full.is_file():
            raise ValueError(f"artifact not written yet: {path}")
        payload = full.read_bytes()
        ref = sc.OutputRef(
            role=role, path=path,
            sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
            bytes=len(payload), original=original)
        self.outputs.append(ref)
        return ref

    def record_attempt(self, source: str, scope: str, outcome: str, *,
                       attempt_id: str | None = None,
                       started_at: str | None = None,
                       ended_at: str | None = None,
                       cache_use: str = "unknown",
                       parser_version: str | None = None,
                       missing_dependencies: list[str] | None = None,
                       notes: str | None = None) -> sc.CoverageAttempt:
        attempt = sc.CoverageAttempt(
            source=source, scope=scope,
            attempt_id=attempt_id or f"{self.run_id}-{len(self.coverage)+1}",
            outcome=outcome, started_at=started_at or self.now(),
            ended_at=ended_at or self.now(), cache_use=cache_use,
            parser_version=parser_version,
            missing_dependencies=list(missing_dependencies or []),
            notes=notes)
        self.coverage.append(attempt)
        return attempt

    def record_gap(self, kind: str, reason: str,
                   evidence_ref: str | None = None) -> sc.Gap:
        gap = sc.Gap(kind=kind, reason=reason, evidence_ref=evidence_ref)
        self.gaps.append(gap)
        return gap

    # -- seal (protocol) -----------------------------------------------------

    def seal(self, *, execution: str, capture: str,
             qa: str = "unknown", delivery: str = "not_applicable",
             previous: sc.SealedRef | None = None,
             owner_manifests: list[sc.SealedRef] | None = None):
        """Delegates to the engine; returns the sealed manifest."""
        result = self._engine.seal(
            self, execution=execution, capture=capture, qa=qa,
            delivery=delivery, previous=previous,
            owner_manifests=owner_manifests)
        return result.manifest
