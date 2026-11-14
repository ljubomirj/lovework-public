#!/usr/bin/env python3
"""Process-safe writer boundary for LoveWork state mutations (WP1b-A).

The lock files in this module are deliberately boring, permanent inode
anchors.  They are opened and locked with ``fcntl.flock`` and are never
renamed or unlinked.  Human-readable progress is a separate atomic JSON file;
it is metadata only and is never consulted to decide whether a writer may run.

An operation first writes its own durable attempt record, then acquires every
requested resource in canonical order, and only then calls
``StrataEngine.begin``.  Consequently, lock contention leaves evidence but no
staging directory and no mutation of another operation's workspace.

Nested Python calls reuse the ambient lease only when identity and resources
are identical.  Cross-principal nesting and lock-set changes are rejected:
expanding a lock set after taking its first lock would reintroduce deadlocks.
"""

from __future__ import annotations

import contextvars
import fcntl
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import strata_contract as sc
from strata import StrataEngine


_RESOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_AMBIENT_LEASE: contextvars.ContextVar[WriterLease | None] = \
    contextvars.ContextVar("lovework_writer_lease", default=None)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validated_name(value: str, kind: str) -> str:
    if not _RESOURCE_RE.fullmatch(value):
        raise ValueError(
            f"{kind} must match {_RESOURCE_RE.pattern!r}, got {value!r}")
    return value


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Durably replace *path* without ever replacing a lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except BaseException:
        # Preserve a failed temporary write for diagnosis.  Lock files are
        # never involved in this replacement path and are never unlinked.
        raise


@dataclass(frozen=True)
class WriterResource:
    """One canonically named kernel-lock resource."""

    key: str
    lock_path: Path


class WriterBusy(RuntimeError):
    """Raised when another process holds one of the requested resources."""

    def __init__(self, resource: WriterResource, attempt_path: Path):
        self.resource = resource
        self.attempt_path = attempt_path
        super().__init__(
            f"writer resource busy: {resource.key} "
            f"(attempt recorded at {attempt_path})")


class NestedWriterError(RuntimeError):
    """Raised when a nested call does not exactly match the ambient writer."""


class WriterLease:
    """The acquired resources and single RunContext owned by the outer call."""

    def __init__(
        self,
        *,
        context,
        run: sc.RunIdentity,
        resources: tuple[WriterResource, ...],
        attempt_id: str,
        attempt_path: Path,
        progress_path: Path,
        boundary: WriterBoundary,
    ):
        self.context = context
        self.run_context = context
        self.run = run
        self.resources = resources
        self.attempt_id = attempt_id
        self.attempt_path = attempt_path
        self.progress_path = progress_path
        self._boundary = boundary
        self._progress: dict[str, Any] = {
            "run_id": run.run_id,
            "attempt_id": attempt_id,
            "principal": run.principal,
            "status": "running",
            "started_at": boundary.started_at,
            "updated_at": boundary._now(),
            "resources": [resource.key for resource in resources],
            "authoritative": False,
        }

    @property
    def resource_keys(self) -> tuple[str, ...]:
        return tuple(resource.key for resource in self.resources)

    def update_progress(self, **fields: Any) -> dict[str, Any]:
        """Atomically publish advisory progress while this lease is active."""
        if _AMBIENT_LEASE.get() is not self:
            raise RuntimeError("writer lease is not the active ambient lease")
        self._progress.update(fields)
        self._progress["updated_at"] = self._boundary._now()
        self._progress["authoritative"] = False
        _atomic_json(self.progress_path, self._progress)
        return dict(self._progress)


def current_writer() -> WriterLease | None:
    """Return the ambient lease for nested Python entry points, if any."""
    return _AMBIENT_LEASE.get()


#: Set by an outer operation for the children it spawns. Its **presence** is
#: what matters ("I am a child; the parent already holds the lease"), never its
#: value — a child must not re-acquire the same flock, and must not be judged by
#: an id it cannot verify.
OUTER_RUN_ENV = "LOVEWORK_OUTER_RUN_ID"


def outer_operation_run_id() -> str | None:
    """The run id of the outer operation this process belongs to, if any."""
    return os.getenv(OUTER_RUN_ENV, "").strip() or None


