"""
Source: Hacker News /jobs listings (news.ycombinator.com/jobs).

A second HN source — distinct from the monthly "Who is hiring?" thread.
The /jobs page is a flat list of all jobs posted by HN users, with a
short title (e.g. "Great Question (YC W21) Is Hiring Applied AI Interns")
and a single link that usually takes the candidate to a YC company page
or a careers site.

This source has a different signal than hn_hiring:
  - Listings are terse ("<Company> (YC <batch>) is hiring <Role>") — the
    matcher has very little description text to score against, so we
    resolve the link and harvest the actual job description.
  - Many listings are US-only / visa-restricted — the work-auth hard-kill
    catches most of these. The matcher's location param receives the
    full page text so the kill can scan for "US citizen only" / "no
    sponsorship" patterns.
  - Listings on /jobs come and go; a listing seen today may be gone
    tomorrow. The job registry tracks lifecycle (new → disappeared).

Flow:
  1. Fetch /jobs (HTML — HN doesn't have an API for this view).
  2. Parse out (title, link, age-text) tuples.
  3. For each listing, optionally fetch the linked page (Firecrawl) to
    extract the role description and location, then upsert + match.

The HN HTML is simple enough that BeautifulSoup isn't required — we use
regex on the canonical structure ("<td class="title"><span><a href=...>").
The fetching step reuses the SmartCrawler for the linked page so we
inherit the LLM-guided extraction and Firecrawl caching.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import List, Optional

from job_registry import JobRegistry
from matcher import JobMatcher
from wiki_store import WikiEntry, match_fields

from .hn_common import HN_REQUEST_TIMEOUT, HN_WEB_BASE

logger = logging.getLogger(__name__)

# Tunables.
JOBS_PAGE_URL = os.getenv("LOVEWORK_HN_JOBS_URL", f"{HN_WEB_BASE}/jobs")
MAX_LISTINGS = int(os.getenv("LOVEWORK_HN_JOBS_MAX_LISTINGS", "60"))
# Age in days — HN /jobs listings older than this are pre-filtered out.
MAX_AGE_DAYS = int(os.getenv("LOVEWORK_HN_JOBS_MAX_AGE_DAYS", "21"))
# HTTP fetch for the linked page (we use HN's own html — Firecrawl handles
# the company careers page; this is just a quick description grab).
FETCH_DETAIL_PAGES = os.getenv("LOVEWORK_HN_JOBS_FETCH_DETAIL", "1") != "0"

# ── HTML parsing ──────────────────────────────────────────────────────────

# Canonical HN /jobs structure (as of 2026-06):
#   <tr class="athing" id="<id>">
#     <td class="title"><span class="age"><a href="item?id=<id>">...</a></span></td>
#     <td class="title">  <a href="<external>">Title text</a>  </td>
#   </tr>
#
# The age is on a separate row. We pull the two columns in one regex by
# scanning line-by-line, which is robust to small markup changes.

TITLE_LINK_RE = re.compile(
    r'<a\s+href="(?P<url>https?://[^"]+)"[^>]*>(?P<title>[^<]+)</a>',
    re.IGNORECASE,
)
AGE_RE = re.compile(
    r'<span\s+class="age"[^>]*><a\s+href="item\?id=(?P<id>\d+)"[^>]*>'
    r'(?P<age_text>[^<]+)</a></span>',
    re.IGNORECASE,
)
ID_RE = re.compile(r'<tr\s+class="athing"\s+id="(?P<id>\d+)"', re.IGNORECASE)


def _age_text_to_days(text: str) -> Optional[int]:
    """Parse HN's age text ('3 hours ago', '2 days ago', '1 month ago')."""
    if not text:
        return None
    t = text.strip().lower()
    m = re.match(r"^(\d+)\s+(minute|hour|day|month|year)s?\s+ago$", t)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "minute":
        return 0
    if unit == "hour":
        return 0
    if unit == "day":
        return n
    if unit == "month":
        return n * 30
    if unit == "year":
        return n * 365
    return None


