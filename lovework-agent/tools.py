"""
Tools — wrap our existing modules as pi-agent AgentTool instances.

Each tool is a thin wrapper that:
1. Has a clear name + description (for the LLM to know when to call it)
2. Has a JSON schema for its parameters (for the LLM to know what to pass)
3. Calls into our existing code (crawler, matcher, registry, history, wiki)

The agent's system prompt tells the LLM what tools are available.

Tool signature (per pi-agent 0.1.0):
    async def execute(tool_call_id, params, abort_event, on_update) -> AgentToolResult
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from pi_agent import AgentTool, AgentToolResult, TextContent

import config
from crawler import SmartCrawler
from history import scan_history
from job_registry import JobRecord, JobRegistry
from llm_client import LLMClient
from matcher import JobMatcher
from sandbox import run_python_tool_factory
from wiki_store import WikiEntry, WikiStore

logger = logging.getLogger(__name__)


def _result(text: str, details: Optional[dict] = None) -> AgentToolResult:
    """Helper to build an AgentToolResult from a string."""
    return AgentToolResult(
        content=[TextContent(text=text)],
        details=details or {},
    )


def _result_json(data: Any) -> AgentToolResult:
    """Helper to build an AgentToolResult from a JSON-serialisable object."""
    text = json.dumps(data, indent=2, default=str)
    return _result(text, details=data if isinstance(data, dict) else {"data": data})


# ── Tool: crawl_org ─────────────────────────────────────────────────────

def crawl_org_tool_factory(crawler: SmartCrawler) -> AgentTool:
    """Tool: crawl an organization's site for job listings."""
    async def execute(tool_call_id: str, params: Dict[str, Any], abort_event=None, on_update=None) -> AgentToolResult:
        org_name = params.get("org_name", "")
        seed_urls = params.get("seed_urls", [])
        goal = params.get("goal", "Find open technical positions suitable for an ML/AI researcher.")
        max_pages = int(params.get("max_pages", 4))

        if not org_name or not seed_urls:
            return _result("Error: org_name and seed_urls are required.")

        try:
            jobs = crawler.crawl_org(
                org_name=org_name,
                seed_urls=seed_urls,
                goal=goal,
                max_pages=max_pages,
            )
            data = {
                "org": org_name,
                "count": len(jobs),
                "jobs": [
                    {
                        "title": j.title,
                        "location": j.location,
                        "url": j.url,
                        "description_snippet": j.description_snippet,
                        "requirements_snippet": j.requirements_snippet,
                    }
                    for j in jobs
                ],
            }
            return _result_json(data)
        except Exception as e:
            return _result(f"Error crawling {org_name}: {e}")

    return AgentTool(
        name="crawl_org",
        label="Crawl organization",
        description=(
            "Crawl an organization's website to find open job listings. "
            "Use this when you have a specific company in mind and want to "
            "discover what roles they have open. The crawler navigates from "
            "the seed URLs, follows careers links, and extracts structured "
            "job listings. Returns a list of jobs with title, location, URL, "
            "and brief description."
        ),
        execute=execute,
        parameters={
            "type": "object",
            "properties": {
                "org_name": {"type": "string", "description": "Name of the organization (e.g. 'OpenAI', 'FAR.AI')"},
                "seed_urls": {"type": "array", "items": {"type": "string"}, "description": "URLs to start crawling from"},
                "goal": {"type": "string", "description": "What kinds of jobs to look for"},
                "max_pages": {"type": "integer", "description": "Maximum pages to crawl", "default": 4},
            },
            "required": ["org_name", "seed_urls"],
        },
    )


# ── Tool: match_profile ─────────────────────────────────────────────────