@contextmanager
def standalone_writer_guard(principal: str, *, mode: str, state_root: Path,
                            run_id: str | None = None,
                            shared: tuple[str, ...] = ()):
    """Take the principal's writer lease for a **direct** invocation.

    The plan's rule for WP1b box ① is "integrate or explicitly exclude, never a
    silent bypass": an entry point invoked on its own (a human running
    `main.py`, a Hermes script calling `consume.py`, the MCP `run_pipeline`
    tool) must not run while a seal or another crawl is mutating the same
    principal's state. It therefore takes the lease for its duration and lets
    the *busy* path do the refusing — `WriterBusy` propagates to the caller,
    which reports exit 75 exactly as the wrapper does.

    ``shared`` names cross-principal resources (e.g. ``("manual",)`` for
    MANUAL.md, the root projection several principals' operations can want).
    A per-principal lock cannot serialise those, so they get their own anchor
    under the shared ``state/`` root — the WP1b gate's explicit case.

    A child of an operation skips this entirely: its parent holds the lease and
    the ambient/child marker says so, so re-acquiring would deadlock or
    double-lock. Refusing by default was rejected deliberately — host-a's
    `lovework-kj-consume.sh`/`lovework-vj-consume.sh` and host-b's launchd
    plist call these scripts directly, and a default refusal would break them.
    """
    if outer_operation_run_id():
        yield None
        return
    state_root = Path(state_root)
    if not state_root.is_dir():
        # Nothing to protect: a fresh clone, a temp fixture, or a principal
        # whose state has not been created yet. Locking would only litter.
        yield None
        return
    identity = sc.RunIdentity(
        run_id=run_id or f"direct-{principal}-{os.getpid()}",
        principal=principal,
        role="",
        mode=mode,
        host=os.uname().nodename,
    )
    with WriterBoundary(state_root, identity,
                        shared_resources=shared) as lease:
        yield lease


