"""Shared, principal-scoped local tool service for CLI and MCP.

Schemas import without optional agent/model dependencies; bodies load lazily.
"""
from __future__ import annotations

import json
import os
import re
from contextlib import nullcontext
from typing import Any, Dict, List

INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601

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
                "status": {"type": "string", "enum": ["new", "still_open", "disappeared", "long_lasting",
                     "not_listed_in_scope"]},
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
        "name": "asof_explain",
        "description": (
            "Explain one recorded assessment: the decision, its evidence hashes, "
            "the policy and profile it was made under, and when each fact was "
            "recorded (with timezone/precision stated). Principal-scoped: a "
            "binding serves the principal it was started for unless others are "
            "explicitly allowed. Returns a bounded result with citations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "principal": {"type": "string",
                              "description": "defaults to this binding's principal"},
                "assessment_id": {"type": "string"},
                "run_id": {"type": "string"},
                "org": {"type": "string"},
                "title": {"type": "string"},
            },
        },
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
            "incremental crawl is 45-90 min, a full run is 1.8-8.8 h. The call "
            "blocks until completion; ensure the client timeout covers the whole "
            "run (prefer the CLI/cron path for full sweeps). Returns the report "
            "path + entry count."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile_name": {"type": "string", "description": "Principal: lj, kj, vj, pk", "default": "lj"},
                "role": {"type": "string", "description": "Role file; default 'general'", "default": "general"},
                "source": {"type": "string", "enum": ["all", "research_orgs", "neolabs", "neoclouds", "speech_companies", "hf_startups", "hn_hiring", "hn_jobs", "gmail_lj_jobs", "linkedin_related", "company_pages", "harnham", "digitaljobs", "jackandjill"], "default": "all"},
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
        raise ToolError(INVALID_PARAMS, f"Missing required parameter(s): {', '.join(missing)}")


