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


# ── Tool schemas ─────────────────────────────────────────────────────────
# Each tool: (name, description, input_schema). Mirrors tools.py AgentTool
# parameters plus run_pipeline. Schemas live here (not imported from tools.py)
# so the MCP surface is independent of pi-agent being importable.

TOOL_SCHEMAS: List[dict] = [
    {
        "name": "crawl_org",
        "description": (
            "Crawl an organization's website to find open job listings. "
            "Use when you have a specific company in mind and want to discover "
            "what roles they have open. Returns jobs with title, location, URL, "
            "and a brief description."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "org_name": {"type": "string", "description": "Organization name (e.g. 'OpenAI', 'FAR.AI')"},
                "seed_urls": {"type": "array", "items": {"type": "string"}, "description": "URLs to start crawling from"},
                "goal": {"type": "string", "description": "What kinds of jobs to look for"},
                "max_pages": {"type": "integer", "description": "Maximum pages to crawl", "default": 4},
            },
            "required": ["org_name", "seed_urls"],
        },
    },
    {
        "name": "match_profile",
        "description": (
            "Score a job listing against a principal's profile. Returns "
            "fit/reach/flourish axes, combined score, action, legacy decision "
            "(GO/MAYBE/FLAG/DROP), and reasoning. Considers "
            "soul, CV (Layer 2), branching possibilities (Layer 3), role "
            "criteria, job lifecycle, and prior contact with the org."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile_name": {"type": "string", "description": "Principal profile: lj, kj, vj, or pk", "default": "lj"},
                "role": {"type": "string", "description": "Role file under profiles/<name>/roles/", "default": "general"},
                "job_title": {"type": "string"},
                "job_description": {"type": "string", "description": "1-3 sentence description"},
                "org_name": {"type": "string"},
                "job_url": {"type": "string"},
                "location": {"type": "string", "description": "Job location text (drives work-auth hard-kill)"},
            },
            "required": ["job_title", "org_name"],
        },
    },
    {
        "name": "search_jobs",
        "description": (
            "Query the persistent job registry. Returns all jobs ever seen, "
            "with lifecycle status (new, still_open, disappeared, long_lasting). "
            "Optionally filter by status or org."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["new", "still_open", "disappeared", "long_lasting"]},
                "org": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "check_history",
        "description": (
            "Check if the principal has prior contact with an organization. "
            "Searches applications/ and Gmail. Use before recommending an "
            "application — re-applying to the same role within 6 months of a "
            "rejection is a DROP."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "org_name": {"type": "string"},
                "use_gmail": {"type": "boolean", "default": True},
            },
            "required": ["org_name"],
        },
    },
    {
        "name": "fetch_url",
        "description": "Fetch a URL and return its content as clean markdown. Renders JS, caches to disk, returns first 50K chars.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "use_cache": {"type": "boolean", "default": True},
            },
            "required": ["url"],
        },
    },
    {
        "name": "update_wiki",
        "description": (
            "Write a finding to the local markdown wiki. Creates or appends to "
            "the org's history page. Use to record discoveries for later review."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "org_name": {"type": "string"},
                "title": {"type": "string"},
                "url": {"type": "string"},
                "location": {"type": "string"},
                "score": {"type": "number"},
                "decision": {"type": "string", "enum": ["GO", "MAYBE", "FLAG", "DROP"]},
                "reasoning": {"type": "string"},
                "source": {"type": "string"},
            },
            "required": ["org_name", "title"],
        },
    },
    {
        "name": "registry_stats",
        "description": "Get a summary of how many jobs we've seen by lifecycle status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "run_python",
        "description": (
            "Run Python code in a sandboxed REPL with the LoveWork modules "
            "available (config, matcher, registry, wiki). Use for ad-hoc analysis "
            "or one-off queries that don't fit a named tool."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
                "profile_name": {"type": "string", "default": "lj"},
                "role": {"type": "string", "default": "general"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "run_pipeline",
        "description": (
            "Trigger a LoveWork discovery pipeline run for a principal. Crawls "
            "the selected source, scores every job against the profile, updates "
            "the registry + wiki, and writes a dated report. LONG-RUNNING: an "
            "incremental crawl is 3-15 min, a full run is 20-30 min. The call "
            "blocks until completion; ensure the client timeout is high "
            "(>=600s for a full run). Returns the report path + entry count."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile_name": {"type": "string", "description": "Principal: lj, kj, vj, pk", "default": "lj"},
                "role": {"type": "string", "description": "Role file; default 'general'", "default": "general"},
                "source": {"type": "string", "enum": ["all", "research_orgs", "neolabs", "hf_startups", "hn_hiring", "hn_jobs", "gmail_lj_jobs", "linkedin_related", "company_pages", "harnham"], "default": "all"},
                "use_dspy": {"type": "boolean", "default": False},
                "dry_run": {"type": "boolean", "default": False},
                "write_report": {"type": "boolean", "default": True},
            },
        },
    },
]

TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS}


# ── Tool dispatch ────────────────────────────────────────────────────────
# Handlers are imported lazily inside _dispatch_tool so that merely listing
# tools (tools/list) doesn't require every heavy dependency to import cleanly.
# Each handler returns a JSON-serialisable result dict.

def _require(params: dict, *names: str) -> None:
    """Raise InvalidParams if any required name is missing."""
    missing = [n for n in names if n not in params or params[n] in (None, "")]
    if missing:
        raise _RpcError(INVALID_PARAMS, f"Missing required parameter(s): {', '.join(missing)}")


class _RpcError(Exception):
    """JSON-RPC error raised inside a tool handler; caught by the dispatcher."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _dispatch_tool(name: str, params: Dict[str, Any]) -> dict:
    """Execute one tool by name. Returns a result dict (JSON-serialisable).

    Raises _RpcError on invalid params; other exceptions bubble up as
    internal-error JSON-RPC responses.
    """
    params = params or {}

    # ── read-only tools: reuse dashboard fetchers where they already exist ──
    if name == "registry_stats":
        # Imported here to avoid a hard circular dep at module load time
        # (dashboard_server imports mcp_server).
        from dashboard_server import fetch_registry_stats
        return fetch_registry_stats()

    if name == "search_jobs":
        from job_registry import JobRegistry
        _require  # noqa: F841 (no required params, but keep the helper visible)
        reg = JobRegistry()
        status = params.get("status")
        org = params.get("org")
        limit = int(params.get("limit", 50))
        if org:
            jobs = reg.by_org(org)
        elif status:
            jobs = reg.all_jobs(status=status)
        else:
            jobs = reg.all_jobs()
        return {
            "count": len(jobs),
            "jobs": [
                {
                    "id": j.id, "org": j.org, "title": j.title, "url": j.url,
                    "first_seen": j.first_seen, "last_seen": j.last_seen,
                    "status": j.status, "age_days": j.age_days,
                }
                for j in jobs[:limit]
            ],
        }

    if name == "check_history":
        from history import scan_history
        _require(params, "org_name")
        prior = scan_history(params["org_name"], use_gmail=bool(params.get("use_gmail", True)))
        return {
            "org": params["org_name"],
            "has_application": prior.has_application,
            "has_rejection": prior.has_rejection,
            "last_contact_date": prior.last_contact_date,
            "summary": prior.summary(),
            "applications": [
                {"date": a.date, "role": a.role, "has_rejection": a.has_rejection,
                 "rejection_date": a.rejection_date}
                for a in prior.applications
            ],
            "gmail_events": [
                {"date": e.date, "subject": e.subject, "kind": e.kind}
                for e in prior.gmail_events
            ],
        }

    if name == "fetch_url":
        from crawler import fetch_page
        _require(params, "url")
        md = fetch_page(params["url"], use_cache=bool(params.get("use_cache", True)))
        return {"url": params["url"], "chars": len(md), "content": md[:50000]}

    # ── crawl/match/wiki: build collaborators lazily, reuse the core modules ──
    if name == "crawl_org":
        from crawler import SmartCrawler
        from llm_client import LLMClient
        _require(params, "org_name", "seed_urls")
        crawler = SmartCrawler(LLMClient())
        jobs = crawler.crawl_org(
            org_name=params["org_name"],
            seed_urls=params["seed_urls"],
            goal=params.get("goal", "Find open technical positions suitable for an ML/AI researcher."),
            max_pages=int(params.get("max_pages", 4)),
        )
        return {
            "org": params["org_name"], "count": len(jobs),
            "jobs": [
                {"title": j.title, "location": j.location, "url": j.url,
                 "description_snippet": j.description_snippet,
                 "requirements_snippet": j.requirements_snippet}
                for j in jobs
            ],
        }

    if name == "match_profile":
        import config as _cfg
        from job_registry import JobRegistry
        from llm_client import LLMClient
        from matcher import JobMatcher
        _require(params, "job_title", "org_name")
        profile_name = params.get("profile_name", "lj")
        role = params.get("role", "general")
        profile_text = _cfg.load_profile_text(profile_name, role=role)
        registry = JobRegistry()
        matcher = JobMatcher(LLMClient(), profile_text, registry=registry, use_history=True)
        result = matcher.match(
            job_title=params["job_title"],
            job_description=params.get("job_description", ""),
            org_name=params["org_name"],
            job_url=params.get("job_url", ""),
            location=params.get("location", ""),
        )
        return {
            "score": result.score,
            "decision": result.decision,
            "fit_score": result.fit_score,
            "reach_score": result.reach_score,
            "flourish_score": result.flourish_score,
            "combined_score": result.combined_score,
            "recommended_action": result.recommended_action,
            "prestige_trap_risk": result.prestige_trap_risk,
            "screening_story": result.screening_story,
            "likely_day_to_day": result.likely_day_to_day,
            "reasoning": result.reasoning,
        }

    if name == "update_wiki":
        from wiki_store import WikiEntry, WikiStore
        _require(params, "org_name", "title")
        wiki = WikiStore()
        entry = WikiEntry(
            org_name=params["org_name"], title=params["title"],
            url=params.get("url", ""), location=params.get("location", ""),
            score=float(params.get("score", 0.0)), decision=params.get("decision", "FLAG"),
            reasoning=params.get("reasoning", ""), source=params.get("source", "mcp"),
            fit_score=params.get("fit_score"),
            reach_score=params.get("reach_score"),
            flourish_score=params.get("flourish_score"),
            combined_score=params.get("combined_score"),
            recommended_action=params.get("recommended_action"),
        )
        wiki.update_org_page(entry)
        return {"status": "ok", "written_to": f"wiki/orgs/{params['org_name']}.md"}

    if name == "run_python":
        import asyncio as _aio
        from sandbox import run_python_tool_factory
        _require(params, "code")
        profile_name = params.get("profile_name", "lj")
        role = params.get("role", "general")
        tool = run_python_tool_factory(profile_name, role)
        # The factory's execute() is async but the body is fully synchronous
        # (subprocess.run is blocking). Run it to completion with a fresh loop.
        result = _aio.run(tool.execute("mcp", {"code": params["code"]}))
        # AgentToolResult.content is a list of TextContent; flatten to text.
        text = "\n".join(getattr(c, "text", str(c)) for c in result.content)
        return {"output": text, "details": result.details}

    if name == "run_pipeline":
        import pipeline as _pipe
        _require  # noqa: F841 — no required params (all defaulted)
        entries, disappeared = _pipe.run_pipeline(
            profile_name=params.get("profile_name", "lj"),
            role=params.get("role", "general"),
            source=params.get("source", "all"),
            use_dspy=bool(params.get("use_dspy", False)),
            dry_run=bool(params.get("dry_run", False)),
            write_report=bool(params.get("write_report", True)),
        )
        # Find the report path written by wiki.save_report (best-effort).
        from config import WIKI_ROOT
        reports_dir = WIKI_ROOT / "reports"
        report_path = None
        if reports_dir.is_dir():
            report_files = sorted(reports_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            if report_files:
                report_path = str(report_files[0])
        return {
            "entries": len(entries),
            "disappeared": disappeared,
            "report_path": report_path,
            "gos": sum(1 for e in entries if e.decision == "GO"),
            "maybes": sum(1 for e in entries if e.decision == "MAYBE"),
        }

    raise _RpcError(METHOD_NOT_FOUND, f"Unknown tool: {name}")


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
