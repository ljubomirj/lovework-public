"""
LoveWorkAgent — the personal job discovery agent.

Wraps the pipeline (crawler, matcher, registry, history, wiki) as pi-agent tools
and exposes an interactive agent loop on top.

Two modes:
- Autonomous (today's cron): run full pipeline, write report
- Interactive (new): user asks questions, agent calls tools to answer them

Example:
    agent = LoveWorkAgent.from_profile("lj", role="general")
    answer = agent.run("Find me UK-based AI research jobs posted this week, skip ones I've already applied to")
"""

import logging
from typing import List, Optional

import config
from crawler import SmartCrawler
from job_registry import JobRegistry
from llm_client import LLMClient
from llm_runtime import (
    AgentContext,
    AgentLoopConfig,
    AgentMessage,
    UserMessage,
    build_model,
    default_convert_to_llm,
    get_api_key,
    run_agent_loop,
)
from matcher import JobMatcher
from tools import build_tools
from wiki_store import WikiStore

logger = logging.getLogger(__name__)


# ── System prompt for the agent ─────────────────────────────────────────

# The prompt lives in `prompts.py` (2026-10-15).
from prompts import agent_system_prompt as _system_prompt


# ── Agent class ─────────────────────────────────────────────────────────

class LoveWorkAgent:
    """The personal job discovery agent.

    Wraps our pipeline as pi-agent tools and exposes an interactive loop.
    The autonomous-mode pipeline still works (see main.py); this class
    adds the conversational / on-demand layer on top.
    """

    def __init__(
        self,
        profile_name: str,
        role: str,
        profile_text: str,
        registry: JobRegistry,
        matcher: JobMatcher,
        crawler: SmartCrawler,
        wiki: WikiStore,
    ):
        self.profile_name = profile_name
        self.role = role
        self.profile_text = profile_text
        self.registry = registry
        self.matcher = matcher
        self.crawler = crawler
        self.wiki = wiki

        # Build the model + tools
        self.model = build_model()
        self.tools = build_tools(registry, matcher, crawler, wiki, profile_name=profile_name, role=role)
        self.system_prompt = _system_prompt(profile_name, role, profile_text)

    @classmethod
    def from_profile(
        cls,
        profile_name: str,
        role: str = "general",
        use_dspy: bool = False,
    ) -> "LoveWorkAgent":
        """Construct an agent from a profile name + role.

        use_dspy=True: uses DSPy typed signatures (compileable, optimisable).
        use_dspy=False (default): uses legacy hand-written prompts.
        """
        profile_text = config.load_profile_text(profile_name, role=role)
        registry = JobRegistry()
        llm = LLMClient()
        crawler = SmartCrawler(llm, use_dspy=use_dspy)
        if use_dspy:
            from matcher import JobMatcherDSPyAdapter
            matcher = JobMatcherDSPyAdapter(profile_text, registry=registry, use_history=True)
        else:
            matcher = JobMatcher(llm, profile_text, registry=registry, use_history=True)
        wiki = WikiStore()
        return cls(profile_name, role, profile_text, registry, matcher, crawler, wiki)

    def run(self, user_message: str) -> str:
        """Run the agent on a single user message. Returns the agent's final answer.

        This is the interactive-mode entry point. The agent will:
        1. Read the user's question
        2. Decide which tools to call (LLM-driven)
        3. Call them, observe results
        4. Repeat until it has an answer
        5. Return the final summary
        """
        context = AgentContext(
            system_prompt=self.system_prompt,
            messages=[],
            tools=self.tools,
        )

        config_loop = AgentLoopConfig(
            model=self.model,
            convert_to_llm=default_convert_to_llm,
            api_key=get_api_key(),
        )

        prompts: List[AgentMessage] = [UserMessage(content=user_message)]

        logger.info(f"[{self.profile_name}/{self.role}] Agent starting: {user_message[:100]}...")

        final_messages = run_agent_loop(prompts, context, config_loop)

        # Extract the last assistant text
        for msg in reversed(final_messages):
            if hasattr(msg, "content") and msg.content:
                for c in msg.content:
                    if hasattr(c, "text") and c.text:
                        return c.text
        return "(agent produced no response)"


# ── Convenience: autonomous mode still works ────────────────────────────

def run_autonomous(profile_name: str, role: str, source: str = "all") -> None:
    """Run the full pipeline in autonomous mode (cron-like).

    This is what the scheduled cron entry point calls (host-a/HermeL: LJ full
    sweep Sat 10:00, VJ Sun 10:00, KJ Sun 14:00). It calls the shared
    pipeline (crawl → registry → match → wiki) directly — no CLI/argv coupling,
    so a future FastAPI service (Phase 3) can call the same function.
    """
    # Lazy import to avoid circular deps (pipeline imports matcher, etc.).
    from pipeline import run_pipeline

    run_pipeline(
        profile_name,
        role=role,
        source=source,
        write_report=True,
    )
