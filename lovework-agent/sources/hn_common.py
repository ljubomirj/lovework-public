"""
Shared HTTP + parsing helpers for the Hacker News sources.

The HN Algolia API (https://hn.algolia.com/api) is the supported read-only
interface — no auth, generous rate limits, returns clean JSON. The "Who is
hiring?" threads are first-class items with stable `objectID`s, and the
threaded comments carry the actual job posts (one comment per role).

The two sources (hn_hiring + hn_jobs) share:
  - HN client: GET the item, walk `kids` for top-level comments
  - Comment parser: split a comment into (company, role, location, url, body)
  - Date helpers: monthly thread discovery via search-by-query

Hacker News related-jobs helpers used by both live sources.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from lead_identity import is_implausible_header

logger = logging.getLogger(__name__)

# HN Algolia API base — no auth, no rate limit issues for our use.
# Used for search and for fetching most items.
HN_API_BASE = "https://hn.algolia.com/api/v1"
# Firebase API — used as a fallback for items whose `kids` are missing
# from the Algolia DB. The June 2026 hiring thread had 363 kids on
# Firebase but 0 on Algolia — this happens when the Algolia index lags
# behind. We try Algolia first, then Firebase, on the same object_id.
HN_FIREBASE_BASE = "https://hacker-news.firebaseio.com/v0"
HN_WEB_BASE = "https://news.ycombinator.com"

# Be polite — single-request bursts only. Tunable via env for tests.
HN_REQUEST_TIMEOUT = float(os.getenv("LOVEWORK_HN_TIMEOUT", "15"))


# ── Low-level HTTP ────────────────────────────────────────────────────────

def _get_json(url: str) -> Optional[dict | list]:
    """GET `url`, return parsed JSON or None on any failure (logged)."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "lovework-agent/1.0 (+https://lovework.be)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HN_REQUEST_TIMEOUT) as resp:
            data = resp.read()
        return json.loads(data)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
        logger.debug(f"HN GET {url} failed: {e}")
        return None


# ── Monthly "Ask HN: Who is hiring?" thread discovery ──────────────────────

HIRING_THREAD_TITLE_RE = re.compile(
    r"^Ask HN:\s*Who is hiring\??\s*\((?P<month>[A-Za-z]+)\s+(?P<year>\d{4})\)\s*$",
    re.IGNORECASE,
)


def parse_hiring_thread_title(title: str) -> Optional[Tuple[str, int]]:
    """Extract (month_name, year) from an "Ask HN: Who is hiring? (Month Year)" title.

    Returns None if the title doesn't match.
    """
    if not title:
        return None
    m = HIRING_THREAD_TITLE_RE.match(title.strip())
    if not m:
        return None
    return m.group("month"), int(m.group("year"))


def find_latest_hiring_thread_id(
    *,
    prefer_year: Optional[int] = None,
    prefer_month: Optional[str] = None,
) -> Optional[int]:
    """Find the most recent "Ask HN: Who is hiring?" thread ID.

    Strategy: search the Algolia search endpoint with a query that matches
    the canonical title, sorted by date descending. The first hit that
    matches the title regex is the current month's thread. If
    `prefer_*` are set, the search is biased to that month/year (e.g. when
    the LJ pipeline runs on the 2nd and the new thread hasn't been
    indexed yet, the previous month is still wanted).

    The HN Algolia search is robust for this — "Ask HN Who is hiring" is
    a phrase that surfaces monthly threads consistently.
    """
    # Sort by date descending (newest first); HN's default is relevance.
    params = {
        "query": "Ask HN Who is hiring",
        "tags": "story",
        "hitsPerPage": 30,
        # "search_by_date" sorts by created_at_i desc — that's what we want.
        # Without this, relevance ordering surfaces older classic threads.
    }
    data = _get_json(f"{HN_API_BASE}/search_by_date?{urllib.parse.urlencode(params)}")
    if not data or not isinstance(data, dict):
        # Fallback to the default search endpoint.
        data = _get_json(f"{HN_API_BASE}/search?{urllib.parse.urlencode(params)}")
    if not data or not isinstance(data, dict):
        return None
    hits = data.get("hits", [])
    if not hits:
        return None

    month_order = {m.lower(): i for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"]
    )}

    for h in hits:
        title = h.get("title", "") or ""
        parsed = parse_hiring_thread_title(title)
        if not parsed:
            continue
        month, year = parsed
        if prefer_year and year != prefer_year:
            continue
        if prefer_month and month.lower() != prefer_month.lower():
            continue
        oid = h.get("objectID")
        if not oid:
            continue
        return int(oid)
    return None


