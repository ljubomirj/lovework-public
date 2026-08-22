"""
Source: LinkedIn related-ads.

A specialised source that complements `gmail_lj_jobs`. LJ's flow:

  1. Gmail LinkedIn alert arrives with N jobs.
  2. The principal opens the alert's "search" URL in LinkedIn.
  3. LinkedIn shows the matched jobs PLUS related / similar jobs at the
     bottom or side (LinkedIn's recommendation engine).
  4. Those related jobs are *fresher* than the original email — they're
     newly posted but LinkedIn deems them similar to what LJ is looking
     for. LJ wants to harvest them as additional leads.

The cron path for this is non-trivial because LinkedIn requires login for
search results. We support two paths:

  - **Seeds file** (`profiles/lj/linkedin_seeds.md`): LJ drops a LinkedIn
    search/job URL in here when he wants the agent to follow it. The
    source fetches the page, harvests all `linkedin.com/jobs/view/...`
    URLs from the HTML, and feeds them through the matcher. If LinkedIn
    returns a login wall (HTTP 999 or the page is mostly auth markup),
    the source logs the URL to `profiles/lj/linkedin_needs_auth.md` and
    moves on — no crash, no retry storm.

  - **Gmail-extracted seeds** (default-off): the gmail_lj_jobs source
    captures the alert's canonical search URL into
    `profiles/lj/linkedin_seeds.md` after each run, so the next run can
    pick them up. This is opt-in via env var.

The output is WikiEntry rows for each harvested related job, registered
and matched like any other source. Stale or already-seen jobs are dedup'd
by the registry hash.

No Firecrawl — LinkedIn HTML is fetched with urllib (it's a single page
per seed; cookies are out of scope for v1).
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from job_registry import JobRegistry
from matcher import JobMatcher
from wiki_store import WikiEntry, match_fields

import config
from .hn_common import HN_REQUEST_TIMEOUT  # re-use the same timeout

logger = logging.getLogger(__name__)

SEEDS_FILENAME = "linkedin_seeds.md"
NEEDS_AUTH_FILENAME = "linkedin_needs_auth.md"

# LinkedIn job-view URL pattern. Captures the view ID we'll use as the
# registry hash's url component.
LI_JOB_URL_RE = re.compile(
    r'https?://(?:www\.)?linkedin\.com/(?:comm/)?jobs/view/(\d+)',
    re.IGNORECASE,
)
# LinkedIn auth wall indicators (HTML title or known redirect markers).
AUTH_MARKERS = (
    "authwall",
    "sign in to linkedin",
    "join linkedin",
    "linkedin login",
)

# How many seeds to process per run (cost bound).
MAX_SEEDS_PER_RUN = int(os.getenv("LOVEWORK_LI_SEEDS_MAX", "10"))
# How many related-job URLs to harvest per seed.
MAX_RELATED_PER_SEED = int(os.getenv("LOVEWORK_LI_RELATED_MAX", "25"))
# Whether to capture gmail-extracted LinkedIn search URLs as new seeds.
CAPTURE_GMAIL_SEEDS = os.getenv("LOVEWORK_LI_CAPTURE_GMAIL_SEEDS", "1") != "0"


# ── Profile files ─────────────────────────────────────────────────────────

def _profile_dir() -> Path:
    return config.PROFILES_DIR / "lj"


def _seeds_path() -> Path:
    return _profile_dir() / SEEDS_FILENAME


def _needs_auth_path() -> Path:
    return _profile_dir() / NEEDS_AUTH_FILENAME


def _read_seeds(seeds_path: Optional[Path] = None) -> List[str]:
    """Return the list of seed URLs to visit this run, oldest first."""
    p = seeds_path or _seeds_path()
    if not p.exists():
        return []
    out: List[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _write_seeds(urls: List[str], seeds_path: Optional[Path] = None) -> None:
    p = seeds_path or _seeds_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "# LinkedIn seeds for the related-ads source\n"
        "# One URL per line. Lines starting with # are ignored.\n"
        "# The source appends discovered search URLs here from Gmail alerts\n"
        "# and consumes the oldest unprocessed ones each run.\n\n"
        + "\n".join(urls) + "\n"
    )
    p.write_text(body, encoding="utf-8")


def _append_needs_auth(url: str, reason: str, needs_auth_path: Optional[Path] = None) -> None:
    p = needs_auth_path or _needs_auth_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = f"- {datetime.now().strftime('%Y-%m-%d')} {url}  # {reason}\n"
    if not p.exists():
        p.write_text(
            "# LinkedIn URLs that need manual auth to harvest\n"
            "# (the cron source logged these as auth-walled; open them in a browser).\n\n"
            + line,
            encoding="utf-8",
        )
    else:
        with p.open("a", encoding="utf-8") as f:
            f.write(line)


def append_seed(url: str, seeds_path: Optional[Path] = None) -> None:
    """Public helper: append a URL to the seeds file if not already present."""
    if not url or not url.startswith("http"):
        return
    seeds = _read_seeds(seeds_path)
    if url in seeds:
        return
    seeds.append(url)
    _write_seeds(seeds, seeds_path)


# ── HTTP + parse ─────────────────────────────────────────────────────────

def _fetch_html(url: str) -> Optional[str]:
    req = urllib.request.Request(
        url, headers={"User-Agent": "lovework-agent/1.0 (+https://lovework.be)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=HN_REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        logger.debug(f"LinkedIn fetch failed for {url}: {e}")
        return None


def _looks_like_auth_wall(html: str) -> bool:
    """Cheap check: is this an auth wall? Title + first KB scan."""
    head = (html or "")[:4096].lower()
    return any(m in head for m in AUTH_MARKERS)


def _harvest_job_urls(html: str) -> List[str]:
    """Pull all linkedin.com/jobs/view/<id> URLs from the page HTML.

    Dedup by view ID (LinkedIn sometimes emits both /comm/jobs/view and
    /jobs/view for the same role). Returned URLs use the canonical
    /jobs/view/ form so the registry hash is stable.
    """
    seen = set()
    out: List[str] = []
    for m in LI_JOB_URL_RE.finditer(html or ""):
        view_id = m.group(1)
        if view_id in seen:
            continue
        seen.add(view_id)
        out.append(f"https://www.linkedin.com/jobs/view/{view_id}")
    return out


# ── Best-effort title/company extraction from the HTML ───────────────────

# LinkedIn SERP cards embed JSON-LD with the JobPosting schema. We try
# that first (clean), then fall back to a regex on the page <title>.
JSONLD_RE = re.compile(
    r'<script\s+type="application/ld\+json"[^>]*>(?P<json>.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
TITLE_RE = re.compile(r"<title>(?P<t>[^<]+)</title>", re.IGNORECASE)


def _parse_jobposting(html: str, url: str) -> tuple[str, str]:
    """Return (title, company) for a LinkedIn job page.

    Best-effort: LinkedIn's HTML is heavily obfuscated, so we accept
    whatever we can get. On failure, return the URL slug as the title
    and "LinkedIn" as the company — the matcher still scores it.
    """
    # 1) JSON-LD JobPosting
    for m in JSONLD_RE.finditer(html or ""):
        try:
            data = json.loads(m.group("json"))
        except (json.JSONDecodeError, ValueError):
            continue
        # JSON-LD can be a list, a single object, or @graph-wrapped.
        json_records = data if isinstance(data, list) else [data]
        for json_record in json_records:
            if isinstance(json_record, dict):
                if "@graph" in json_record and isinstance(json_record["@graph"], list):
                    json_records.extend(json_record["@graph"])
        for json_record in json_records:
            if not isinstance(json_record, dict):
                continue
            t = json_record.get("@type", "")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                title = (json_record.get("title") or "").strip()
                org = json_record.get("hiringOrganization") or {}
                if isinstance(org, dict):
                    company = (org.get("name") or "").strip()
                else:
                    company = str(org).strip()
                if title and company:
                    return title, company
    # 2) <title> fallback: "Role Company | LinkedIn"
    title_match = TITLE_RE.search(html or "")
    if title_match:
        t = title_match.group("t").strip()
        if " | LinkedIn" in t:
            t = t.replace(" | LinkedIn", "")
        # Heuristic: "Senior Engineer at Acme" or "Senior Engineer, Acme"
        for sep in (" at ", " - ", " — ", ", "):
            if sep in t:
                role, _, company = t.partition(sep)
                return role.strip(), company.strip()
        return t, "LinkedIn"
    # 3) URL slug fallback.
    m = re.search(r"/jobs/view/(\d+)", url)
    slug = m.group(1) if m else url
    return f"LinkedIn Job {slug}", "LinkedIn"


# ── Source ────────────────────────────────────────────────────────────────

class LinkedInRelatedSource:
    """Follows LJ-maintained LinkedIn seeds and harvests related jobs."""

    name = "linkedin_related"

    def __init__(self, crawler=None, matcher: Optional[JobMatcher] = None,
                 registry: Optional[JobRegistry] = None,
                 sources_dir: Optional[Path] = None):
        # `crawler` accepted for interface uniformity; we use direct HTTP.
        self.matcher = matcher
        self.registry = registry
        self.sources_dir = sources_dir

    @property
    def seeds_path(self) -> Optional[Path]:
        if self.sources_dir is None:
            return None
        return self.sources_dir / SEEDS_FILENAME

    @property
    def needs_auth_path(self) -> Optional[Path]:
        if self.sources_dir is None:
            return None
        return self.sources_dir / NEEDS_AUTH_FILENAME

    def run(self) -> List[WikiEntry]:
        seeds = _read_seeds(self.seeds_path)
        if not seeds:
            seed_path = self.seeds_path or _seeds_path()
            logger.info(f"[{self.name}] No seeds in {seed_path}; skipping.")
            return []

        # Only process the oldest N seeds this run.
        to_process = seeds[:MAX_SEEDS_PER_RUN]
        remaining = seeds[MAX_SEEDS_PER_RUN:]
        logger.info(f"[{self.name}] Processing {len(to_process)} seeds "
                    f"({len(remaining)} deferred)")

        entries: List[WikiEntry] = []
        auth_walled: List[str] = []
        seen_urls: set[str] = set()

        for seed_url in to_process:
            html = _fetch_html(seed_url)
            if html is None:
                # Network error — leave in seeds for next run.
                remaining.insert(0, seed_url)
                continue
            if _looks_like_auth_wall(html):
                auth_walled.append(seed_url)
                _append_needs_auth(seed_url, "auth wall detected", self.needs_auth_path)
                continue

            job_urls = _harvest_job_urls(html)[:MAX_RELATED_PER_SEED]
            for job_url in job_urls:
                if job_url in seen_urls:
                    continue
                seen_urls.add(job_url)
                entry = self._harvest_one(job_url)
                if entry is not None:
                    entries.append(entry)

        # Persist: keep the un-processed + auth-walled URLs for the next run.
        # Auth-walled are appended at the end so they get retried later
        # (LJ may add a cookie or change approach).
        next_seeds = remaining + auth_walled
        _write_seeds(next_seeds, self.seeds_path)

        logger.info(f"[{self.name}] {len(entries)} entries; {len(auth_walled)} auth-walled")
        return entries

    def _harvest_one(self, job_url: str) -> Optional[WikiEntry]:
        if self.matcher is None:
            return None
        html = _fetch_html(job_url)
        if html is None:
            return None
        # The individual job view page can also be auth-walled.
        if _looks_like_auth_wall(html):
            return None
        title, company = _parse_jobposting(html, job_url)
        if not title or not company:
            return None
        # Description = the <title> + JSON-LD description if we can grab it.
        description = ""
        for m in JSONLD_RE.finditer(html or ""):
            try:
                data = json.loads(m.group("json"))
            except (json.JSONDecodeError, ValueError):
                continue
            json_records = data if isinstance(data, list) else [data]
            for json_record in json_records:
                if isinstance(json_record, dict) and "@graph" in json_record:
                    json_records.extend(json_record["@graph"])
            for json_record in json_records:
                if not isinstance(json_record, dict):
                    continue
                t = json_record.get("@type", "")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    description = (json_record.get("description") or "").strip()
                    break
            if description:
                break
        if not description:
            title_match = TITLE_RE.search(html or "")
            description = title_match.group("t") if title_match else title

        record = None
        if self.registry is not None:
            try:
                record = self.registry.upsert(
                    org=company, title=title, url=job_url,
                    careers_url=job_url, source=self.name,
                )
            except Exception as e:
                logger.debug(f"[{self.name}] Registry upsert failed: {e}")

        match = self.matcher.match(
            title, description, company,
            job_url=job_url, location="",
        )
        entry = WikiEntry(
            org_name=company, title=title, url=job_url,
            location=None,
            score=match.score, decision=match.decision,
            reasoning=match.reasoning, source=self.name,
            advert_excerpt=description,
            **match_fields(match),
        )
        if record is not None:
            entry.lifecycle_status = record.status
            entry.first_seen = record.first_seen
        return entry
