"""
Sandboxed Python REPL for the agent.

A `run_python` tool that lets the LLM write code to explore data, batch-process,
and compose operations. This is the "RLM pattern" — instead of fixed tools
like web_search(query), give the LLM a REPL.

Approach:
- Run code in a subprocess with a short timeout
- Expose a small set of "agent variables" (the job registry, profile, etc.)
- Capture stdout + stderr, return as the tool result
- Optionally persist variables across calls (for multi-step analysis)

We use `python -I -S` for some isolation:
- `-I` = isolated mode (no user site, no PYTHONPATH)
- `-S` = don't import site
We then re-import only the modules we want to expose.

This is not a security-grade sandbox — it's a usability sandbox. The agent
runs on LJ's machine, so the LLM can read files and make network calls
(that's fine — the agent is meant to). The point is to give the LLM a
Python interpreter to write code in, not to lock it down.

For real isolation, use Pyodide (in-process) or a Docker container.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from pi_agent import AgentTool, AgentToolResult, TextContent

import config

logger = logging.getLogger(__name__)


SANDBOX_HEADER = '''
import sys, os, json
# Disable site packages — we re-add only what we need
sys.path.insert(0, {lovework_agent_path!r})

# Pre-import the modules we want available
import config
from job_registry import JobRegistry, JobRecord
from history import scan_history, PriorContact
from wiki_store import WikiEntry, WikiStore

# Default agent variables
registry = JobRegistry()
profile_name = {profile_name!r}
role = {role!r}

# Where to write persistent variables (for multi-call analysis)
_vars_file = {vars_file!r}
_persistent_vars = json.loads({initial_vars!r}) if {initial_vars} else {{}}
locals().update(_persistent_vars)
'''


def _make_sandbox_script(
    user_code: str,
    profile_name: str,
    role: str,
    vars_file: str,
    initial_vars: Dict[str, Any],
    lovework_agent_path: str,
) -> str:
    """Wrap user code with sandbox header that exposes agent variables."""
    header = SANDBOX_HEADER.format(
        lovework_agent_path=lovework_agent_path,
        profile_name=profile_name,
        role=role,
        vars_file=vars_file,
        initial_vars=json.dumps(initial_vars, default=str),
    )

    # Footer: write back any new variables the user added at module level
    footer = f'''
# Persist any new variables for next call
import json as _json
_new_vars = {{k: v for k, v in locals().items()
              if not k.startswith("_") and k not in {list(initial_vars.keys())!r}
              and k not in ("registry", "profile_name", "role", "config",
                            "JobRegistry", "JobRecord", "scan_history",
                            "PriorContact", "WikiEntry", "WikiStore",
                            "sys", "os", "json")}}
try:
    with open({vars_file!r}, "w") as _f:
        _f.write(_json.dumps(_new_vars, default=str))
except Exception as _e:
    print(f"[sandbox: could not persist vars: {{_e}}]", file=sys.stderr)
'''
    return header + "\n" + user_code + "\n" + footer


def _read_persistent_vars(vars_file: Path) -> Dict[str, Any]:
    """Read persistent variables from the previous call's vars file."""
    if not vars_file.exists():
        return {}
    try:
        return json.loads(vars_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def run_python_tool_factory(profile_name: str, role: str) -> AgentTool:
    """Tool: execute Python code in a sandboxed subprocess.

    Exposes:
    - `registry` (JobRegistry)
    - `profile_name`, `role` (strings)
    - `scan_history(org_name)` — history lookup function
    - All of `config` module
    - Persistent variables across calls (read from / write to a temp file)
    """
    lovework_agent_path = str(config.AGENT_ROOT)
    vars_file = config.CACHE_DIR / "_sandbox_vars.json"

    async def execute(tool_call_id: str, params: Dict[str, Any], abort_event=None, on_update=None) -> AgentToolResult:
        code = params.get("code", "")
        if not code.strip():
            return AgentToolResult(
                content=[TextContent(text="Error: code is required.")],
                details={},
            )

        # Read persistent vars from previous calls
        initial_vars = _read_persistent_vars(vars_file)

        # Build the full script
        script = _make_sandbox_script(
            user_code=code,
            profile_name=profile_name,
            role=role,
            vars_file=str(vars_file),
            initial_vars=initial_vars,
            lovework_agent_path=lovework_agent_path,
        )

        # Run in a subprocess
        try:
            # Add the venv's site-packages to PYTHONPATH so we can import our deps
            import sys as _sys
            venv_site = str(Path(_sys.executable).parent.parent / "lib" / f"python{_sys.version_info.major}.{_sys.version_info.minor}" / "site-packages")
            env = os.environ.copy()
            env["PYTHONPATH"] = venv_site + os.pathsep + env.get("PYTHONPATH", "")
            env["DEEPSEEK_API_KEY"] = config.LLM_API_KEY or ""
            env["FIRECRAWL_API_KEY"] = config.FIRECRAWL_API_KEY or ""

            proc = subprocess.run(
                [sys.executable, "-I", "-c", script],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
                cwd=str(config.AGENT_ROOT),
            )
            stdout = proc.stdout[-10000:]  # truncate
            stderr = proc.stderr[-3000:]
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            return AgentToolResult(
                content=[TextContent(text="Error: code execution timed out after 30s.")],
                details={"timeout": True},
            )
        except Exception as e:
            return AgentToolResult(
                content=[TextContent(text=f"Error: {e}")],
                details={},
            )

        result_text = f"```\n{stdout}\n```"
        if stderr.strip():
            result_text += f"\n\n**stderr:**\n```\n{stderr[-1500:]}\n```"
        if rc != 0:
            result_text += f"\n\n**exit code:** {rc}"

        return AgentToolResult(
            content=[TextContent(text=result_text)],
            details={"stdout": stdout, "stderr": stderr, "returncode": rc},
        )

    return AgentTool(
        name="run_python",
        label="Run Python code",
        description=(
            "Execute Python code in a sandboxed subprocess. The sandbox has "
            "access to: the JobRegistry (as `registry`), the history scanner, "
            "the wiki store, and the current profile_name/role. Variables you "
            "set at the top level persist across calls (via a JSON file in the "
            "cache dir). Use this for: batch operations, custom filters, "
            "exploratory analysis, anything that needs computation. "
            "Returns stdout (and stderr if any) plus the exit code. "
            "30-second timeout per call."
        ),
        execute=execute,
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute. Top-level variables persist across calls.",
                },
            },
            "required": ["code"],
        },
    )