# ── Item fetch (with kid walking) ─────────────────────────────────────────

def fetch_item(object_id: int) -> Optional[dict]:
    """Fetch a single HN item by id. Tries Algolia first, then Firebase.

    The Algolia API is faster and is the canonical search backend, but
    it doesn't always carry the `kids` list for older / not-yet-indexed
    items. The Firebase API is the source of truth for live data. We
    fall back automatically when Algolia returns a thread with no kids.
    """
    item = _get_json(f"{HN_API_BASE}/items/{object_id}")
    if item is None:
        item = _get_json(f"{HN_FIREBASE_BASE}/item/{object_id}.json")
    elif not item.get("kids") and not item.get("children"):
        # Algolia may have indexed the item but without kids (the June
        # 2026 hiring thread had 363 kids on Firebase but 0 on Algolia).
        # The Firebase item is the source of truth for live data — try
        # to upgrade. The OP text doesn't matter for thread fetching
        # since we only need the kids list.
        fb = _get_json(f"{HN_FIREBASE_BASE}/item/{object_id}.json")
        if fb is not None and (fb.get("kids") or fb.get("text")):
            item = fb
    return item


def fetch_thread_comments(object_id: int, *, max_kids: int = 600) -> List[dict]:
    """Fetch top-level comments of a story by ID.

    Returns a list of comment dicts. Limit caps how many we walk; the June
    2026 thread had ~363 top-level comments and the source already caps at
    200-300 for cost. Caller should pass a smaller cap if they want fewer.

    Two representations of the comment tree can come back:
      - Firebase: a flat `kids` list of top-level comment IDs. Each comment
        is fetched individually.
      - Algolia: a `children` tree where each node carries its own `text`
        and `children`. Top-level nodes are the ones whose `story_id` (or
        `parent_id`) equals the thread id.

    We accept either and normalise to a flat list of (text-bearing) comment
    dicts with `objectID` / `id` set.
    """
    item = fetch_item(object_id)
    if not item:
        return []
    parent_id = int(item.get("objectID") or item.get("id") or object_id)
    comments: List[dict] = []

    # Firebase-style: flat kids list of comment IDs.
    kids = item.get("kids") or []
    if kids:
        for kid_id in kids[:max_kids]:
            c = fetch_item(int(kid_id))
            if not c:
                continue
            c_parent = c.get("parent_id") or c.get("parent")
            if c_parent and int(c_parent) != parent_id:
                continue  # nested reply
            if c.get("text"):
                comments.append(c)
        return comments[:max_kids]

    # Algolia-style: nested children tree. Walk it breadth-first and keep
    # the top-level (story_id == parent_id) nodes.
    queue: List[dict] = list(item.get("children") or [])
    while queue and len(comments) < max_kids:
        node = queue.pop(0)
        node_id = node.get("id") or node.get("objectID")
        node_parent = node.get("parent_id") or node.get("parent") or node.get("story_id")
        if node_parent and int(node_parent) == parent_id and node.get("text"):
            comments.append(node)
        # Always descend to find more top-level comments in deeper branches.
        for child in node.get("children") or []:
            queue.append(child)
    return comments


# ── Comment parser (Hacker News job-post format) ──────────────────────────

# Canonical HN hiring thread format:
#   Company | Role | Location | ...
#   <one or more paragraphs of description>
# Example:
#   Anthropic | AI Research Engineer | San Francisco / Remote
#   We are looking for an AI research engineer to work on...
#
# Some comments put the fields on a second line or use "Company – Role – Loc".
# We try a few patterns and accept the first that yields >= 2 non-empty parts.

SEPARATOR_RE = re.compile(r"\s+\|\s+|\s+–\s+|\s+—\s+")


def _split_header(line: str) -> Optional[List[str]]:
    """Split a header line into [company, role, location?, url?, ...]."""
    parts = [p.strip() for p in SEPARATOR_RE.split(line) if p.strip()]
    if len(parts) < 2:
        return None
    return parts