def match_profile_tool_factory(matcher: JobMatcher) -> AgentTool:
    """Tool: score a job against a principal's profile."""
    async def execute(tool_call_id: str, params: Dict[str, Any], abort_event=None, on_update=None) -> AgentToolResult:
        job_title = params.get("job_title", "")
        job_description = params.get("job_description", "")
        org_name = params.get("org_name", "")
        job_url = params.get("job_url", "")

        if not job_title or not org_name:
            return _result("Error: job_title and org_name are required.")

        try:
            result = matcher.match(
                job_title=job_title,
                job_description=job_description,
                org_name=org_name,
                job_url=job_url,
            )
            return _result_json({
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
            })
        except Exception as e:
            return _result(f"Error matching: {e}")

    return AgentTool(
        name="match_profile",
        label="Match against profile",
        description=(
            "Score a job listing against a principal's profile. "
            "Returns fit/reach/flourish axes, combined score, action, "
            "legacy decision (GO/MAYBE/FLAG/DROP), and reasoning. "
            "Considers the principal's soul, CV, role-specific criteria, "
            "job lifecycle status, and prior contact with the org."
        ),
        execute=execute,
        parameters={
            "type": "object",
            "properties": {
                "job_title": {"type": "string"},
                "job_description": {"type": "string", "description": "1-3 sentence description"},
                "org_name": {"type": "string"},
                "job_url": {"type": "string"},
            },
            "required": ["job_title", "org_name"],
        },
    )


# ── Tool: search_jobs ───────────────────────────────────────────────────

def search_jobs_tool_factory(registry: JobRegistry) -> AgentTool:
    """Tool: query the job registry."""
    async def execute(tool_call_id: str, params: Dict[str, Any], abort_event=None, on_update=None) -> AgentToolResult:
        status = params.get("status")
        org = params.get("org")
        limit = int(params.get("limit", 50))

        if org:
            jobs = registry.by_org(org)
        elif status:
            jobs = registry.all_jobs(status=status)
        else:
            jobs = registry.all_jobs()

        return _result_json({
            "count": len(jobs),
            "jobs": [
                {
                    "id": j.id,
                    "org": j.org,
                    "title": j.title,
                    "url": j.url,
                    "first_seen": j.first_seen,
                    "last_seen": j.last_seen,
                    "status": j.status,
                    "age_days": j.age_days,
                }
                for j in jobs[:limit]
            ],
        })

    return AgentTool(
        name="search_jobs",
        label="Search job registry",
        description=(
            "Query the persistent job registry. Returns all jobs we've ever "
            "seen, with their lifecycle status (new, still_open, disappeared, "
            "long_lasting). Optionally filter by status or org."
        ),
        execute=execute,
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["new", "still_open", "disappeared", "long_lasting"]},
                "org": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    )


# ── Tool: check_history ─────────────────────────────────────────────────

def check_history_tool_factory() -> AgentTool:
    """Tool: check prior contact with an organization."""
    async def execute(tool_call_id: str, params: Dict[str, Any], abort_event=None, on_update=None) -> AgentToolResult:
        org_name = params.get("org_name", "")
        use_gmail = bool(params.get("use_gmail", True))

        if not org_name:
            return _result("Error: org_name is required.")

        try:
            prior = scan_history(org_name, use_gmail=use_gmail)
            return _result_json({
                "org": org_name,
                "has_application": prior.has_application,
                "has_rejection": prior.has_rejection,
                "last_contact_date": prior.last_contact_date,
                "summary": prior.summary(),
                "applications": [
                    {
                        "date": a.date,
                        "role": a.role,
                        "has_rejection": a.has_rejection,
                        "rejection_date": a.rejection_date,
                    }
                    for a in prior.applications
                ],
                "gmail_events": [
                    {"date": e.date, "subject": e.subject, "kind": e.kind}
                    for e in prior.gmail_events
                ],
            })
        except Exception as e:
            return _result(f"Error checking history: {e}")

    return AgentTool(
        name="check_history",
        label="Check prior contact",
        description=(
            "Check if the principal has prior contact with an organization. "
            "Searches applications/ and Gmail. Use this before recommending "
            "an application — re-applying to the same role within 6 months "
            "of a rejection is a DROP."
        ),
        execute=execute,
        parameters={
            "type": "object",
            "properties": {
                "org_name": {"type": "string"},
                "use_gmail": {"type": "boolean", "default": True},
            },
            "required": ["org_name"],
        },
    )


