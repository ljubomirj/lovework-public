"""
Source: Neolabs and emerging AI labs (from neolab-and-emerging-ai-lab-tracker.txt).
Parses the local tracker file and crawls each lab's careers page.
"""

import logging
import re
from pathlib import Path
from typing import List

import config
from crawler import SmartCrawler
from job_registry import JobRegistry
from matcher import JobMatcher
from wiki_store import WikiEntry, match_fields

logger = logging.getLogger(__name__)

TRACKER_PATH = config.NEOLAB_TRACKER


class NeolabsSource:
    """Crawls neolabs from the local tracker file."""

    name = "neolabs"

    def __init__(self, crawler: SmartCrawler, matcher: JobMatcher, registry: JobRegistry = None):
        self.crawler = crawler
        self.matcher = matcher
        self.registry = registry

    def _parse_tracker(self) -> List[dict]:
        """Parse the neolab tracker txt into org dicts."""
        orgs = []
        if not TRACKER_PATH.exists():
            logger.warning(f"Tracker file not found: {TRACKER_PATH}")
            return orgs

        text = TRACKER_PATH.read_text(encoding="utf-8")
        lines = text.splitlines()
        current = None

        for line in lines:
            line = line.rstrip()
            if line.startswith("•"):
                # Save previous org before starting a new one
                if current is not None:
                    orgs.append(current)
                    current = None
                m = re.match(r"•\s+(.+?)\s+—\s+(https?://\S+)\s+—\s+(.*)", line)
                if m:
                    name = m.group(1).strip()
                    url = m.group(2).strip()
                    rest = m.group(3).strip()
                    current = {"name": name, "url": url, "careers_url": None, "rest": rest}
            elif current is not None:
                if "careers:" in line.lower():
                    m = re.search(r"Careers:\s*(https?://\S+)", line, re.I)
                    if m:
                        current["careers_url"] = m.group(1).strip()
                elif line.strip() == "":
                    # Blank line ends current org
                    orgs.append(current)
                    current = None

        if current is not None:
            orgs.append(current)

        logger.info(f"Parsed {len(orgs)} orgs from neolab tracker")
        return orgs

    def run(self) -> List[WikiEntry]:
        entries: List[WikiEntry] = []
        orgs = self._parse_tracker()

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
                        "Find open positions at this frontier AI lab / startup. "
                        "Look for: research scientist, research engineer, ML engineer, "
                        "founding engineer, applied scientist, or similar technical roles."
                    ),
                )
                for job in jobs:
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
                    if record is not None:
                        entry.lifecycle_status = record.status
                        entry.first_seen = record.first_seen
                    entries.append(entry)
            except Exception as e:
                logger.error(f"[{self.name}] Failed on {org_name}: {e}")

        return entries