def _get_html(url: str) -> Optional[str]:
    """GET the HN /jobs page. Returns the HTML body or None on failure.

    HN rate-limits aggressively (HTTP 429) when too many requests come
    from the same IP. We honour Retry-After and back off with a short
    sleep + one retry before giving up — the cron schedule is generous
    (3×/week) so a single drop is acceptable.
    """
    import time
    headers = {
        "User-Agent": "lovework-agent/1.0 (+https://lovework.be)",
        "Accept": "text/html,application/xhtml+xml",
    }
    for attempt in range(2):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=HN_REQUEST_TIMEOUT) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                retry_after = e.headers.get("Retry-After", "5")
                try:
                    sleep_s = min(int(retry_after), 30)
                except ValueError:
                    sleep_s = 5
                logger.warning(f"[hn_jobs] 429 rate-limited; sleeping {sleep_s}s")
                time.sleep(sleep_s)
                continue
            logger.debug(f"HN /jobs GET failed: {e}")
            return None
        except (urllib.error.URLError, TimeoutError) as e:
            logger.debug(f"HN /jobs GET failed: {e}")
            return None
    return None


def _parse_jobs_html(html: str) -> List[dict]:
    """Extract {hn_id, title, url, age_text, age_days} tuples from /jobs HTML."""
    out: List[dict] = []
    # Each row is "id=... title-link age-link" — split by <tr class="athing">.
    rows = re.split(r'<tr\s+class="athing"\s+id="', html)
    for chunk in rows[1:]:
        head, _, rest = chunk.partition("</tr>")
        id_match = re.match(r'(?P<id>\d+)"', head)
        if not id_match:
            continue
        hn_id = id_match.group("id")
        block = head + rest
        link_match = TITLE_LINK_RE.search(block)
        if not link_match:
            continue
        title = re.sub(r"\s+", " ", link_match.group("title")).strip()
        url = link_match.group("url").strip()
        if not title or not url:
            continue
        age_match = AGE_RE.search(block)
        age_text = age_match.group("age_text") if age_match else ""
        age_days = _age_text_to_days(age_text) if age_match else None
        out.append({
            "hn_id": hn_id,
            "title": title,
            "url": url,
            "age_text": age_text,
            "age_days": age_days,
        })
    # De-dup by URL (HN sometimes lists the same job twice in adjacent pages).
    seen = set()
    deduped: List[dict] = []
    for r in out:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        deduped.append(r)
    return deduped


# ── Terse-title heuristics: company name + role + flags ───────────────────

# Common HN /jobs title forms:
#   "Great Question (YC W21) Is Hiring Applied AI Interns (ycombinator.com)"
#   "Stripe Is Hiring Software Engineers, ML Platform"
#   "Anthropic | AI Research Engineer | Remote"
HIRING_KEYWORDS = (" is hiring ", " hiring ", " | ", " (yc ", " (ycombinator")
YC_BATCH_RE = re.compile(r"\((?:YC\s*)?[SW]\d{2}\)", re.IGNORECASE)


def _split_title_to_org_role(title: str) -> tuple[str, str]:
    """Best-effort split of a terse /jobs title into (org, role).

    Returns (raw_title, raw_title) if it can't be parsed cleanly — the
    matcher will see the full title and figure it out.
    """
    t = title.strip()
    # Strip trailing "(ycombinator.com)" or similar domain hints.
    t = re.sub(r"\s*\([a-z0-9.-]+\.[a-z]{2,}\)\s*$", "", t, flags=re.IGNORECASE)
    # "X is hiring Y" form.
    m = re.match(r"^(?P<org>.+?)\s+(?:is\s+)?hiring\s+(?P<role>.+)$", t, re.IGNORECASE)
    if m:
        return m.group("org").strip(), m.group("role").strip()
    # "X | Y" form (already split-friendly).
    if " | " in t:
        org, _, role = t.partition(" | ")
        return org.strip(), role.strip()
    return t, t


# ── Source ────────────────────────────────────────────────────────────────

