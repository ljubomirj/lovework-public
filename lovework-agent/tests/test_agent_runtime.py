import pytest

import agent_runtime
from agent_runtime import (
    LocalLoveWorkRuntime,
    TauBackedRuntime,
    TauRuntimeUnavailable,
    tau_dependency_status,
)


class _FakeAgent:
    def __init__(self, output: str = "ok") -> None:
        self.output = output
        self.seen: list[str] = []

    def run(self, task: str) -> str:
        self.seen.append(task)
        return f"{self.output}: {task}"


def test_local_runtime_runs_task_with_fake_agent():
    runtime = LocalLoveWorkRuntime(agent_factory=lambda profile, role: _FakeAgent("answer"))

    run = runtime.run_task("show latest GO roles", profile_name="lj", role="general")

    assert run.status == "succeeded"
    assert run.output == "answer: show latest GO roles"
    assert run.profile_name == "lj"
    assert run.role == "general"
    assert run.runtime == "local"
    assert "run_succeeded" in run.events


def test_local_runtime_captures_agent_failure():
    def factory(profile, role):
        raise RuntimeError("boom")

    runtime = LocalLoveWorkRuntime(agent_factory=factory)
    run = runtime.run_task("x", profile_name="lj", role="general")

    assert run.status == "failed"
    assert run.error == "boom"
    assert "run_failed" in run.events


def test_local_runtime_continue_is_explicitly_unsupported():
    runtime = LocalLoveWorkRuntime(agent_factory=lambda profile, role: _FakeAgent())
    first = runtime.run_task("x", profile_name="lj", role="general")

    continued = runtime.continue_task(first.run_id)

    assert continued.status == "failed"
    assert "does not support resumable sessions" in continued.error


def test_tau_dependency_status_reports_python_mismatch(tmp_path):
    tau = tmp_path / "tau"
    tau.mkdir()
    (tau / "pyproject.toml").write_text('requires-python = ">=3.12"\n', encoding="utf-8")

    status = tau_dependency_status(tau_source_path=tau, python_version=(3, 11))

    assert status["source_exists"] is True
    assert status["requires_python"] == ">=3.12"
    assert status["python_ok"] is False
    assert status["can_import_tau_agent"] is False
    assert "requires Python >=3.12" in status["reason"]


def test_tau_runtime_unavailable_without_source(tmp_path):
    missing = tmp_path / "missing"
    status = tau_dependency_status(tau_source_path=missing, python_version=(3, 12))
    assert status["source_exists"] is False


def test_tau_backed_runtime_fails_when_probe_is_not_ready(monkeypatch):
    # Tau is intentionally not a runtime dependency yet; the class should fail
    # early instead of half-importing an unstable backend.
    monkeypatch.setattr(
        agent_runtime,
        "tau_dependency_status",
        lambda: {"can_import_tau_agent": False, "reason": "not ready"},
    )
    with pytest.raises(TauRuntimeUnavailable):
        TauBackedRuntime()
