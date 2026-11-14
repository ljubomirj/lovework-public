"""
Source: CompBioJobs (https://www.compbiojobs.com) — a curated job board for
bioinformatics, computational biology and cheminformatics roles in pharma and
biotech.

Why this source exists: KJ's own ads kept arriving *through LinkedIn* with a
compbiojobs.com link (e.g. GSK "Principle Scientist — Cheminformatics",
2026-09-11), i.e. we were reading the board by accident and only when someone
else republished it. The board itself carries a `/jobs/cheminformatics`
category — "Cheminformatics & Computational Chemistry Jobs" — which is exactly
KJ's profession, and it is not in any of his sources.

How it is read (politely, and within their robots.txt):
  * `GET /jobs/<category>` once per configured category (default:
    `cheminformatics`, `ai-drug-discovery`) to collect job URLs;
  * `GET /jobs/<id>/<slug>` per job, capped, with a delay between requests, to
    read the page's **schema.org JobPosting JSON-LD** — the structured block
    that carries the real `datePosted`, `hiringOrganization`, `jobLocation` and
    the description;
  * never `/search`, `?q=`, `/api/` or `/admin/` (all disallowed for
    `User-agent: *`).

Tier and scope: this is a *curated aggregator* (their own listing says
"Source: manual" — a human vets each ad), so `primary = False`: the hiring
party's own publication is the primary evidence. `complete_scope = False`:
two categories are not the whole market, so absence must never become
`not_listed_in_scope`.

An ad the board marks "no longer listed" is skipped — the board is telling us
the role closed, and a closed advert is not a lead (the registry would only
record it as `disappeared` a week later anyway).
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date
from html import unescape
from typing import List, Optional

from wiki_store import WikiEntry, match_fields

from .neolabs import NeolabsSource

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("LOVEWORK_COMPBIOJOBS_BASE", "https://www.compbiojobs.com")
#: Categories to read. Both are KJ's profession; `machine-learning` and
#: `computational-biology` exist too but are broad and would flood the matcher.
CATEGORIES = [
    c.strip() for c in os.getenv(
        "LOVEWORK_COMPBIOJOBS_CATEGORIES",
        "cheminformatics,ai-drug-discovery",
    ).split(",") if c.strip()
]
MAX_PER_CATEGORY = int(os.getenv("LOVEWORK_COMPBIOJOBS_MAX_PER_CATEGORY", "25"))
REQUEST_TIMEOUT = float(os.getenv("LOVEWORK_COMPBIOJOBS_TIMEOUT", "25"))
REQUEST_DELAY = float(os.getenv("LOVEWORK_COMPBIOJOBS_REQUEST_DELAY", "1.0"))
MAX_DESCRIPTION_CHARS = int(os.getenv("LOVEWORK_COMPBIOJOBS_MAX_DESCRIPTION", "8000"))

USER_AGENT = os.getenv(
    "LOVEWORK_COMPBIOJOBS_UA",
    "LoveWork/1.0 (personal job agent; contact user@example.invalid)",
)

JOB_HREF_RE = re.compile(r'href="(/jobs/(\d+)/([a-z0-9-]+))"')
LD_JSON_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)
CLOSED_MARKERS = ("no longer listed on CompBioJobs", "This listing is no longer active")


def _get(url: str) -> Optional[str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        logger.warning("[compbiojobs] GET %s failed (%s): %s", url, type(exc).__name__, exc)
        return None


def _job_urls(html: str) -> List[str]:
    """Job URLs on a category page, in page order, deduped."""
    seen, out = set(), []
    for path, _id, _slug in JOB_HREF_RE.findall(html):
        if path in seen:
            continue
        seen.add(path)
        out.append(f"{BASE_URL}{path}")
    return out


def _job_posting(html: str) -> Optional[dict]:
    """The schema.org JobPosting block on a job page, if present."""
    for block in LD_JSON_RE.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


def _location_of(posting: dict) -> str:
    loc = posting.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if not isinstance(loc, dict):
        return ""
    addr = loc.get("address") or {}
    if isinstance(addr, str):
        return addr
    parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
    if isinstance(parts[2], dict):
        parts[2] = parts[2].get("name") or parts[2].get("addressCountry")
    return ", ".join(str(p) for p in parts if p)


def _org_of(posting: dict) -> str:
    org = posting.get("hiringOrganization") or {}
    if isinstance(org, dict):
        return unescape(str(org.get("name") or "")).strip()
    return ""


def _posted_at(posting: dict) -> str:
    raw = str(posting.get("datePosted") or "")
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else ""


class CompBioJobsSource(NeolabsSource):
    """Curated bioinformatics/cheminformatics board — KJ's profession."""

    primary = False
    complete_scope = False
    name = "compbiojobs"

    goal = (
        "Find open cheminformatics / computational-chemistry / AI-for-drug-discovery "
        "positions. Look for: cheminformatics or computational-chemistry scientist or "
        "engineer, CADD, molecular modelling, ML for molecular design, scientific "
        "software engineer, or similar."
    )

    # ── the crawl ─────────────────────────────────────────────────────────
    def run(self) -> List[WikiEntry]:
        entries: List[WikiEntry] = []
        seen_ids: set[str] = set()

        for category in CATEGORIES:
            url = f"{BASE_URL}/jobs/{category}"
            html = _get(url)
            if not html:
                continue
            job_urls = _job_urls(html)[:MAX_PER_CATEGORY]
            logger.info("[compbiojobs] %s: %d job links (cap %d)", category, len(job_urls), MAX_PER_CATEGORY)

            for job_url in job_urls:
                job_id = job_url.rstrip("/").split("/")[-2]
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                page = _get(job_url)
                time.sleep(REQUEST_DELAY)
                if not page:
                    continue
                if any(marker in page for marker in CLOSED_MARKERS):
                    logger.info("[compbiojobs] %s closed — skipped", job_url)
                    continue
                posting = _job_posting(page)
                if not posting:
                    logger.warning("[compbiojobs] no JobPosting JSON-LD on %s", job_url)
                    continue
                entry = self._make_entry(posting, job_url, discovery_url=url)
                if entry is not None:
                    entries.append(entry)

        logger.info("[compbiojobs] %d entries after parse + match", len(entries))
        return entries

    # ── one job → one entry ───────────────────────────────────────────────
    def _make_entry(self, posting: dict, job_url: str, *, discovery_url: str) -> Optional[WikiEntry]:
        if self.matcher is None:
            return None
        title = unescape(str(posting.get("title") or "")).strip()
        org = _org_of(posting)
        if not title or not org:
            return None
        description = unescape(re.sub(r"<[^>]+>", " ", str(posting.get("description") or "")))
        description = re.sub(r"\s+", " ", description).strip()[:MAX_DESCRIPTION_CHARS]
        location = _location_of(posting)
        posted_at = _posted_at(posting)
        employment = str(posting.get("employmentType") or "")
        if employment:
            description = f"{description}\n\nEmployment type: {employment}"

        match = self.matcher.match(title, description, org, job_url=job_url, location=location)

        if self.registry is not None:
            try:
                self.registry.upsert(
                    org=org, title=title, url=job_url,
                    careers_url=f"{BASE_URL}/companies/{_slugify(org)}",
                    source=self.name,
                    primary=self.primary,
                    discovery_url=discovery_url,
                    discovery_date=date.today().isoformat(),
                    # the board republishes the employer's stated date: a real
                    # posting date, not a discovery date (docs/28 §WP3a)
                    posted_at=posted_at,
                )
            except Exception as exc:      # noqa: BLE001 - a swallowed upsert is data loss
                logger.warning("[compbiojobs] Registry upsert failed (%s): %s", type(exc).__name__, exc)

        return WikiEntry(
            org_name=org,
            title=title,
            url=job_url,
            location=location or None,
            score=match.score,
            decision=match.decision,
            reasoning=match.reasoning,
            source=self.name,
            discovery_url=discovery_url,
            discovery_date=date.today().isoformat(),
            advert_excerpt=description,
            **match_fields(match),
        )


def _slugify(name: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")