class ToolError(Exception):
    """JSON-RPC error raised inside a tool handler; caught by the dispatcher."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _validate(name: str, params: dict) -> None:
    if name not in TOOL_NAMES:
        raise ToolError(METHOD_NOT_FOUND, f"Unknown tool: {name}")
    if not isinstance(params, dict):
        raise ToolError(INVALID_PARAMS, "Tool arguments must be an object")
    schema = next(t["inputSchema"] for t in TOOL_SCHEMAS if t["name"] == name)
    _require(params, *schema.get("required", []))
    types = {"string": str, "integer": int, "number": (int, float),
             "boolean": bool, "array": list}
    properties = schema["properties"]
    for key, value in params.items():
        spec = properties.get(key)
        if spec is None:
            if key in ("principal", "profile_name") and isinstance(value, str):
                continue
            raise ToolError(INVALID_PARAMS, f"Unknown parameter: {key}")
        kind = spec.get("type")
        if kind in types and (not isinstance(value, types[kind]) or
                              kind in ("integer", "number") and isinstance(value, bool)):
            raise ToolError(INVALID_PARAMS, f"{key} must be {kind}")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolError(INVALID_PARAMS, f"Invalid {key}: {value!r}")
        if kind == "array" and (not value or not all(isinstance(v, str) and v.strip() for v in value)):
            raise ToolError(INVALID_PARAMS, f"{key} must contain nonempty strings")
    for key in ("limit", "max_pages"):
        if key in params and params[key] < (1 if key == "max_pages" else 0):
            raise ToolError(INVALID_PARAMS, f"{key} is out of range")
    if "role" in params and not re.fullmatch(r"[a-zA-Z0-9_-]+", params["role"]):
        raise ToolError(INVALID_PARAMS, "role must be a simple role name")


def execute_tool(name: str, params: dict | None = None, *, principal: str | None = None) -> dict:
    """Execute locally; without an explicit principal, enforce MCP binding policy.

    Mutations take the existing principal writer lease. Pure reads neither
    acquire it nor initialize state. Optional model imports happen after input
    validation and authorization.
    """
    params = {} if params is None else params
    _validate(name, params)
    from principal_runtime import (PrincipalRefused, authorize_principal,
                                   resolve_principal_runtime)
    requested = [v.lower().strip() for v in
                 (principal, params.get("principal"), params.get("profile_name")) if v is not None]
    if len(set(requested)) > 1:
        raise ToolError(INVALID_PARAMS, "Conflicting principal/profile_name arguments")
    caller = os.getenv("LOVEWORK_BINDING_PRINCIPAL", "lj").lower().strip()
    selected = requested[0] if requested else caller
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", selected):
        raise ToolError(INVALID_PARAMS, "principal must be a simple principal name")
    if principal is None:
        allowed = tuple(p.strip().lower() for p in
                        os.getenv("LOVEWORK_ALLOWED_PRINCIPALS", "").split(",") if p.strip())
        try:
            selected = authorize_principal(selected, caller=caller, allowed=allowed)
        except PrincipalRefused as exc:
            return {"error": "principal_refused", "detail": str(exc)}
    runtime = resolve_principal_runtime(selected)
    import config as _config
    _config.set_active_profile(selected)
    from writer_boundary import WriterBusy, standalone_writer_guard
    mutates = name not in ("registry_stats", "search_jobs", "asof_explain")
    if name == "run_pipeline" and params.get("dry_run", False):
        mutates = False
    guard = (standalone_writer_guard(selected, mode="direct", state_root=runtime.state_root)
             if mutates else nullcontext())
    try:
        with guard:
            if mutates:
                from model_transcripts import active_dataset_dir
                with active_dataset_dir(runtime.dataset_dir):
                    return _execute(name, params, runtime)
            return _execute(name, params, runtime)
    except WriterBusy as busy:
        return {"error": "writer_busy", "detail": f"another operation holds {busy.resource.key}",
                "attempt": str(busy.attempt_path), "exit_code": 75}


def _history_kwargs(runtime) -> dict:
    kwargs = {"applications_dir": runtime.applications_dir, "use_gmail": False}
    if runtime.gmail_mailbox is not None:
        kwargs.update(use_gmail=True, gmail_label=runtime.gmail_mailbox.label,
                      gmail_credential_home=runtime.gmail_mailbox.credential_home)
    return kwargs


def _registry_stats(runtime) -> dict:
    """Read CSV or legacy SQLite without constructing a writing registry."""
    import csv
    import sqlite3
    csv_path, db_path = runtime.cache_dir / "jobs.csv", runtime.cache_dir / "jobs.db"
    path = csv_path if csv_path.exists() else db_path
    if not path.exists():
        return {"available": False, "path": str(path)}
    source = "csv" if path == csv_path else "sqlite"
    try:
        if source == "csv":
            counts = {}
            with path.open(encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    status = row.get("status", "unknown")
                    counts[status] = counts.get(status, 0) + 1
        else:
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            try:
                counts = dict(connection.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status"))
            finally:
                connection.close()
        return {"source": source, "path": str(path), "available": True,
                "size_mb": round(path.stat().st_size / 1024 / 1024, 1),
                "total": sum(counts.values()), "stats": counts}
    except (OSError, sqlite3.Error, csv.Error) as exc:
        return {"available": False, "source": source, "error": str(exc), "path": str(path)}


def _execute(name: str, params: Dict[str, Any], runtime) -> dict:
    """Execute one tool by name. Returns a result dict (JSON-serialisable).

    Raises ToolError on invalid params; other exceptions bubble up as
    internal-error JSON-RPC responses.
    """
    params = params or {}

    # ── read-only tools: reuse dashboard fetchers where they already exist ──
    if name == "registry_stats":
        return _registry_stats(runtime)

    if name == "asof_explain":
        from asof import Asof
        from strata import StrataEngine
        principal = runtime.profile_name
        result = Asof(StrataEngine(runtime.state_root)).explain(
            assessment_id=params.get("assessment_id"),
            run_id=params.get("run_id"), org=params.get("org"),
            title=params.get("title"))
        if result.get("status") != "ok":
            return {"status": result.get("status"),
                    "detail": result.get("detail"),
                    "citations": [{"principal": principal,
                                   "ledger": result.get("ledger")}]}
        # Bounded by construction (one assessment) and cited: a caller can name
        # exactly which artifact answered.
        return {
            "status": "ok",
            "principal": principal,
            "assessment_id": result["assessment_id"],
            "run_id": result["run_id"],
            "org_name": result["org_name"],
            "title": result["title"],
            "decision": result["decision"],
            "time_basis": result["time_basis"],
            "policy": result["policy"],
            "caveats": result["caveats"],
            "citations": [
                {"kind": "ledger", "path": result["location"]["ledger"]},
                {"kind": "seal", "run_id": result["run_id"],
                 "bundle": result["location"]["sealed_bundle"]},
            ],
        }

    if name == "search_jobs":
        from job_registry import JobRegistry
        _require  # noqa: F841 (no required params, but keep the helper visible)
        path = runtime.cache_dir / "jobs.csv"
        if not path.exists():
            return {"count": 0, "jobs": []}
        reg = JobRegistry(csv_path=path)
        status = params.get("status")
        org = params.get("org")
        limit = int(params.get("limit", 50))
        if org:
            jobs = reg.by_org(org)
            if status:
                jobs = [job for job in jobs if job.status == status]
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
        history_kwargs = _history_kwargs(runtime)
        history_kwargs["use_gmail"] = history_kwargs["use_gmail"] and params.get("use_gmail", True)
        prior = scan_history(params["org_name"], **history_kwargs)
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
        md, provider = fetch_page(params["url"], use_cache=params.get("use_cache", True), cache_dir=runtime.cache_dir)
        return {"url": params["url"], "chars": len(md), "provider": provider, "content": md[:50000]}

    # ── crawl/match/wiki: build collaborators lazily, reuse the core modules ──
    if name == "crawl_org":
        from crawler import SmartCrawler
        from llm_client import LLMClient
        _require(params, "org_name", "seed_urls")
        crawler = SmartCrawler(LLMClient(), cache_dir=runtime.cache_dir)
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
        profile_name = runtime.profile_name
        role = params.get("role", "general")
        profile_text = _cfg.load_profile_text(profile_name, role=role)
        registry = JobRegistry(csv_path=runtime.cache_dir / "jobs.csv")
        matcher = JobMatcher(LLMClient(), profile_text, registry=registry, use_history=True,
                             history_kwargs=_history_kwargs(runtime))
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
        wiki = WikiStore(root=runtime.wiki_root)
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
        return {"status": "ok", "written_to": str(wiki.orgs_dir / (wiki._org_stem(params["org_name"]) + ".md"))}

    if name == "run_python":
        import asyncio as _aio
        from sandbox import run_python_tool_factory
        _require(params, "code")
        profile_name = runtime.profile_name
        role = params.get("role", "general")
        tool = run_python_tool_factory(profile_name, role, runtime=runtime)
        # The factory's execute() is async but the body is fully synchronous
        # (subprocess.run is blocking). Run it to completion with a fresh loop.
        result = _aio.run(tool.execute("mcp", {"code": params["code"]}))
        # AgentToolResult.content is a list of TextContent; flatten to text.
        text = "\n".join(getattr(c, "text", str(c)) for c in result.content)
        return {"output": text, "details": result.details}

    if name == "run_pipeline":
        import pipeline as _pipe
        from config import STATE_DIR
        from writer_boundary import WriterBusy, standalone_writer_guard

        _require  # noqa: F841 — no required params (all defaulted)
        profile = params.get("profile_name", "lj")
        dry = bool(params.get("dry_run", False))
        # WP1b box ①: this tool runs in-process, so it must take the principal's
        # writer lease or refuse — a blocked crawl must never mutate state that
        # another operation is sealing. A dry run writes nothing and is exempt.
        if dry:
            from contextlib import nullcontext
            guard = nullcontext()
        else:
            guard = standalone_writer_guard(
                profile, mode="direct", state_root=STATE_DIR / profile)
        try:
            with guard:
                entries, disappeared = _pipe.run_pipeline(
                    profile_name=profile,
                    role=params.get("role", "general"),
                    source=params.get("source", "all"),
                    use_dspy=bool(params.get("use_dspy", False)),
                    dry_run=dry,
                    write_report=bool(params.get("write_report", True)),
                )
        except WriterBusy as busy:
            return {"error": "writer_busy",
                    "detail": f"another operation holds {busy.resource.key}",
                    "attempt": str(busy.attempt_path),
                    "exit_code": 75}
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

    raise ToolError(METHOD_NOT_FOUND, f"Unknown tool: {name}")

