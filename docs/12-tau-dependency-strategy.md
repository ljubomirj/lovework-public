# Chapter 12 — Tau Dependency Strategy

> **Audience:** builders deciding how LoveWork should use Tau without losing
> control of the intelligence layer.
> **See also:** [`11-agentic-intelligence-harness.md`](11-agentic-intelligence-harness.md).

## Decision

Tau is a pinned harness dependency principal, not an owned product layer.

LoveWork may use Tau for the generic agent runtime: loop, tools, transcript,
events, cancellation, and session mechanics. LoveWork must not outsource its
person model, decision policy, decision ledger, reflection loop, or evaluation
harness to Tau.

The current local Tau source is:

```text
path:   ~/LJ-AI-agents/tau/
commit: 64f7f9ae3b92737d006691f8efdff264d9345f21
status: clean; re-verified 2026-07-09 (still the tip of the local clone)
```

This is the source pin for design work. It is not yet a runtime dependency in
`lovework-agent/pyproject.toml`. The same commit is hard-pinned in
`agent_runtime.py` (`TAU_SOURCE_COMMIT`).

## Why not add Tau to pyproject immediately?

The Python-version blocker this section used to describe is now cleared. As of
the 3.13 migration:

- Tau declares `requires-python = ">=3.12"`, `name = "tau-ai"`.
- LoveWork now declares `requires-python = ">=3.13"` and runs on Python 3.13 on
  gigul2.
- `agent_runtime.tau_dependency_status()` reports Tau as `ready` at the version
  level: `python_ok=True`, `can_import_tau_agent=True`.

So Python compatibility is no longer a reason to defer. The remaining reason is
that the Tau-backed runner does not exist yet: `agent_runtime.TauBackedRuntime`
is a stub that raises `TauRuntimeUnavailable` ("not implemented yet"). Adding
the dependency before the adapter compiles against Tau's real public surface
would commit LoveWork to an API that has not been exercised against actual
tasks.

Therefore the near-term pin is still documentary and architectural:

- record the Tau source commit;
- design the adapter boundary;
- use the local Tau clone as the reference implementation;
- add a runtime dependency only when the LoveWork harness adapter is ready and
  Python compatibility is resolved.

## Control boundary

Tau may own:

| Tau-owned runtime surface | LoveWork usage |
|---|---|
| Agent loop | Run a bounded LoveWork task. |
| Tool call protocol | Invoke LoveWork tools with typed arguments. |
| Events | Stream progress to CLI/dashboard/logs. |
| Transcript/session mechanics | Keep conversation and tool-result state coherent. |
| Cancellation/continuation | Let long-running agent tasks be interrupted and resumed. |

LoveWork must own:

| LoveWork-owned intelligence surface | Reason |
|---|---|
| Person model | The durable product asset. |
| Decision policy | The user-visible judgment layer. |
| Decision ledger | Training/evaluation data for learning with the client. |
| Reflection loop | Converts outcomes into profile/policy changes. |
| Evaluation fixtures | Prevents regressions in hard-won judgment. |
| Profile/policy patches | Must be reviewable and auditable. |

The adapter must make Tau replaceable. If Tau disappears, breaks, changes
license, or grows in the wrong direction, LoveWork should keep its intelligence
layer intact and swap the runtime shell.

## Integration options

### Option A — Git-pinned dependency

Use when:

- Python compatibility holds — Tau (`>=3.12`) under LoveWork's current Python
  3.13 (now satisfied; re-check on any version change on either side);
- the LoveWork adapter compiles against Tau's public package surface;
- tests cover the Tau-backed runner.

Shape:

```toml
dependencies = [
  "tau-ai @ git+https://github.com/alejandro-ao/tau.git@64f7f9ae3b92737d006691f8efdff264d9345f21",
]
```

Upgrade rule: bump the commit deliberately in a separate change, run tests,
and record the new pin in this chapter.

### Option B — Local path dependency during development

Use while iterating locally:

```toml
[project.optional-dependencies]
harness-dev = [
  "tau-ai @ file:///home/ljubomir/LJ-AI-agents/tau",
]
```

