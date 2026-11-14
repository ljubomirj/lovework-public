"""
Source: Neolabs and emerging AI labs (from neolab-and-emerging-ai-lab-tracker.txt).
Parses the local tracker file and crawls each lab's careers page.
"""

import logging
import re
from pathlib import Path
from typing import List

import config
from crawler import SmartCrawler, job_evidence_text
from job_registry import JobRegistry
from matcher import JobMatcher
from wiki_store import WikiEntry, match_fields

logger = logging.getLogger(__name__)


class NeolabsSource:
    #: This source's tier, declared here and nowhere else
    #: (docs/28 §WP3b box 1). Primary = the hiring party's own
    #: publication; secondary = someone relaying it.
    primary = False
    #: Set when the run enumerated its *whole* scope (no truncation,
    #: caps or failures). Only then may absence become
    #: `not_listed_in_scope` rather than `disappeared`.
    complete_scope = False
    """Crawls a local tracker-list file (neolabs, neoclouds, …) for job openings.

    Subclasses override `name`, `tracker_path` and `goal`; the parser and the run
    loop are shared so every tracker file uses exactly one format.
    """

    name = "neolabs"
    tracker_path = config.NEOLAB_TRACKER
    goal = (
        "Find open positions at this frontier AI lab / startup. "
        "Look for: research scientist, research engineer, ML engineer, "
        "founding engineer, applied scientist, or similar technical roles."
    )

    def __init__(self, crawler: SmartCrawler, matcher: JobMatcher, registry: JobRegistry = None):
        self.crawler = crawler
        self.matcher = matcher
        self.registry = registry

    def _parse_tracker(self) -> List[dict]:
        """Parse the tracker txt into org dicts."""
        orgs = []
        tracker = Path(self.tracker_path)
        if not tracker.exists():
            logger.warning(f"Tracker file not found: {tracker}")
            return orgs

        text = tracker.read_text(encoding="utf-8")
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

        logger.info(f"Parsed {len(orgs)} orgs from {self.name} tracker")
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
                    goal=self.goal,
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
                            # A swallowed upsert is data loss, and the exception type is
                            # what separates contract drift from a transient failure
                            # (LEARNINGS 2026-09-20).
                            logger.warning(f"Registry upsert failed "
                                           f"({type(e).__name__}): {e}")

                    desc = job_evidence_text(job)
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
