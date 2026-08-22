"""
Smart crawler with LLM-in-the-loop for iterative job discovery.

Strategy:
1. Fetch a seed URL via Firecrawl (renders JS, returns markdown).
2. Ask LLM: given this page, did we find jobs? Where should we look next?
3. Iterate up to MAX_PAGES_PER_ORG / MAX_DEPTH.
4. Extract structured job listings with LLM.
"""

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, Field

import config
from llm_client import LLMClient

logger = logging.getLogger(__name__)

# Try Firecrawl; fall back to httpx if not available
_firecrawl_available = False
_firecrawl_app = None
if config.FIRECRAWL_API_KEY:
    try:
        from firecrawl import FirecrawlApp

        _firecrawl_app = FirecrawlApp(api_key=config.FIRECRAWL_API_KEY)
        _firecrawl_available = True
    except Exception as e:
        logger.warning(f"Firecrawl import failed: {e}")


def _cache_path(url: str, cache_dir: Optional[Path] = None) -> Path:
    h = hashlib.sha256(url.encode()).hexdigest()[:16]
    return (cache_dir or config.CACHE_DIR) / f"page_{h}.md"


def fetch_page(url: str, use_cache: bool = True, cache_dir: Optional[Path] = None) -> str:
    """Fetch a URL and return markdown content. Uses disk cache."""
    cache = _cache_path(url, cache_dir)
    if use_cache and cache.exists():
        text = cache.read_text(encoding="utf-8")
        logger.info(f"Cache hit {url} ({len(text)} chars)")
        return text

    md = ""
    if _firecrawl_available and _firecrawl_app is not None:
        try:
            result = _firecrawl_app.scrape(url, formats=["markdown"])
            md = getattr(result, "markdown", "") or ""
            if md:
                logger.info(f"Firecrawl fetched {url} ({len(md)} chars)")
        except Exception as e:
            logger.warning(f"Firecrawl failed for {url}: {e}")

    if not md:
        # Fallback to httpx
        try:
            import httpx

            resp = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            from html import unescape

            text = unescape(resp.text)
            import re

            text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.S)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S)
            text = re.sub(r"<[^>]+>", "\n", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            md = text.strip()
            logger.info(f"HTTP fallback fetched {url} ({len(md)} chars)")
        except Exception as e:
            logger.error(f"Failed to fetch {url}: {e}")
            return ""

    if use_cache and md:
        cache.write_text(md, encoding="utf-8")
    return md


class CrawlDecision(BaseModel):
    """LLM decision after scanning a page."""

    found_jobs: bool = Field(description="Whether job listings were found on this page")
    job_listings: List[dict] = Field(default_factory=list, description="Any job listings found here")
    next_urls: List[str] = Field(default_factory=list, description="URLs to crawl next for jobs")
    confidence: int = Field(ge=0, le=10, description="Confidence in this assessment")
    reasoning: str = Field(description="Brief reasoning for the decision")


class ExtractedJob(BaseModel):
    """Structured job listing extracted by LLM."""

    title: str = Field(description="Job title")
    team: Optional[str] = Field(default=None, description="Team or department")
    location: Optional[str] = Field(default=None, description="Location or remote policy")
    url: Optional[str] = Field(default=None, description="Direct link to the job posting")
    description_snippet: Optional[str] = Field(default=None, description="1-2 sentence summary")
    requirements_snippet: Optional[str] = Field(default=None, description="Key requirements mentioned")
    employment_type: Optional[str] = Field(default=None, description="Full-time, part-time, contract, internship, residency, fellowship")
    posted_date: Optional[str] = Field(default=None, description="When the job was posted (ISO date or relative like '2 weeks ago')")


# ── Filters ──────────────────────────────────────────────────────────────

# Cities/regions that count as UK/Europe (LJ is based in London)
UK_EU_KEYWORDS = (
    "london", "uk", "united kingdom", "england", "scotland", "wales", "northern ireland",
    "edinburgh", "cambridge", "oxford", "manchester", "bristol", "dublin", "ireland",
    "paris", "france", "berlin", "germany", "munich", "amsterdam", "netherlands",
    "zurich", "geneva", "switzerland", "stockholm", "sweden", "oslo", "norway",
    "copenhagen", "denmark", "helsinki", "finland", "barcelona", "spain", "madrid",
    "milan", "italy", "vienna", "austria", "warsaw", "poland", "prague", "czech",
    "lisbon", "portugal", "brussels", "belgium", "remote", "anywhere", "global",
    "worldwide", "europe", "emea",
)

# Hard rejections — US-only or Remote (US)
US_KEYWORDS = (
    "united states", " u.s.", "u.s.a", "usa", "us only", "us-based",
    "san francisco", "san jose", "palo alto", "mountain view", "menlo park",
    "cupertino", "sunnyvale", "santa clara", "berkeley", "oakland", "los angeles",
    "san diego", "seattle", "bellevue", "redmond", "austin", "boston", "new york",
    "brooklyn", "chicago", "denver", "portland", "atlanta", "miami", "dallas",
    "houston", "philadelphia", "washington dc", "washington, dc", "north america",
    "americas", "canada", "toronto", "vancouver", "montreal",
)


def is_location_acceptable(location: Optional[str]) -> bool:
    """Return True if the job's location is UK, EU, or explicitly Remote/Global.

    Location strings can be semicolon-separated (e.g. "Berkeley Office; Remote (International);
    Remote (US)"). We split on ; and | and check each option. A job is acceptable if AT LEAST
    ONE option is UK/EU/global-remote (even if others are US-only).
    """
    if not location:
        # No location info — be conservative, allow it (LLM may filter later)
        return True

    # Split into individual options
    options = re.split(r"[;|]", location)
    options = [o.strip() for o in options if o.strip()]

    if not options:
        return True

    # A job is acceptable if ANY option is UK/EU-eligible
    for opt in options:
        if _is_single_location_ok(opt):
            return True

    # If we get here, none of the options were UK/EU-eligible
    return False


def _is_single_location_ok(loc: str) -> bool:
    """Check a single location option (no semicolons)."""
    loc_lower = loc.lower().strip()

    # Hard reject if US-only signals present
    for kw in US_KEYWORDS:
        if kw in loc_lower:
            return False

    # Reject "Remote" without explicit EU/global qualifier (default = US-only)
    if "remote" in loc_lower or "hybrid" in loc_lower:
        for kw in ("eu", "emea", "europe", "global", "worldwide", "anywhere",
                   "uk", "european", "international"):
            if kw in loc_lower:
                return True
        # Plain "Remote" or "Hybrid" with no qualifier — reject
        return False

    # Accept if any UK/EU signal present
    for kw in UK_EU_KEYWORDS:
        if kw in loc_lower:
            return True

    # Unknown — accept (LLM matcher will catch US later)
    return True


def is_recent(posted_date: Optional[str], max_weeks: int = 4) -> bool:
    """Return True if the job was posted within the last max_weeks weeks."""
    if not posted_date:
        # No date info — be conservative and accept (date may not be on the page)
        return True
    from datetime import datetime, timedelta

    text = posted_date.lower().strip()
    now = datetime.now()

    # Relative formats: "2 weeks ago", "3 days ago", "1 month ago"
    import re

    rel = re.match(r"(\d+)\s*(day|week|month)s?\s*ago", text)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2)
        if unit == "day":
            return n <= max_weeks * 7
        if unit == "week":
            return n <= max_weeks
        if unit == "month":
            return n <= 1  # ~1 month ≈ 4 weeks

    # ISO date: 2026-06-15
    try:
        dt = datetime.fromisoformat(posted_date)
        return (now - dt) <= timedelta(weeks=max_weeks)
    except (ValueError, TypeError):
        pass

    # "Just posted", "Today", "Yesterday"
    if any(t in text for t in ("today", "just posted", "yesterday", "new")):
        return True

    return True  # Unknown format — accept