class HNHiringJobsSource:
    """Reads the live HN /jobs page and surfaces matching listings."""

    name = "hn_jobs"

    def __init__(self, crawler=None, matcher: Optional[JobMatcher] = None,
                 registry: Optional[JobRegistry] = None):
        # `crawler` accepted for interface uniformity; we use direct HTTP
        # for /jobs and a cheap description grab for the linked page.
        self.matcher = matcher
        self.registry = registry

    def run(self) -> List[WikiEntry]:
        entries: List[WikiEntry] = []
        html = _get_html(JOBS_PAGE_URL)
        if not html:
            logger.warning(f"[{self.name}] {JOBS_PAGE_URL} returned no HTML")
            return entries

        rows = _parse_jobs_html(html)
        logger.info(f"[{self.name}] Parsed {len(rows)} /jobs listings (cap {MAX_LISTINGS})")

        for row in rows[:MAX_LISTINGS]:
            age_days = row.get("age_days")
            if age_days is not None and age_days > MAX_AGE_DAYS:
                continue  # Skip stale jobs — LJ explicitly wants to avoid them.
            entry = self._make_entry(row)
            if entry is not None:
                entries.append(entry)

        logger.info(f"[{self.name}] {len(entries)} entries after parse + match")
        return entries

    def _make_entry(self, row: dict) -> Optional[WikiEntry]:
        if self.matcher is None:
            return None
        title_full = row["title"]
        org, role = _split_title_to_org_role(title_full)
        url = row["url"]
        age_text = row.get("age_text", "")
        age_days = row.get("age_days")
        hn_id = row.get("hn_id", "")

        # Cheap description grab — a snippet of the linked page's <title> +
        # <meta description> when available. We don't Firecrawl here because
        # most /jobs links go to YC company pages or ATS sites that the
        # SmartCrawler would later re-visit anyway. The work-auth hard-kill
        # just needs the text scan; the LLM can score on terse + age.
        description = title_full
        if FETCH_DETAIL_PAGES:
            page_snippet = _fetch_meta_description(url)
            if page_snippet:
                description = f"{title_full}\n\n{page_snippet}"

        match = self.matcher.match(
            role, description, org,
            job_url=url, location="",
        )

        comment_url = f"{HN_WEB_BASE}/item?id={hn_id}" if hn_id else ""
        discovery_date = (
            (date.today() - timedelta(days=age_days)).isoformat()
            if age_days is not None else ""
        )
        record = None
        if self.registry is not None:
            try:
                record = self.registry.upsert(
                    org=org, title=role, url=url,
                    careers_url=comment_url, source=self.name,
                    discovery_url=comment_url,
                    discovery_date=discovery_date,
                )
            except Exception as e:
                logger.debug(f"[{self.name}] Registry upsert failed: {e}")

        # Use the original terse title in the wiki so the reader sees the
        # actual HN posting, not our best-effort split.
        entry = WikiEntry(
            org_name=org,
            title=title_full,
            url=url,
            location=None,
            score=match.score,
            decision=match.decision,
            reasoning=match.reasoning,
            source=self.name,
            discovery_url=comment_url,
            discovery_date=discovery_date,
            **match_fields(match),
        )
        if record is not None:
            entry.lifecycle_status = record.status
            entry.first_seen = record.first_seen
        # Add the age text to the reasoning so the wiki reader sees it.
        if age_text:
            entry.reasoning = f"[{age_text}] {entry.reasoning}"
        return entry


# ── Cheap page-snippet helper (no Firecrawl) ──────────────────────────────

_META_DESC_RE = re.compile(
    r'<meta\s+name="description"\s+content="(?P<desc>[^"]+)"',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title>(?P<t>[^<]+)</title>", re.IGNORECASE)


def _fetch_meta_description(url: str) -> str:
    """Grab <title> + <meta description> from `url` (best-effort, no JS).

    Used to add a few sentences of context to /jobs listings whose HN title
    is just "X is hiring Y". Returns "" on any failure.
    """
    req = urllib.request.Request(
        url, headers={"User-Agent": "lovework-agent/1.0 (+https://lovework.be)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=HN_REQUEST_TIMEOUT) as resp:
            html = resp.read(200_000).decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return ""
    title_m = _TITLE_RE.search(html)
    desc_m = _META_DESC_RE.search(html)
    title = title_m.group("t").strip() if title_m else ""
    desc = desc_m.group("desc").strip() if desc_m else ""
    bits = [b for b in (title, desc) if b]
    return " — ".join(bits)
