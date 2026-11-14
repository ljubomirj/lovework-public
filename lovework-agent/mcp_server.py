"""
LoveWork MCP server — JSON-RPC 2.0 over HTTP, served at POST /mcp by
dashboard_server.py.

Exposes the 8 LoveWork agent tools plus a 9th (`run_pipeline`) so any
MCP-speaking agent (Hermes, Claude Code, Codex, OpenCode) can drive LoveWork
in-process. See DECISIONS.md D18 for the rationale (one process, two faces:
dashboard HTML + MCP tool endpoint).

Wire format: JSON-RPC 2.0. Three methods implemented:
  - initialize      → server capabilities + protocol version
  - tools/list      → the 9 tool schemas
  - tools/call      → dispatch by name

No `mcp` / `fastmcp` SDK dependency — the JSON-RPC framing is hand-rolled
(keeps the server launchd-safe and avoids a churning SDK in the venv). If MCP
grows features we'll reconsider, but initialize/list/call is the stable core
every client speaks today.

The tool *bodies* reuse the existing modules: read-only tools share the
dashboard's `fetch_*` functions (live state, no duplicate reader), and the
crawl/match/wiki tools reuse the same factories `tools.py` exposes to the
pi-agent. One source of truth for tool behaviour.
"""
from __future__ import annotations

import json
import os
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Protocol constants ───────────────────────────────────────────────────
PROTOCOL_VERSION = "2025-06-18"  # MCP protocol version this server speaks
SERVER_NAME = "lovework"
SERVER_VERSION = "1.0.0"

# JSON-RPC error codes (per spec)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


from tool_service import TOOL_SCHEMAS, TOOL_NAMES, ToolError as _RpcError, execute_tool


def _dispatch_tool(name: str, params: Dict[str, Any]) -> dict:
    return execute_tool(name, params)


# ── JSON-RPC method handlers ─────────────────────────────────────────────

def _mcp_initialize(params: dict) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "capabilities": {"tools": {}},
    }


def _mcp_tools_list(params: dict) -> dict:
    return {"tools": TOOL_SCHEMAS}


def _mcp_tools_call(params: dict) -> dict:
    """params: {name, arguments}."""
    name = params.get("name")
    if name not in TOOL_NAMES:
        raise _RpcError(METHOD_NOT_FOUND, f"Unknown tool: {name}")
    args = params.get("arguments", {}) or {}
    try:
        result = _dispatch_tool(name, args)
    except _RpcError:
        raise  # propagate as JSON-RPC error
    # MCP tools/call wraps the result in a {content: [...]} envelope.
    return {
        "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}],
        "isError": False,
    }


_METHODS: Dict[str, Callable[[dict], dict]] = {
    "initialize": _mcp_initialize,
    "tools/list": _mcp_tools_list,
    "tools/call": _mcp_tools_call,
}


# ── Top-level JSON-RPC dispatcher ────────────────────────────────────────

def handle_request(body: bytes) -> Optional[bytes]:
    """Parse a JSON-RPC request body and return the JSON response body.

    Returns None for notifications (no `id`), per spec — caller should not
    write a body in that case.
    """
    try:
        req = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return json.dumps({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": PARSE_ERROR, "message": f"Parse error: {e}"},
        }).encode("utf-8")

    if not isinstance(req, dict):
        return _error_response(None, INVALID_REQUEST, "Request must be a JSON object")

    req_id = req.get("id")  # may be None (notification) — keep separate from method
    method = req.get("method")
    params = req.get("params", {}) or {}

    if not method:
        return _error_response(req_id, INVALID_REQUEST, "Missing 'method'")

    handler = _METHODS.get(method)
    if handler is None:
        if req_id is None:
            return None  # notification for unknown method: silent per spec
        return _error_response(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    # Notification (no id): execute but return no response.
    if req_id is None:
        try:
            handler(params)
        except Exception as e:
            logger.warning("notification %s failed: %s", method, e)
        return None

    try:
        result = handler(params)
    except _RpcError as e:
        return _error_response(req_id, e.code, e.message, e.data)
    except Exception as e:
        logger.exception("tool/internal error in %s", method)
        return _error_response(req_id, INTERNAL_ERROR, f"Internal error: {e}")

    return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}).encode("utf-8")


def _error_response(req_id: Any, code: int, message: str, data: Any = None) -> bytes:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "error": err}).encode("utf-8")
