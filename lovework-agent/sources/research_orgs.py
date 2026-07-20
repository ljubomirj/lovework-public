"""
Source: Research orgs list (from juleslogs tweet, 2026-05-27).
Crawls each org's website for open positions.
"""

import logging
from typing import List

import config
from crawler import SmartCrawler
from job_registry import JobRecord, JobRegistry
from matcher import JobMatcher
from wiki_store import WikiEntry, match_fields

logger = logging.getLogger(__name__)


class ResearchOrgsSource:
    """Crawls the research-orgs list for job openings."""

    name = "research_orgs"

    def __init__(self, crawler: SmartCrawler, matcher: JobMatcher, registry: JobRegistry = None):
        self.crawler = crawler
        self.matcher = matcher
        self.registry = registry

    def run(self) -> List[WikiEntry]:
        entries: List[WikiEntry] = []
        orgs = config.RESEARCH_ORGS

        for org in orgs:
            org_name = org["name"]
            urls = [org["url"]]
            careers_url = org.get("careers_url")
            if careers_url:
                urls.append(careers_url)

            logger.info(f"[{self.name}] Checking {org_name}")
            try:
                jobs = self.crawler.crawl_org(
                    org_name=org_name,
                    seed_urls=urls,
                    goal=(
                        "Find open positions suitable for an experienced ML/AI researcher/engineer. "
                        "Look for: research scientist, research engineer, ML engineer, AI engineer, "
                        "residency, fellowship, or similar technical roles. Avoid pure finance/quant roles."
                    ),
                )
                for job in jobs:
                    # Upsert into registry to track lifecycle
                    record = None
                    if self.registry is not None:
                        try:
                            record = self.registry.upsert(
                                org=org_name,
                                title=job.title,
                                url=job.url or "",
                                careers_url=careers_url or "",
                                source=self.name,
                            )
                        except Exception as e:
                            logger.debug(f"Registry upsert failed: {e}")

                    desc = " ".join(
                        filter(
                            None,
                            [
                                job.description_snippet,
                                job.requirements_snippet,
                                job.employment_type,
                            ],
                        )
                    )
                    match = self.matcher.match(
                        job.title, desc, org_name, job_url=job.url or "",
                        location=job.location or "",
                    )
                    entry = WikiEntry(
                        org_name=org_name,
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
                    # Tag with registry status for the report
                    if record is not None:
                        entry.lifecycle_status = record.status
                        entry.first_seen = record.first_seen
                    entries.append(entry)
            except Exception as e:
                logger.error(f"[{self.name}] Failed on {org_name}: {e}")

        return entries