This is useful for experiments but must not become the production/default
dependency because it depends on LJ's machine layout.

### Option C — Vendor minimal `tau_agent`

Use if Tau's package/runtime is too unstable, too broad, or version-mismatched.
Vendor only the minimal harness primitives, with attribution and source commit:

```text
lovework-agent/vendor/tau_agent/
lovework-agent/vendor/TAU_SOURCE.md
```

The vendored subset should be small:

- messages
- tools
- events
- loop
- harness

Do not vendor `tau_coding`, Textual UI, release machinery, or coding-specific
tools. LoveWork is not a coding agent.

### Option D — Internal reimplementation

Use if the desired surface is small enough:

```text
lovework-agent/lovework_harness/
  messages.py
  tools.py
  events.py
  loop.py
  runner.py
```

This sacrifices upstream reuse but maximizes control. It is viable because the
Tau core is intentionally small and readable.

## Recommended path

Use this sequence (steps 1–2 are done; step 3 is current):

1. ✅ **Done:** Tau is an external reference clone at `~/LJ-AI-agents/tau/`
   and the commit pin is recorded here and in `agent_runtime.py`.
2. ✅ **Done:** LoveWork's internal harness adapter interface
   (`LoveWorkAgentRuntime` in `agent_runtime.py`) is defined before importing
   Tau, with a working `LocalLoveWorkRuntime` and a stub `TauBackedRuntime`.
3. **Now:** build a real Tau-backed experimental runner behind that adapter
   (replace the stub), using fake providers/tools first.
4. **Only then:** choose between Git-pinned dependency, local dev extra, or
   vendoring the minimal subset.

The adapter should be framed around LoveWork concepts, not Tau classes — this
is the interface that already exists in `agent_runtime.py`:

```python
class LoveWorkAgentRuntime(Protocol):
    def run_task(self, task: str, *, profile_name: str, role: str) -> AgentRun:
        ...

    def continue_task(self, run_id: str) -> AgentRun:
        ...

    def cancel(self, run_id: str) -> None:
        ...
```

Tau can implement that interface. It should not leak throughout the codebase.

## Dependency acceptance checklist

Before Tau becomes a default dependency:

- LoveWork supports the Python version Tau requires, or Tau supports LoveWork's
  Python version.
- The dependency is pinned to a commit or exact released version.
- The Tau-backed runner is behind a LoveWork adapter.
- The existing pipeline and MCP tools still work without Tau.
- Tests cover one successful Tau-backed task using fake providers/tools.
- Tests cover fallback behavior when Tau is unavailable.
- The decision ledger and intelligence layer do not import Tau.
- The upgrade procedure is documented.

## Upgrade procedure

When updating Tau:

1. Record old commit and new commit.
2. Read Tau release notes or inspect diff for `tau_agent` changes.
3. Run LoveWork harness tests and the full LoveWork suite.
4. Update this chapter's source pin.
5. Add a `JOURNAL.md` line summarizing the bump.

No silent floating dependency. No unpinned branch dependency. No dependency on
LJ's local path in production/default installs.

## Current state

As of 2026-07-09:

- Tau is inspected and pinned by source commit, both here and in
  `agent_runtime.py` (`TAU_SOURCE_COMMIT`). The pinned commit
  (`64f7f9a…`) is still the tip of the local clone and the clone is clean.
- LoveWork moved from Python 3.11 to **Python 3.13**
  (`requires-python = ">=3.13"`), clearing the version blocker this chapter
  previously described. `tau_dependency_status()` now reports Tau as `ready`
  (`python_ok=True`, `can_import_tau_agent=True`).
- The LoveWork-owned adapter interface `LoveWorkAgentRuntime` exists
  (`agent_runtime.py`), backed by a working `LocalLoveWorkRuntime` and a stub
  `TauBackedRuntime` that raises `TauRuntimeUnavailable`.
- Tau is **still not** in `lovework-agent/pyproject.toml`. LoveWork still
  depends on `pi-agent==0.1.0` for the existing agent loop.
- The next implementation step is a real `TauBackedRuntime` behind the
  existing adapter, with fake-provider tests — still not a dependency change.
