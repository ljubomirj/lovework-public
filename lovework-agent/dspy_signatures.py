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
- MatchJob: score a job against the candidate's profile
"""

import logging
from typing import List, Optional

import dspy

logger = logging.getLogger(__name__)


# ── CrawlDecision ───────────────────────────────────────────────────────

class CrawlDecision(dspy.Signature):
    """Decide where to crawl next on a careers site.

    Given a page from an organisation's website, decide:
    1. Are there job listings on this page? If yes, list them briefly.
    2. If not, what URLs should we visit next to find jobs?
    3. How confident are you in this assessment?
    """

    org_name: str = dspy.InputField(desc="Name of the organisation we're crawling")
    url: str = dspy.InputField(desc="The URL of the page we're examining")
    content: str = dspy.InputField(desc="Markdown content of the page (truncated to ~12K chars)")
    goal: str = dspy.InputField(desc="What kinds of jobs we're looking for (role-specific)")

    found_jobs: bool = dspy.OutputField(desc="True if job listings were found on this page")
    job_listings: List[dict] = dspy.OutputField(
        desc="Brief mentions of any jobs found (title, location, URL)",
    )
    next_urls: List[str] = dspy.OutputField(
        desc="Up to 3 URLs to visit next to find more jobs",
    )
    confidence: int = dspy.OutputField(desc="Confidence in this assessment, 0-10")
    reasoning: str = dspy.OutputField(desc="Brief reasoning for the decision")


# ── ExtractJobs ─────────────────────────────────────────────────────────

class ExtractJobs(dspy.Signature):
    """Extract structured job listings from a page that has them.

    Pull each job's title, team, location, URL, and brief description.
    Keep snippets very brief (under 20 words each) to avoid output truncation.
    """

    org_name: str = dspy.InputField(desc="Name of the organisation")
    url: str = dspy.InputField(desc="The URL of the page with jobs")
    content: str = dspy.InputField(desc="Markdown content of the page (truncated)")
    goal: str = dspy.InputField(desc="What kinds of jobs we're looking for")

    jobs: List[dict] = dspy.OutputField(
        desc=(
            "List of jobs. Each has: title (required), team, location, url, "
            "description_snippet (<= 20 words), requirements_snippet (<= 20 words), "
            "employment_type, posted_date."
        ),
    )


# ── MatchJob ────────────────────────────────────────────────────────────

class MatchJob(dspy.Signature):
    """Score a job listing against a candidate's profile.

    Returns three independent 0-10 axes:
    - fit_score: skill/intellectual alignment
    - reach_score: realistic screening odds
    - flourish_score: day-to-day enjoyment

    Special signals to incorporate:
    - Long-lasting jobs (open >30 days) score 1-2 lower
    - Prior rejection for same role within 6 months = DROP
    - "New" status = fresh opportunity, no penalty
    - If the role aligns with one of the candidate's explicit branching
      possibilities (in the profile's CANDIDATE POSSIBILITIES section), add +1
      and name the branch letter (e.g. "(a)", "(g)") in the reasoning.
    - Small/early startups are more reachable than star-researcher labs. Rare
      direct-domain experience plus a shipped/demoed artifact should normally
      yield fit 9-10, reach 7-9, flourish 8-10; prefer concrete artifacts in
      the evidence alignment and application angle. Relevant evidence marked
      [CONCRETE ARTIFACT] must lead the application angle.
    """

    profile: str = dspy.InputField(desc="The candidate's profile (soul + CV + branching possibilities + role criteria)")
    job_title: str = dspy.InputField(desc="The job title")
    job_description: str = dspy.InputField(desc="1-3 sentence description of the role")
    org_name: str = dspy.InputField(desc="Name of the hiring organisation")
    job_url: str = dspy.InputField(desc="Direct link to the job posting", default="")
    additional_context: str = dspy.InputField(
        desc="Job registry status + prior contact summary (if any)",
        default="",
    )

    fit_score: float = dspy.OutputField(desc="Skill/intellect alignment, 0.0-10.0")
    reach_score: float = dspy.OutputField(desc="Realistic screening odds, 0.0-10.0")
    flourish_score: float = dspy.OutputField(desc="Day-to-day enjoyment, 0.0-10.0")
    prestige_trap_risk: str = dspy.OutputField(desc="One of: low, medium, high")
    screening_story: str = dspy.OutputField(desc="How the candidate might, or might not, get through screening")
    likely_day_to_day: str = dspy.OutputField(desc="What the actual work likely feels like")
    alignment_matrix: list[str] = dspy.OutputField(desc="3-6 job need -> candidate evidence alignments")
    gaps: list[str] = dspy.OutputField(desc="Material requirements with no supplied candidate evidence")
    application_angle: str = dspy.OutputField(desc="Specific truthful application narrative from strongest evidence")
    reasoning: str = dspy.OutputField(desc="Brief explanation tying the three axes together")


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
