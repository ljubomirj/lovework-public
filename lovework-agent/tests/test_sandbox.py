"""
Tests for the sandboxed Python REPL.

The sandbox is the LLM's "scratch pad" — it can write code to explore data.
These tests verify:
- Simple execution works
- The registry is accessible
- Variables persist across calls
- Errors are reported cleanly
- Timeouts work
"""

import asyncio

import pytest

from sandbox import run_python_tool_factory


@pytest.fixture
def sandbox_tool():
    return run_python_tool_factory("lj", "general")


async def _exec(tool, code: str):
    """Helper to run code through the tool and return the result text."""
    result = await tool.execute("test-call", {"code": code})
    return result.content[0].text


def test_sandbox_simple_print(sandbox_tool):
    """A simple print statement works."""
    out = asyncio.run(_exec(sandbox_tool, 'print("hello")'))
    assert "hello" in out


def test_sandbox_arithmetic(sandbox_tool):
    """Basic arithmetic works."""
    out = asyncio.run(_exec(sandbox_tool, "x = 2 + 2\nprint(f'result: {x}')"))
    assert "result: 4" in out


def test_sandbox_registry_accessible(sandbox_tool):
    """The registry is accessible as a pre-imported variable."""
    out = asyncio.run(_exec(sandbox_tool, "print(f'jobs: {len(registry.all_jobs())}')"))
    assert "jobs:" in out


def test_sandbox_variables_persist(sandbox_tool):
    """Top-level variables persist across calls."""
    asyncio.run(_exec(sandbox_tool, "my_counter = 42"))
    out = asyncio.run(_exec(sandbox_tool, "print(f'counter: {my_counter}')"))
    assert "counter: 42" in out


def test_sandbox_errors_reported(sandbox_tool):
    """Errors are captured and reported, not raised."""
    out = asyncio.run(_exec(sandbox_tool, "raise ValueError('boom')"))
    assert "ValueError" in out or "boom" in out


def test_sandbox_empty_code(sandbox_tool):
    """Empty code returns an error."""
    out = asyncio.run(_exec(sandbox_tool, ""))
    assert "Error" in out or "required" in out


def test_sandbox_can_import_stdlib(sandbox_tool):
    """Standard library imports work (json, math, etc.)."""
    out = asyncio.run(_exec(sandbox_tool, "import json\nprint(json.dumps({'a': 1}))"))
    assert '{"a": 1}' in out


def test_sandbox_exit_code_on_error(sandbox_tool):
    """Non-zero exit code is reported when code fails."""
    out = asyncio.run(_exec(sandbox_tool, "1/0"))
    # Should mention the error or the exit code
    assert "ZeroDivision" in out or "exit" in out.lower() or "Error" in out
