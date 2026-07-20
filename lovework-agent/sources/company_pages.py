"""
Source: company-landing-pages — LJ's curated list of careers landing pages
for known target companies. Each entry has a re-crawl cadence (days) so the
cron job decides per-entry whether to re-visit today.

Why a separate source:
  - `research_orgs` is a public, one-shot list (juleslogs tweet, 19 orgs).
  - `neolabs` and `hf_startups` are scraped from a local tracker file.
  - The company-landing-pages list is LJ's *personal* keep-list — past
    companies he's applied to (re-crawl every ~2 weeks) and YC companies
    that had ML/AI openings (re-crawl monthly). It is the source most
    likely to surface a re-application opportunity or a re-opening of a
    role LJ previously engaged with.

The list lives in a YAML/JSON file under profiles/<name>/company_pages.yaml
(or .json), with this shape:
  - name: Anthropic
    careers_url: https://www.anthropic.com/careers
    cadence_days: 14           # re-crawl every N days
    reason: applied            # one of: applied, yc_ml_ai, watchlist
    last_checked: 2026-06-10   # updated by the source on each run

The list is also written back after each run with `last_checked` and
`last_found` updated, so the cadence decision survives across runs without
a separate DB.

Cadence logic (cron-time):
  - today - last_checked >= cadence_days  → fetch today
  - else                                  → skip
This keeps the source cheap and focused. A daily "should I check today?"
decision is exactly what LJ asked for.

If the file does not exist, the source no-ops (returns []). The file is
created with a starter template the first time the agent runs.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import yaml  # pyyaml — already a transitive dep in this stack

# Disable PyYAML's auto-parsing of ISO dates: we want last_checked to
# round-trip as a string so the cadence math (fromisoformat) works
# uniformly and the persisted file is human-readable.
_YAML_LOADER = yaml.SafeLoader
_YAML_LOADER.yaml_implicit_resolvers = {
    k: [(t, r) for t, r in v if t != "tag:yaml.org,2002:timestamp"]
    for k, v in _YAML_LOADER.yaml_implicit_resolvers.items()
}

from job_registry import JobRegistry
from matcher import JobMatcher
from wiki_store import WikiEntry, match_fields

import config
from crawler import SmartCrawler

logger = logging.getLogger(__name__)

DEFAULT_FILENAME = "company_pages.yaml"

# Cadence defaults by reason. Applied to entries that omit cadence_days.
DEFAULT_CADENCE_DAYS = {
    "applied": 14,        # past employer / applied → fortnightly
    "yc_ml_ai": 30,       # YC company with prior ML/AI opening → monthly
    "watchlist": 7,       # manual watchlist → weekly
}
# Force a check today if this many days have elapsed, regardless of cadence.
FORCE_CHECK_AFTER_DAYS = 60


def _profile_dir() -> Path:
    return config.PROFILES_DIR / "lj"


def _list_path() -> Path:
    return _profile_dir() / DEFAULT_FILENAME


def _seed_starter_list() -> None:
    """Write a starter template so the file always exists. Idempotent."""
    p = _list_path()
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    starter = [
        {
            "name": "Anthropic",
            "careers_url": "https://www.anthropic.com/careers",
            "cadence_days": 14,
            "reason": "applied",
            "last_checked": None,
            "last_found": 0,
        },
        {
            "name": "OpenAI",
            "careers_url": "https://openai.com/careers",
            "cadence_days": 14,
            "reason": "applied",
            "last_checked": None,
            "last_found": 0,
        },
        {
            "name": "DeepMind",
            "careers_url": "https://deepmind.google/careers",
            "cadence_days": 14,
            "reason": "applied",
            "last_checked": None,
            "last_found": 0,
        },
        {
            "name": "Hugging Face",
            "careers_url": "https://huggingface.co/jobs",
            "cadence_days": 14,
            "reason": "applied",
            "last_checked": None,
            "last_found": 0,
        },
        {
            "name": "Mistral",
            "careers_url": "https://mistral.ai/careers",
            "cadence_days": 30,
            "reason": "yc_ml_ai",
            "last_checked": None,
            "last_found": 0,
        },
        {
            "name": "Perplexity",
            "careers_url": "https://www.perplexity.ai/careers",
            "cadence_days": 30,
            "reason": "yc_ml_ai",
            "last_checked": None,
            "last_found": 0,
        },
    ]
    p.write_text(yaml.safe_dump(starter, sort_keys=False), encoding="utf-8")
    logger.info(f"[company_pages] Seeded starter list at {p}")


def _load_list() -> List[dict]:
    _seed_starter_list()
    p = _list_path()
    text = p.read_text(encoding="utf-8")
    # YAML is the canonical format; JSON is accepted as a convenience.
    if p.suffix.lower() == ".json":
        return json.loads(text)
    return list(yaml.load(text, Loader=_YAML_LOADER) or [])


def _save_list(rows: List[dict]) -> None:
    p = _list_path()
    p.write_text(yaml.safe_dump(rows, sort_keys=False), encoding="utf-8")


def _should_check_today(entry: dict, today: str) -> bool:
    """Cron-time decision: is this entry due for a re-crawl today?"""
    last = entry.get("last_checked")
    if not last:
        return True  # never checked
    # YAML may load ISO dates as datetime.date objects; normalise to a
    # string so fromisoformat below works uniformly.
    if hasattr(last, "isoformat"):
        last = last.isoformat()
    try:
        last_dt = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True
    days = (datetime.fromisoformat(today) - last_dt).days
    cadence = int(entry.get("cadence_days") or DEFAULT_CADENCE_DAYS.get(
        entry.get("reason", "watchlist"), 14
    ))
    return days >= cadence or days >= FORCE_CHECK_AFTER_DAYS


class CompanyPagesSource:
    """Re-crawls known target company careers pages on a per-entry cadence."""

    name = "company_pages"

    def __init__(self, crawler: SmartCrawler, matcher: Optional[JobMatcher] = None,
                 registry: Optional[JobRegistry] = None):
        self.crawler = crawler
        self.matcher = matcher
        self.registry = registry

    def run(self) -> List[WikiEntry]:
        rows = _load_list()
        if not rows:
            logger.info(f"[{self.name}] No company-pages entries; skipping.")
            return []

        today = datetime.now().strftime("%Y-%m-%d")
        due = [r for r in rows if _should_check_today(r, today)]
        logger.info(f"[{self.name}] {len(due)} of {len(rows)} entries due today ({today})")

        entries: List[WikiEntry] = []
        found_counts: dict[str, int] = {}

        for row in due:
            name = row.get("name") or "Unknown"
            url = row.get("careers_url") or ""
            if not url:
                logger.debug(f"[{self.name}] {name} has no careers_url; skipping")
                continue
            try:
                jobs = self.crawler.crawl_org(
                    org_name=name,
                    seed_urls=[url],
                    goal=(
                        "Find open technical positions at this company. "
                        "Look for: ML engineer, AI engineer, AI researcher, "
                        "research engineer, applied scientist, founding engineer, "
                        "or similar. Skip pure finance / quant / SWE roles."
                    ),
                    max_pages=4,
                )
            except Exception as e:
                logger.error(f"[{self.name}] Crawl failed for {name}: {e}")
                continue

            for job in jobs:
                record = None
                if self.registry is not None:
                    try:
                        record = self.registry.upsert(
                            org=name, title=job.title, url=job.url or "",
                            careers_url=url, source=self.name,
                        )
                    except Exception as e:
                        logger.debug(f"[{self.name}] Registry upsert failed: {e}")

                desc = " ".join(filter(None, [
                    job.description_snippet, job.requirements_snippet,
                    job.employment_type,
                ]))
                match = self.matcher.match(
                    job.title, desc, name,
                    job_url=job.url or "",
                    location=job.location or "",
                ) if self.matcher is not None else None

                if match is None:
                    continue
                entry = WikiEntry(
                    org_name=name, title=job.title, url=job.url,
                    location=job.location,
                    score=match.score, decision=match.decision,
                    reasoning=match.reasoning, source=self.name,
                    advert_excerpt=desc,
                    **match_fields(match),
                )
                if record is not None:
                    entry.lifecycle_status = record.status
                    entry.first_seen = record.first_seen
                entries.append(entry)

            found_counts[name] = len(jobs)
            # Mark this entry as checked.
            row["last_checked"] = today

        # Persist updated last_checked + last_found counts.
        for r in rows:
            r["last_found"] = int(found_counts.get(r.get("name", ""), 0))
        _save_list(rows)

        logger.info(f"[{self.name}] {len(entries)} entries across {len(due)} re-crawled companies")
        return entries
