"""
Source: AI for Hedge Funds Startup Tracker (Alex Izydorczyk).
Parses the local CSV/HTML tracker and crawls each startup's site.
"""

import csv
import io
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

TRACKER_DIR = config.HF_TRACKER_DIR


class HFStartupsSource:
    """Crawls AI-for-Hedge-Fund startups from the local tracker."""

    name = "hf_startups"

    def __init__(self, crawler: SmartCrawler, matcher: JobMatcher, registry: JobRegistry = None):
        self.crawler = crawler
        self.matcher = matcher
        self.registry = registry

    def _parse_csv(self, csv_path: Path) -> List[dict]:
        """Parse Datawrapper CSV export (Company, Website columns)."""
        orgs = []
        text = csv_path.read_text(encoding="utf-8", errors="ignore")
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            name = row.get("Company", "").strip()
            url = row.get("Website", "").strip()
            status = row.get("Status", "").strip()
            if not name or not url:
                continue
            if not url.startswith("http"):
                continue
            # Skip acquired / pivoted / inactive
            if status.lower() in ("acquired", "pivot", "dead"):
                continue
            orgs.append({"name": name, "url": url, "careers_url": None})
        return orgs

    def _parse_html(self, html_path: Path) -> List[dict]:
        """Fallback: parse old saved HTML format."""
        orgs = []
        text = html_path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>([^<]{3,60})</a>', text):
            url = m.group(1)
            name = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if any(b in url.lower() for b in ("twitter.com", "x.com", "linkedin.com", "mailto:", "github.com")):
                continue
            if name and url:
                orgs.append({"name": name, "url": url, "careers_url": None})
        return orgs

    def _parse_tracker(self) -> List[dict]:
        orgs = []

        # Prefer CSV (fresh Datawrapper export)
        csv_files = list(TRACKER_DIR.glob("*.csv"))
        if csv_files:
            # Use the newest CSV
            csv_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            orgs = self._parse_csv(csv_files[0])
            logger.info(f"Parsed {len(orgs)} startups from CSV: {csv_files[0].name}")
        else:
            # Fallback to HTML
            html_files = list(TRACKER_DIR.glob("*.html"))
            if not html_files:
                logger.warning(f"No tracker files found in {TRACKER_DIR}")
                return orgs
            for html_path in html_files:
                orgs.extend(self._parse_html(html_path))
            logger.info(f"Parsed {len(orgs)} startups from HTML tracker")

        seen = set()
        deduped = []
        for o in orgs:
            if o["url"] not in seen:
                seen.add(o["url"])
                deduped.append(o)

        # Limit to top 30 to keep costs reasonable
        return deduped[:30]

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
                        "Find open technical positions at this AI-for-finance startup. "
                        "Look for: ML engineer, AI researcher, quant researcher, data scientist, "
                        "founding engineer, or similar. Be mindful: this is for a candidate who wants "
                        "ML/AI research roles, NOT pure trading execution or portfolio management."
                    ),
                    max_pages=3,
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
