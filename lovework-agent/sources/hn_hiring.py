"""
Source: Hacker News "Ask HN: Who is hiring?" monthly thread.

Replaces the old local-report scraper with a live HN Algolia API pull. The
hiring threads are first-class HN items (one per month); each top-level
comment is one job post. The agent can decide readily if a comment is a
match because the canonical format is:

    Company | Role | Location [| Apply URL]
    <one-paragraph description>

Discovery: HN Algolia's /search endpoint with the phrase
"Ask HN Who is hiring". The newest thread wins; cron runs around the 1st
of the month (and a few days after) until the new thread is indexed.

Job location/visa text in the comment is sent to the matcher, which fires
the work-auth hard-kill (e.g. "US citizen only" → DROP) before the LLM call.

Compare to the other HN source (hn_jobs) which scrapes /jobs and needs a
click-through to judge.
"""

import logging
import os
import re
from typing import List, Optional

import config
from job_registry import JobRegistry
from matcher import JobMatcher
from wiki_store import WikiEntry, match_fields

from .hn_common import (
    fetch_thread_comments,
    find_latest_hiring_thread_id,
    parse_hn_job_comment,
    HN_WEB_BASE,
)

logger = logging.getLogger(__name__)

# Tunables.
THREAD_ID_OVERRIDE = os.getenv("LOVEWORK_HN_HIRING_THREAD_ID", "").strip()
# Cap on how many top-level comments to pull per run. The June 2026 thread
# had ~322 comments; 250 is a sensible default that bounds LLM cost while
# covering nearly all real listings.
MAX_COMMENTS = int(os.getenv("LOVEWORK_HN_HIRING_MAX_COMMENTS", "250"))
# Cap on entries we record in the wiki from this source per run (after
# match). Without this, a noisy thread could explode the report.
MAX_ENTRIES = int(os.getenv("LOVEWORK_HN_HIRING_MAX_ENTRIES", "200"))


class HNHiringSource:
    """Reads the monthly "Ask HN: Who is hiring?" thread live."""

    name = "hn_hiring"

    def __init__(self, crawler=None, matcher: Optional[JobMatcher] = None,
                 registry: Optional[JobRegistry] = None,
                 thread_id: Optional[int] = None):
        # `crawler` accepted for interface uniformity; this source never crawls.
        self.matcher = matcher
        self.registry = registry
        # Configurable thread ID > env override > auto-discover.
        env_thread = int(THREAD_ID_OVERRIDE) if THREAD_ID_OVERRIDE else None
        self.thread_id = thread_id or env_thread

    def _resolve_thread_id(self) -> Optional[int]:
        if self.thread_id:
            return self.thread_id
        oid = find_latest_hiring_thread_id()
        if oid:
            logger.info(f"[{self.name}] Auto-discovered thread id: {oid} ({HN_WEB_BASE}/item?id={oid})")
        return oid

    def run(self) -> List[WikiEntry]:
        entries: List[WikiEntry] = []
        thread_id = self._resolve_thread_id()
        if not thread_id:
            logger.warning(f"[{self.name}] No 'Ask HN: Who is hiring?' thread found; skipping.")
            return entries

        comments = fetch_thread_comments(thread_id, max_kids=MAX_COMMENTS)
        if not comments:
            logger.warning(f"[{self.name}] Thread {thread_id} returned 0 comments (network or empty).")
            return entries

        logger.info(f"[{self.name}] Thread {thread_id}: {len(comments)} top-level comments")
        for c in comments:
            if len(entries) >= MAX_ENTRIES:
                break
            parsed = parse_hn_job_comment(c)
            if not parsed:
                continue
            entry = self._make_entry(parsed)
            if entry is not None:
                entries.append(entry)

        logger.info(f"[{self.name}] {len(entries)} entries (after parser + matcher)")
        return entries

    def _make_entry(self, job: dict) -> Optional[WikiEntry]:
        if self.matcher is None:
            return None
        org_name = job["company"]
        title = job["role"]
        location = job.get("location", "")
        body = job.get("body", "")
        url = job.get("url", "")

        # The comment body is the description; location and any inline visa
        # text is also passed so the work-auth kill sees it.
        match = self.matcher.match(
            title, body, org_name,
            job_url=url or "",
            location=location,
        )

        # Registry upsert — these jobs rarely have a real careers_url, so we
        # use the comment permalink as the canonical "more info" URL.
        record = None
        hn_id = job.get("hn_comment_id")
        comment_url = f"{HN_WEB_BASE}/item?id={hn_id}" if hn_id else ""
        registry_url = url or comment_url
        if self.registry is not None:
            try:
                record = self.registry.upsert(
                    org=org_name, title=title, url=registry_url,
                    careers_url=comment_url, source=self.name,
                    discovery_url=comment_url,
                    discovery_date=job.get("discovery_date", ""),
                )
            except Exception as e:
                logger.debug(f"[{self.name}] Registry upsert failed: {e}")

        entry = WikiEntry(
            org_name=org_name,
            title=title,
            url=url or (comment_url or None),
            location=location or None,
            score=match.score,
            decision=match.decision,
            reasoning=match.reasoning,
            source=self.name,
            discovery_url=comment_url,
            discovery_date=job.get("discovery_date", ""),
            advert_excerpt=body,
            **match_fields(match),
        )
        if record is not None:
            entry.lifecycle_status = record.status
            entry.first_seen = record.first_seen
        return entry
