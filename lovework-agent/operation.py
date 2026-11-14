#!/usr/bin/env python3
"""Outer-operation runner (WP1b-B) — the one entry real writers call.

Binds the pieces WP1b requires into a single call:

    WriterBoundary (OS locks + attempt records + ambient lease)
      → RunContext (pinned effective inputs at START)
      → work(ctx)  — the actual operation (pipeline call, replay, consume…);
                     may record attempts/gaps on the context; nested
                     WriterBoundary calls reuse the ambient lease
      → exact artifact registration (the produced report, by path — never
        "newest by mtime")
      → StrataEngine.seal (evidence captured whether the operation
        succeeded OR failed; a failed run still leaves a sealed bundle)

Failures never vanish: if ``work`` raises, the operation seals with
``execution="failed"`` plus a capture gap naming the error, and the
exception is re-raised for the caller's exit-code handling. Lock
contention raises :class:`WriterBusy` after leaving a non-mutating attempt
record. Exiting without reaching the seal is impossible through this
runner — the lease-exit path in ``writer_boundary`` is the safety net for
direct users.

Not yet wired into ``lovework-crawl.sh`` / ``main.py`` — that mechanical
integration (and the first live seal on host-a) is the next slice; this
module is the tested seam it plugs into.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import strata_contract as sc
from strata import SealResult, StrataEngine
from writer_boundary import WriterBoundary

@dataclass
class OperationOutcome:
    """What ``work`` reports back to the runner."""

    execution: str                          # succeeded | failed | interrupted
    capture: str = "complete"               # complete | partial | failed | none
    qa: str = "unknown"                     # passed | failed | unknown | n/a
    delivery: str = "not_applicable"        # sent | failed | pending | n/a
    report_path: Path | None = None         # exact live-tree artifact produced
    note: str | None = None
    extra_gaps: list[sc.Gap] = field(default_factory=list)


Work = Callable[[Any], OperationOutcome]


def run_operation(
    state_root: Path,
    run: sc.RunIdentity,
    work: Work,
    *,
    pin_inputs: dict[str, Path | bytes] | None = None,
    input_complete: bool = False,
    shared_resources: tuple[str, ...] = (),
    shared_root: Path | None = None,
    clock=None,
    engine_class=StrataEngine,
) -> SealResult:
    """Run one outer operation under the writer boundary and seal it.

    Raises ``WriterBusy`` on contention (attempt recorded, nothing else
    touched). Re-raises anything ``work`` raises — but only AFTER sealing
    the failed operation, so failed runs leave evidence, not silence.
    """
    state_root = Path(state_root)
    engine = engine_class(state_root)
    with WriterBoundary(
        state_root, run,
        shared_resources=shared_resources, shared_root=shared_root,
        clock=clock, engine_class=engine_class,
    ) as lease:
        context = lease.context
        for role, origin in (pin_inputs or {}).items():
            context.pin_input(role, origin)

        failure: BaseException | None = None
        outcome: OperationOutcome | None = None
        try:
            outcome = work(context)
        except BaseException as exc:  # seal the failure, then re-raise
            failure = exc

        if outcome is not None and outcome.note:
            context.record_gap(kind="other", reason=outcome.note)

        if outcome is not None and outcome.report_path is not None:
            try:
                _register_report(context, Path(outcome.report_path))
            except BaseException as exc:
                failure = failure or exc

        if failure is not None:
            context.record_gap(
                kind="capture",
                reason=f"operation raised: "
                       f"{type(failure).__name__}: {failure}")

        result = engine.seal(
            context,
            execution=(outcome.execution if outcome and not failure
                       else "failed"),
            capture=(outcome.capture if outcome else "partial"),
            qa=(outcome.qa if outcome else "unknown"),
            delivery=(outcome.delivery if outcome else "not_applicable"),
            input_complete=input_complete,
        )
        if failure is not None:
            raise failure
        return result


def _register_report(context, live_path: Path) -> sc.OutputRef:
    """Copy the exact produced artifact into the bundle as the original
    output — the anti-"newest report by mtime" rule. A delayed heal of an
    old report can never masquerade as this operation's output."""
    live_path = Path(live_path)
    if not live_path.is_file():
        raise ValueError(f"reported artifact does not exist: {live_path}")
    rel = f"outputs/{live_path.name}"
    dest = context.staging / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(live_path, dest)
    return context.register_artifact("report", rel, original=True)
