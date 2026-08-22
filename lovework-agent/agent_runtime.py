"""LoveWork-owned agent runtime boundary.

The runtime interface is deliberately framed in LoveWork terms. Tau, pi-agent,
or a future internal harness can implement it, but the rest of LoveWork should
not depend on those implementation classes directly.
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Protocol


AgentRunStatus = Literal["succeeded", "failed", "cancelled"]
AgentFactory = Callable[[str, str], object]

TAU_SOURCE_PATH = Path.home() / "LJ-AI-agents" / "tau"
TAU_SOURCE_COMMIT = "64f7f9ae3b92737d006691f8efdff264d9345f21"


@dataclass(slots=True)
class AgentRun:
    """One completed or failed agent task."""

    run_id: str
    profile_name: str
    role: str
    task: str
    status: AgentRunStatus
    output: str = ""
    error: str = ""
    runtime: str = "local"
    started_at: str = ""
    finished_at: str = ""
    events: list[str] = field(default_factory=list)


class LoveWorkAgentRuntime(Protocol):
    """Stable runtime interface for LoveWork agent tasks."""

    def run_task(self, task: str, *, profile_name: str, role: str) -> AgentRun:
        """Run one bounded task for a principal profile."""
        ...

    def continue_task(self, run_id: str) -> AgentRun:
        """Continue a prior task if the runtime supports resumable sessions."""
        ...

    def cancel(self, run_id: str) -> None:
        """Request cancellation for a run if possible."""
        ...


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_run_id() -> str:
    return str(uuid.uuid4())


class LocalLoveWorkRuntime:
    """Runtime backed by the existing `LoveWorkAgent` implementation."""

    name = "local"

    def __init__(self, agent_factory: AgentFactory | None = None) -> None:
        self._agent_factory = agent_factory or self._default_agent_factory
        self._runs: dict[str, AgentRun] = {}
        self._cancelled: set[str] = set()

    @staticmethod
    def _default_agent_factory(profile_name: str, role: str) -> object:
        from agent import LoveWorkAgent

        return LoveWorkAgent.from_profile(profile_name, role=role)

    def run_task(self, task: str, *, profile_name: str, role: str) -> AgentRun:
        run_id = _new_run_id()
        started = _now()
        events = ["run_started", "agent_created"]
        try:
            agent = self._agent_factory(profile_name, role)
            if run_id in self._cancelled:
                run = AgentRun(
                    run_id=run_id,
                    profile_name=profile_name,
                    role=role,
                    task=task,
                    status="cancelled",
                    runtime=self.name,
                    started_at=started,
                    finished_at=_now(),
                    events=events + ["run_cancelled"],
                )
            else:
                output = getattr(agent, "run")(task)
                run = AgentRun(
                    run_id=run_id,
                    profile_name=profile_name,
                    role=role,
                    task=task,
                    status="succeeded",
                    output=str(output or ""),
                    runtime=self.name,
                    started_at=started,
                    finished_at=_now(),
                    events=events + ["run_succeeded"],
                )
        except Exception as exc:
            run = AgentRun(
                run_id=run_id,
                profile_name=profile_name,
                role=role,
                task=task,
                status="failed",
                error=str(exc),
                runtime=self.name,
                started_at=started,
                finished_at=_now(),
                events=events + ["run_failed"],
            )
        self._runs[run_id] = run
        return run

    def continue_task(self, run_id: str) -> AgentRun:
        previous = self._runs.get(run_id)
        if previous is None:
            raise KeyError(f"Unknown run_id: {run_id}")
        run = AgentRun(
            run_id=run_id,
            profile_name=previous.profile_name,
            role=previous.role,
            task=previous.task,
            status="failed",
            error="LocalLoveWorkRuntime does not support resumable sessions yet.",
            runtime=self.name,
            started_at=_now(),
            finished_at=_now(),
            events=["continue_unsupported"],
        )
        self._runs[run_id] = run
        return run

    def cancel(self, run_id: str) -> None:
        self._cancelled.add(run_id)


class TauRuntimeUnavailable(RuntimeError):
    """Raised when the Tau-backed runtime is requested but unavailable."""


class TauBackedRuntime:
    """Placeholder for the future Tau implementation behind the same boundary."""

    name = "tau"

    def __init__(self) -> None:
        status = tau_dependency_status()
        if not status["can_import_tau_agent"]:
            raise TauRuntimeUnavailable(
                "Tau runtime is not available yet. "
                "See docs/12-tau-dependency-strategy.md. "
                f"Reason: {status['reason']}"
            )

    def run_task(self, task: str, *, profile_name: str, role: str) -> AgentRun:
        raise TauRuntimeUnavailable("Tau-backed LoveWork runtime is not implemented yet.")

    def continue_task(self, run_id: str) -> AgentRun:
        raise TauRuntimeUnavailable("Tau-backed LoveWork runtime is not implemented yet.")

    def cancel(self, run_id: str) -> None:
        raise TauRuntimeUnavailable("Tau-backed LoveWork runtime is not implemented yet.")


def tau_dependency_status(
    *,
    tau_source_path: Path = TAU_SOURCE_PATH,
    python_version: tuple[int, int] | None = None,
) -> dict[str, object]:
    """Return current Tau readiness without importing or installing Tau."""
    version = python_version or (sys.version_info.major, sys.version_info.minor)
    pyproject = tau_source_path / "pyproject.toml"
    source_exists = tau_source_path.exists()
    requires_python = ""
    if pyproject.exists():
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("requires-python"):
                requires_python = line.split("=", 1)[1].strip().strip('"')
                break
    python_ok = version >= (3, 12)
    can_import = python_ok and source_exists
    reason = "ready"
    if not source_exists:
        reason = f"Tau source path not found: {tau_source_path}"
    elif not python_ok:
        reason = f"Tau requires Python >=3.12; current Python is {version[0]}.{version[1]}"
    return {
        "source_path": str(tau_source_path),
        "source_commit": TAU_SOURCE_COMMIT,
        "source_exists": source_exists,
        "requires_python": requires_python,
        "python_version": f"{version[0]}.{version[1]}",
        "python_ok": python_ok,
        "can_import_tau_agent": can_import,
        "reason": reason,
    }


def build_agent_runtime(runtime: str = "local") -> LoveWorkAgentRuntime:
    """Build a runtime by name."""
    if runtime == "local":
        return LocalLoveWorkRuntime()
    if runtime == "tau":
        return TauBackedRuntime()
    raise ValueError(f"Unknown runtime: {runtime}")
