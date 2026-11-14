"""
DSPy signatures for LoveWork.

Typed, declarative LLM prompts. Each signature has:
- Input fields (what the LLM sees)
- Output fields (what the LLM returns, typed)
- A docstring (the task description)
- Field-level descriptions

DSPy can then compile these against a metric and optimise them automatically.
See https://dspy.ai for the full model.

Signatures:
- CrawlDecision: where to crawl next on a careers site
- ExtractJobs: pull structured listings from a page with jobs
- MatchJob: score a job against the principal's profile
"""

import logging
from typing import List, Optional

import dspy

logger = logging.getLogger(__name__)


# ── CrawlDecision ───────────────────────────────────────────────────────

# The signatures live in `prompts.py` — their docstrings and field
# descriptions are the prompts, and they belong with the rest (2026-10-15).
from prompts import CrawlDecision, ExtractJobs, MatchJob  # noqa: F401



# ── ExtractJobs ─────────────────────────────────────────────────────────



# ── MatchJob ────────────────────────────────────────────────────────────



# ── Module wrappers ─────────────────────────────────────────────────────

class SmartCrawlerDSPy(dspy.Module):
    """DSPy module wrapping the two-stage crawl: decide + extract."""

    def __init__(self):
        super().__init__()
        self.decide = dspy.ChainOfThought(CrawlDecision)
        self.extract = dspy.ChainOfThought(ExtractJobs)

    def decide_next(
        self, org_name: str, url: str, content: str, goal: str
    ) -> dspy.Prediction:
        return self.decide(org_name=org_name, url=url, content=content, goal=goal)

    def extract_jobs(
        self, org_name: str, url: str, content: str, goal: str
    ) -> List[dict]:
        result = self.extract(org_name=org_name, url=url, content=content, goal=goal)
        return result.jobs or []


class JobMatcherDSPy(dspy.Module):
    """DSPy module for job-to-profile matching."""

    def __init__(self):
        super().__init__()
        self.match = dspy.ChainOfThought(MatchJob)

    def match(
        self,
        profile: str,
        job_title: str,
        job_description: str,
        org_name: str,
        job_url: str = "",
        additional_context: str = "",
    ) -> dspy.Prediction:
        return self.match(
            profile=profile,
            job_title=job_title,
            job_description=job_description,
            org_name=org_name,
            job_url=job_url,
            additional_context=additional_context,
        )


# ── Configuration helper ────────────────────────────────────────────────

def configure_dspy() -> None:
    """Configure DSPy to use our LLM (DeepSeek via OpenAI-compatible API).

    Call this once at startup. Idempotent.
    """
    import config

    lm = dspy.LM(
        model=config.LLM_MODEL,
        api_key=config.LLM_API_KEY,
        api_base=config.LLM_BASE_URL,
        temperature=config.LLM_TEMPERATURE,
        max_tokens=config.LLM_MAX_TOKENS,
    )
    dspy.configure(lm=lm)
    logger.info(f"DSPy configured with model={config.LLM_MODEL} at {config.LLM_BASE_URL}")