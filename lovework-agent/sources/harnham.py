"""
Source: Harnham search-result pages.

Harnham does not provide a useful job-alert registration flow for LJ's
Gmail LJ-jobs inbox, but its public search endpoint is queryable. This
source keeps those search URLs under the LJ profile and crawls them as a
regular source.
"""

import logging
import os
from pathlib import Path
from typing import List, Optional

import yaml

import config
from crawler import SmartCrawler
from job_registry import JobRegistry
from matcher import JobMatcher
from wiki_store import WikiEntry, match_fields

logger = logging.getLogger(__name__)

SEARCHES_FILENAME = "harnham_searches.yaml"
MAX_SEARCHES_PER_RUN = int(os.getenv("LOVEWORK_HARNHAM_MAX_SEARCHES", "10"))


def _profile_dir() -> Path:
    return config.PROFILES_DIR / "lj"


def _searches_path() -> Path:
    return _profile_dir() / SEARCHES_FILENAME


def _seed_searches() -> None:
    """Create LJ's starter Harnham search list. Idempotent."""
    path = _searches_path()
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "name": "agentic-engineer",
            "url": "https://www.harnham.com/job-search/?_keyword=agentic%20engineer",
            "reason": "Manual search: agentic engineer",
        },
        {
            "name": "agentic-engineer-london-contract",
            "url": (
                "https://www.harnham.com/job-search/?_keyword=agentic%20engineer"
                "&_specialism=2cba37376f4d7aa921494e8a4fc12888"
                "&_location=england&_location_city=london&_job_type=contract"
            ),
            "reason": "Manual search: agentic engineer, London, contract",
        },
    ]
    path.write_text(yaml.safe_dump(rows, sort_keys=False), encoding="utf-8")
    logger.info(f"[harnham] Seeded Harnham search list at {path}")


def _load_searches() -> List[dict]:
    _seed_searches()
    path = _searches_path()
    rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [r for r in rows if isinstance(r, dict) and r.get("url")]


class HarnhamSource:
    """Crawls LJ-maintained Harnham search URLs."""

    name = "harnham"

    def __init__(
        self,
        crawler: SmartCrawler,
        matcher: Optional[JobMatcher] = None,
        registry: Optional[JobRegistry] = None,
    ):
        self.crawler = crawler
        self.matcher = matcher
        self.registry = registry

    def run(self) -> List[WikiEntry]:
        searches = _load_searches()[:MAX_SEARCHES_PER_RUN]
        if not searches:
            logger.info(f"[{self.name}] No Harnham searches configured; skipping.")
            return []

        entries: List[WikiEntry] = []
        for row in searches:
            search_name = str(row.get("name") or "harnham-search")
            url = str(row["url"])
            reason = str(row.get("reason") or search_name)
            logger.info(f"[{self.name}] Crawling {search_name}: {url}")

            try:
                jobs = self.crawler.crawl_org(
                    org_name="Harnham",
                    seed_urls=[url],
                    goal=(
                        "Extract open Harnham job adverts from this search-results page. "
                        "Prioritise AI/ML, agentic engineering, ML engineering, data-centric "
                        "ML, and London/UK contract roles. Include recruiter/reposted adverts, "
                        "but preserve the source URL so downstream scoring can judge reliability."
                    ),
                    max_pages=3,
                )
            except Exception as e:
                logger.error(f"[{self.name}] Crawl failed for {search_name}: {e}")
                continue

            for job in jobs:
                record = None
                if self.registry is not None:
                    try:
                        record = self.registry.upsert(
                            org="Harnham",
                            title=job.title,
                            url=job.url or "",
                            careers_url=url,
                            source=self.name,
                        )
                    except Exception as e:
                        logger.debug(f"[{self.name}] Registry upsert failed: {e}")

                if self.matcher is None:
                    continue

                desc = " ".join(filter(None, [
                    reason,
                    job.description_snippet,
                    job.requirements_snippet,
                    job.employment_type,
                    "Source is a recruiter search page; consider ghost/stale advert risk.",
                ]))
                match = self.matcher.match(
                    job.title,
                    desc,
                    "Harnham",
                    job_url=job.url or "",
                    location=job.location or "",
                )
                entry = WikiEntry(
                    org_name="Harnham",
                    title=job.title,
                    url=job.url,
                    location=job.location,
                    score=match.score,
                    decision=match.decision,
                    reasoning=match.reasoning,
                    source=self.name,
                    advert_excerpt=desc,
                    **match_fields(match),
                )
                if record is not None:
                    entry.lifecycle_status = record.status
                    entry.first_seen = record.first_seen
                entries.append(entry)

        logger.info(f"[{self.name}] Produced {len(entries)} entries")
        return entries