class WriterBoundary:
    """Acquire one principal writer resource plus optional shared resources.

    ``state_root`` is ``state/<principal>``.  Shared locks default to the
    containing ``state/`` directory, so different principals contend on the
    same stable files.  Acquisition is non-blocking by design: callers receive
    :class:`WriterBusy` and can report/defer without guessing from progress.
    """

    def __init__(
        self,
        state_root: Path,
        run: sc.RunIdentity,
        *,
        shared_resources: Iterable[str] = (),
        shared_root: Path | None = None,
        progress_path: Path | None = None,
        clock=None,
        engine_class=StrataEngine,
    ):
        self.state_root = Path(state_root).resolve()
        self.run = run
        self.clock = clock
        self.engine_class = engine_class
        self.started_at = self._now()
        self.attempt_id = uuid.uuid4().hex
        self.attempt_path = (
            self.state_root / "strata" / "attempts" /
            f"{self.attempt_id}.json")
        self.progress_path = Path(progress_path).resolve() \
            if progress_path is not None \
            else self.state_root / "strata" / "writer-progress.json"

        principal = _validated_name(run.principal, "principal")
        shared_base = Path(shared_root).resolve() \
            if shared_root is not None else self.state_root.parent
        names = {_validated_name(name, "shared resource")
                 for name in shared_resources}
        resources = [WriterResource(
            key=f"principal:{principal}",
            lock_path=self.state_root / "strata" / "writer.lock")]
        resources.extend(
            WriterResource(
                key=f"shared:{name}",
                lock_path=shared_base / ".writer-locks" / f"{name}.lock")
            for name in names)
        self.resources = tuple(sorted(resources, key=lambda item: item.key))

        self._descriptors: list[int] = []
        self._lease: WriterLease | None = None
        self._token: contextvars.Token | None = None
        self._nested = False
        self._entered = False

    def _now(self) -> str:
        return self.clock() if self.clock else _utc_now_iso()

    @property
    def resource_keys(self) -> tuple[str, ...]:
        return tuple(resource.key for resource in self.resources)

    def _signature(self) -> tuple[Any, ...]:
        return (
            str(self.state_root),
            json.dumps(self.run.to_dict(), sort_keys=True),
            tuple((resource.key, str(resource.lock_path))
                  for resource in self.resources),
        )

    def _attempt(self, status: str, **fields: Any) -> dict[str, Any]:
        record = {
            "attempt_id": self.attempt_id,
            "run": self.run.to_dict(),
            "status": status,
            "started_at": self.started_at,
            "updated_at": self._now(),
            "resources": [resource.key for resource in self.resources],
        }
        if self.attempt_path.is_file():
            previous = json.loads(self.attempt_path.read_text(encoding="utf-8"))
            previous.update(record)
            record = previous
        record.update(fields)
        _atomic_json(self.attempt_path, record)
        return record

    def _acquire_resources(self) -> None:
        for resource in self.resources:
            resource.lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                resource.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                self._release_resources()
                self._attempt(
                    "contended", finished_at=self._now(),
                    contended_resource=resource.key)
                raise WriterBusy(resource, self.attempt_path) from None
            except BaseException:
                os.close(descriptor)
                self._release_resources()
                raise
            self._descriptors.append(descriptor)

    def _release_resources(self) -> None:
        while self._descriptors:
            descriptor = self._descriptors.pop()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _reuse_or_reject_nested(self, ambient: WriterLease) -> WriterLease:
        if ambient.run.principal != self.run.principal:
            raise NestedWriterError(
                "cross-principal writer nesting is forbidden: "
                f"{ambient.run.principal!r} -> {self.run.principal!r}")
        if ambient._boundary._signature() != self._signature():
            requested = set(self.resource_keys)
            held = set(ambient.resource_keys)
            if requested > held:
                reason = "nested writer resource expansion is forbidden"
            elif requested != held:
                reason = "nested writer resources must exactly match the outer lease"
            else:
                reason = "nested writer identity must exactly match the outer operation"
            raise NestedWriterError(reason)
        self._nested = True
        self._lease = ambient
        return ambient

    def __enter__(self) -> WriterLease:
        if self._entered:
            raise RuntimeError("WriterBoundary instances are single-use")
        self._entered = True

        ambient = _AMBIENT_LEASE.get()
        if ambient is not None:
            return self._reuse_or_reject_nested(ambient)

        # This record must exist before the first flock attempt.  A contender
        # may update only this file; progress belongs to the current lock owner.
        self._attempt("waiting_for_lock")
        try:
            self._acquire_resources()
            self._attempt("running", acquired_at=self._now())
            engine = self.engine_class(self.state_root)
            context = engine.begin(
                self.run, attempt=self.attempt_id,
                started_at=self.started_at, clock=self.clock)
            self._lease = WriterLease(
                context=context, run=self.run, resources=self.resources,
                attempt_id=self.attempt_id, attempt_path=self.attempt_path,
                progress_path=self.progress_path, boundary=self)
            self._token = _AMBIENT_LEASE.set(self._lease)
            self._lease.update_progress()
            return self._lease
        except BaseException as error:
            if not isinstance(error, WriterBusy):
                self._attempt(
                    "failed", finished_at=self._now(),
                    error=f"{type(error).__name__}: {error}")
            if self._token is not None:
                _AMBIENT_LEASE.reset(self._token)
                self._token = None
            self._release_resources()
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self._nested:
            return False
        if self._lease is None:
            return False

        unsealed = not self._lease.context.sealed
        if unsealed:
            # The operation never sealed. Mark the staging record so the
            # on-disk state says "interrupted" instead of "running"
            # forever; bytes remain for StrataEngine.recover.
            self._lease.context.abandon("lease exited without seal")
        if exc_type is not None:
            status = "failed"
        elif unsealed:
            status = "exited_unsealed"
        else:
            status = "finished"
        fields: dict[str, Any] = {"finished_at": self._now()}
        if exc_value is not None:
            fields["error"] = f"{exc_type.__name__}: {exc_value}"
        try:
            self._attempt(status, **fields)
            self._lease.update_progress(status=status, **fields)
        finally:
            if self._token is not None:
                _AMBIENT_LEASE.reset(self._token)
                self._token = None
            self._release_resources()
        return False