# ── Tool: fetch_url ─────────────────────────────────────────────────────

def fetch_url_tool_factory() -> AgentTool:
    """Tool: fetch a URL and return its markdown content."""
    from crawler import fetch_page

    async def execute(tool_call_id: str, params: Dict[str, Any], abort_event=None, on_update=None) -> AgentToolResult:
        url = params.get("url", "")
        use_cache = bool(params.get("use_cache", True))

        if not url:
            return _result("Error: url is required.")

        try:
            md = fetch_page(url, use_cache=use_cache)
            return _result_json({
                "url": url,
                "chars": len(md),
                "content": md[:50000],  # truncate
            })
        except Exception as e:
            return _result(f"Error fetching {url}: {e}")

    return AgentTool(
        name="fetch_url",
        label="Fetch URL",
        description=(
            "Fetch a URL and return its content as clean markdown. "
            "Renders JavaScript, caches to disk, returns first 50K chars."
        ),
        execute=execute,
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "use_cache": {"type": "boolean", "default": True},
            },
            "required": ["url"],
        },
    )


# ── Tool: update_wiki ───────────────────────────────────────────────────

def update_wiki_tool_factory(wiki: WikiStore) -> AgentTool:
    """Tool: write a finding to the local markdown wiki."""
    async def execute(tool_call_id: str, params: Dict[str, Any], abort_event=None, on_update=None) -> AgentToolResult:
        org_name = params.get("org_name", "")
        title = params.get("title", "")

        if not org_name or not title:
            return _result("Error: org_name and title are required.")

        try:
            entry = WikiEntry(
                org_name=org_name,
                title=title,
                url=params.get("url", ""),
                location=params.get("location", ""),
                score=float(params.get("score", 0.0)),
                decision=params.get("decision", "FLAG"),
                reasoning=params.get("reasoning", ""),
                source=params.get("source", "agent"),
                fit_score=params.get("fit_score"),
                reach_score=params.get("reach_score"),
                flourish_score=params.get("flourish_score"),
                combined_score=params.get("combined_score"),
                recommended_action=params.get("recommended_action"),
            )
            wiki.update_org_page(entry)
            return _result_json({
                "status": "ok",
                "written_to": f"wiki/orgs/{org_name}.md",
            })
        except Exception as e:
            return _result(f"Error updating wiki: {e}")

    return AgentTool(
        name="update_wiki",
        label="Update wiki",
        description=(
            "Write a finding to the local markdown wiki. Creates or appends to "
            "the org's history page. Use this to record discoveries so the user "
            "can review them later."
        ),
        execute=execute,
        parameters={
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
    )


# ── Tool: registry_stats ────────────────────────────────────────────────

def registry_stats_tool_factory(registry: JobRegistry) -> AgentTool:
    """Tool: get summary stats from the job registry."""
    async def execute(tool_call_id: str, params: Dict[str, Any], abort_event=None, on_update=None) -> AgentToolResult:
        return _result_json({"stats": registry.stats()})

    return AgentTool(
        name="registry_stats",
        label="Registry stats",
        description="Get a summary of how many jobs we've seen by status.",
        execute=execute,
        parameters={"type": "object", "properties": {}},
    )


# ── Factory: build all tools ────────────────────────────────────────────

def build_tools(
    registry: JobRegistry,
    matcher: JobMatcher,
    crawler: SmartCrawler,
    wiki: WikiStore,
    profile_name: str = "",
    role: str = "",
) -> List[AgentTool]:
    """Construct the full set of tools the agent can call."""
    tools = [
        crawl_org_tool_factory(crawler),
        match_profile_tool_factory(matcher),
        search_jobs_tool_factory(registry),
        check_history_tool_factory(),
        fetch_url_tool_factory(),
        update_wiki_tool_factory(wiki),
        registry_stats_tool_factory(registry),
    ]
    if profile_name and role:
        tools.append(run_python_tool_factory(profile_name, role))
    return tools