def _find_url_in_text(text: str) -> Optional[str]:
    """Pick the first http(s) URL found in `text` (apply or company site)."""
    m = re.search(r"https?://[^\s)\]>]+", text)
    return m.group(0).rstrip(".,;") if m else None


def _comment_date(comment: dict) -> str:
    """Return an HN comment's source date as YYYY-MM-DD when available."""
    raw = comment.get("created_at") or comment.get("created_at_i") or comment.get("time")
    if raw in (None, ""):
        return ""
    if isinstance(raw, (int, float)) or str(raw).isdigit():
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).date().isoformat()
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def parse_hn_job_comment(comment: dict) -> Optional[dict]:
    """Parse a single HN job-post comment into {company, role, location, url, body}.

    Returns None if the comment doesn't look like a job post (no recognisable
    header line, or it looks like a moderation/reply/meta comment).
    """
    text = (comment.get("text") or "").strip()
    if not text:
        return None

    # HTML <p>...</p> blocks. HN Algolia returns HTML; first paragraph is the header.
    # Strip the surrounding tags but keep the line structure.
    text = re.sub(r"</?p\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    # HTML entities: minimal set we hit.
    text = (
        text.replace("&#x2F;", "/")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
    )

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None

    # Skip pure moderation/meta lines at the start (rare but possible).
    skip_phrases = (
        "i am hiring",
        "my company",
        "throwaway account",
        "remote-only",
        "this thread",
    )

    header = None
    body_start = 1
    for i, line in enumerate(lines[:3]):
        if any(line.lower().startswith(p) for p in skip_phrases):
            continue
        parts = _split_header(line)
        if parts and len(parts) >= 2:
            header = parts
            body_start = i + 1
            break

    if not header:
        return None

    company = header[0]
    role = header[1] if len(header) > 1 else ""
    location = header[2] if len(header) > 2 else ""

    # P2 — identity integrity: a long prose sentence is a mis-split
    # description (e.g. an em-dash inside a paragraph), not a header.
    # Drop it so a description never surfaces as company/role.
    if not company or not role or is_implausible_header(company, role):
        return None

    # URL is usually inline in header[3] or in the body.
    url = None
    for part in header[3:]:
        if part.startswith("http"):
            url = part
            break
    if not url:
        url = _find_url_in_text("\n".join(lines[body_start:]))

    body = "\n".join(lines[body_start:]).strip()

    return {
        "company": company,
        "role": role,
        "location": location or "",
        "url": url or "",
        "body": body,
        # Algolia calls these objectID/author/created_at; Firebase calls them
        # id/by/time. Normalise both so provenance survives either API path.
        "hn_comment_id": comment.get("objectID") or comment.get("id"),
        "hn_author": comment.get("author") or comment.get("by", ""),
        "discovery_date": _comment_date(comment),
    }


# ── "Who wants to be hired?" thread discovery (companion monthly thread) ──

SEEKING_THREAD_TITLE_RE = re.compile(
    r"^Who wants to be hired\??\s*\((?P<month>[A-Za-z]+)\s+(?P<year>\d{4})\)\s*$",
    re.IGNORECASE,
)


def find_latest_seeking_thread_id(**kwargs) -> Optional[int]:
    """Companion to find_latest_hiring_thread_id — the monthly "Who wants
    to be hired?" thread where people post their own résumés."""
    params = {
        "query": "Who wants to be hired",
        "tags": "story",
        "hitsPerPage": 30,
    }
    data = _get_json(f"{HN_API_BASE}/search_by_date?{urllib.parse.urlencode(params)}")
    if not data or not isinstance(data, dict):
        data = _get_json(f"{HN_API_BASE}/search?{urllib.parse.urlencode(params)}")
    if not data or not isinstance(data, dict):
        return None
    hits = data.get("hits", [])
    if not hits:
        return None

    prefer_year = kwargs.get("prefer_year")
    prefer_month = kwargs.get("prefer_month")

    for h in hits:
        title = h.get("title", "") or ""
        m = SEEKING_THREAD_TITLE_RE.match(title.strip())
        if not m:
            continue
        month, year = m.group("month"), int(m.group("year"))
        if prefer_year and year != prefer_year:
            continue
        if prefer_month and month.lower() != prefer_month.lower():
            continue
        oid = h.get("objectID")
        if not oid:
            continue
        return int(oid)
    return None