def filter_jobs(jobs: List["ExtractedJob"], max_weeks: int = 4) -> List["ExtractedJob"]:
    """Filter jobs by recency and location. Returns only acceptable jobs."""
    out = []
    for j in jobs:
        if not is_location_acceptable(j.location):
            logger.debug(f"Filter: dropped (location) {j.title} @ {j.location}")
            continue
        if not is_recent(j.posted_date, max_weeks):
            logger.debug(f"Filter: dropped (stale) {j.title} posted {j.posted_date}")
            continue
        out.append(j)
    return out


class SmartCrawler:
    """Iterative LLM-guided crawler for a single organization.

    Two prompt implementations:
    - use_dspy=False (default): hand-written prompts via LLMClient.structured()
    - use_dspy=True: DSPy typed signatures (compileable, optimisable)

    Both produce the same outputs (CrawlDecision, List[ExtractedJob]).
    """

    def __init__(
        self, llm: LLMClient, use_dspy: bool = False, cache_dir: Optional[Path] = None
    ):
        self.llm = llm
        self.visited: set = set()
        self.use_dspy = use_dspy
        self.cache_dir = cache_dir
        self._dspy = None
        if use_dspy:
            try:
                from dspy_signatures import SmartCrawlerDSPy, configure_dspy
                configure_dspy()
                self._dspy = SmartCrawlerDSPy()
                logger.info("SmartCrawler using DSPy signatures")
            except Exception as e:
                logger.warning(f"DSPy not available, falling back to legacy prompts: {e}")
                self.use_dspy = False

    def crawl_org(
        self,
        org_name: str,
        seed_urls: List[str],
        goal: str = "Find open job or research positions suitable for an experienced ML/AI researcher.",
        max_pages: int = config.MAX_PAGES_PER_ORG,
        max_depth: int = config.MAX_DEPTH,
    ) -> List[ExtractedJob]:
        """
        Crawl an organization's site iteratively to find job listings.
        Returns deduplicated ExtractedJob objects.
        """
        self.visited = set()
        seen_urls = set()
        queue: List[tuple] = []
        for url in seed_urls:
            if url not in seen_urls:
                seen_urls.add(url)
                queue.append((url, 0))
        all_jobs: List[ExtractedJob] = []
        pages_crawled = 0

        while queue and pages_crawled < max_pages:
            url, depth = queue.pop(0)
            if url in self.visited or depth > max_depth:
                continue
            self.visited.add(url)

            logger.info(f"[{org_name}] Crawling {url} (depth={depth})")
            content = fetch_page(url, cache_dir=self.cache_dir)
            if not content:
                continue

            # Truncate very long content for LLM
            truncated = content[:12000]

            # Ask LLM what to do with this page
            decision = self._ask_decision(org_name, url, truncated, goal)
            pages_crawled += 1

            if decision.found_jobs:
                # Try extraction even if job_listings is empty (LLM may have
                # returned strings, which fail Pydantic validation)
                jobs = self._extract_jobs_from_page(org_name, url, truncated, goal)
                all_jobs.extend(jobs)
            elif decision.confidence == 0 and any(
                kw in url.lower() for kw in ("careers", "jobs", "positions", "openings", "fellows", "residency")
            ):
                # Parse failure on a careers page — try extraction anyway
                logger.info(f"[{org_name}] Parse-failure fallback: extracting from {url}")
                jobs = self._extract_jobs_from_page(org_name, url, truncated, goal)
                all_jobs.extend(jobs)

            for next_url in decision.next_urls[:3]:  # Limit branching
                absolute = urljoin(url, next_url)
                # Sanity filter: same domain or known careers domains
                if absolute not in self.visited and absolute not in seen_urls and self._is_sensible_url(absolute, seed_urls):
                    seen_urls.add(absolute)
                    queue.append((absolute, depth + 1))

            time.sleep(config.REQUEST_DELAY_SECONDS)

        # Deduplicate by title+company
        seen = set()
        deduped = []
        for job in all_jobs:
            key = (org_name.lower(), job.title.lower())
            if key not in seen:
                seen.add(key)
                deduped.append(job)

        # Filter by recency and location
        before = len(deduped)
        deduped = filter_jobs(deduped, max_weeks=config.MAX_JOB_AGE_WEEKS)
        filtered = before - len(deduped)

        logger.info(
            f"[{org_name}] Crawled {pages_crawled} pages, found {len(deduped)} unique jobs "
            f"({filtered} filtered out as stale or US-only)"
        )
        return deduped

    def _ask_decision(self, org_name: str, url: str, content: str, goal: str) -> CrawlDecision:
        """Ask LLM to assess a page and decide next steps."""
        if self.use_dspy and self._dspy is not None:
            try:
                pred = self._dspy.decide_next(
                    org_name=org_name, url=url, content=content, goal=goal,
                )
                return CrawlDecision(
                    found_jobs=bool(getattr(pred, "found_jobs", False)),
                    job_listings=list(getattr(pred, "job_listings", []) or []),
                    next_urls=list(getattr(pred, "next_urls", []) or []),
                    confidence=int(getattr(pred, "confidence", 0) or 0),
                    reasoning=str(getattr(pred, "reasoning", "") or ""),
                )
            except Exception as e:
                logger.warning(f"DSPy decide failed for {url}: {e}, falling back to legacy")
                # fall through to legacy path

        # Legacy path: hand-written prompt + LLMClient.structured()
        prompt = f"""You are a smart job-discovery agent. Your goal: {goal}

Organization: {org_name}
Page URL: {url}

Below is the page content in markdown. Analyze it and decide:
1. Are there job listings on this page? If yes, list each as a JSON object with fields: title, team, location, url.
   IMPORTANT: job_listings must be a list of objects, NOT strings. Each object needs at least a "title" field.
2. If not, what URLs on this page should we visit next to find jobs? (careers, jobs, about, team, etc.)
3. Rate your confidence 0-10.
4. Provide brief reasoning.

Page content:
---
{content}
---
"""
        messages = [{"role": "user", "content": prompt}]
        try:
            return self.llm.structured(
                messages, CrawlDecision,
                context=f"[{org_name}] decision: {url}",
            )
        except Exception:
            logger.warning(f"Structured decision failed for {url}, continuing")
            return CrawlDecision(found_jobs=False, next_urls=[], confidence=0, reasoning="parse failed")

    def _extract_jobs_from_page(self, org_name: str, url: str, content: str, goal: str) -> List[ExtractedJob]:
        """Ask LLM to extract structured job listings from a page known to have jobs."""
        if self.use_dspy and self._dspy is not None:
            try:
                jobs = self._dspy.extract_jobs(
                    org_name=org_name, url=url, content=content, goal=goal,
                )
                # Convert dicts to ExtractedJob
                result: List[ExtractedJob] = []
                for j in jobs or []:
                    if isinstance(j, dict):
                        result.append(ExtractedJob(**j))
                    elif isinstance(j, ExtractedJob):
                        result.append(j)
                return result
            except Exception as e:
                logger.warning(f"DSPy extract failed for {url}: {e}, falling back to legacy")
                # fall through

        # Legacy path: hand-written prompt + LLMClient.structured()
        prompt = f"""Extract job listings from this page for organization: {org_name}
Page URL: {url}
Goal: {goal}

For each job, extract ONLY these fields (keep snippets very brief, under 20 words each):
- title (required)
- team (optional)
- location (optional, e.g. "London, UK" or "Remote (US)" or "Remote, EU")
- url (optional, direct link to posting)
- description_snippet (optional, 1 sentence max)
- requirements_snippet (optional, 1 sentence max)
- employment_type (optional: full-time, internship, residency, fellowship, etc.)
- posted_date (optional, e.g. "2026-06-15" or "2 weeks ago" or "Today" — whatever the page shows)

If a field is missing, omit it entirely. Be concise to avoid truncation.

Page content:
---
{content}
---
"""
        messages = [{"role": "user", "content": prompt}]
        try:
            class JobList(BaseModel):
                jobs: List[ExtractedJob]

            result = self.llm.structured(
                messages,
                JobList,
                max_tokens=8192,
                context=f"[{org_name}] extract: {url}",
            )
            return result.jobs
        except Exception as e:
            logger.warning(f"Job extraction failed for {url}: {e}")
            return []

    def _is_sensible_url(self, url: str, seed_urls: List[str]) -> bool:
        """Basic sanity filter for URLs."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if any(x in url.lower() for x in ("mailto:", "tel:", "javascript:", "#")):
            return False
        # Allow same domain as seeds
        seed_domains = {urlparse(u).netloc for u in seed_urls}
        if parsed.netloc in seed_domains:
            return True
        # Allow known ATS/careers domains
        allowed_hosts = ("jobs.ashbyhq.com", "boards.greenhouse.io", "jobs.lever.co",
                         "apply.workable.com", "careers.", "jobs.", "boards.")
        if any(h in parsed.netloc for h in allowed_hosts):
            return True
        return False
