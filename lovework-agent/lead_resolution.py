"""Resolve actionable leads to their original advert, and harvest the company.

Post-match step for GO/MAYBE leads whose advert sits behind a LinkedIn wall
(`enrichment.py` fetches those through the guest endpoint, which carries the
text but no outbound apply link). For each such lead we search for the original
posting, prefer its URL in the report, and record the company in the
principal's periodic-check list — a company hiring once is worth watching.

Cost control: only GO/MAYBE leads are searched, results are cached per
(company, title), and one run searches at most `LOVEWORK_RESOLVE_MAX_SEARCHES`.
Set `LOVEWORK_RESOLVE_ORIGINALS=0` to disable the step entirely.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from enrichment import linkedin_job_id
from sources.company_pages import add_entry, resolve_board_url
from web_search import AdvertMatch, find_original_advert

logger = logging.getLogger(__name__)

CACHE_FILENAME = "original_adverts.json"
CACHE_TTL_DAYS = 30
RESOLVABLE_DECISIONS = {"GO", "MAYBE"}
DEFAULT_MAX_SEARCHES = 25
HARVEST_CADENCE_DAYS = 30
HARVEST_REASON = "lead-detected"


def enabled() -> bool:
    return os.getenv("LOVEWORK_RESOLVE_ORIGINALS", "1").strip().lower() not in {"0", "false", "no"}


def _max_searches() -> int:
    try:
        return int(os.getenv("LOVEWORK_RESOLVE_MAX_SEARCHES", str(DEFAULT_MAX_SEARCHES)))
    except ValueError:
        return DEFAULT_MAX_SEARCHES


def _cache_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / "search" / CACHE_FILENAME


def _load_cache(cache_dir: Path) -> dict:
    path = _cache_path(cache_dir)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unreadable original-advert cache %s: %s", path, exc)
        return {}


def _save_cache(cache_dir: Path, data: dict) -> None:
    path = _cache_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _key(company: str, title: str) -> str:
    return f"{(company or '').strip().lower()}|{(title or '').strip().lower()}"


def _is_fresh(entry: dict) -> bool:
    stamped = entry.get("at")
    if not stamped:
        return False
    try:
        # Aware, to match the aware stamp we write: comparing an aware value
        # against a naive `now()` raises TypeError, which the except below
        # would silently read as "stale" (2026-09-21).
        return (datetime.fromisoformat(stamped)
                > datetime.now(timezone.utc) - timedelta(days=CACHE_TTL_DAYS))
    except (TypeError, ValueError):
        return False


def _seed_for(match: AdvertMatch) -> str:
    """Which URL to check periodically: the employer's site when known, else
    the ATS posting (the crawler follows it to the other roles).

    A search result on neither the employer's domain nor a known ATS is not a
    company — it is a board or a mirror, and must never enter the keep-list.
    """
    return match.site_url or match.advert_url


def resolve_actionable(
    entries: Iterable,
    profile_name: str,
    *,
    cache_dir: Path,
    searcher: Callable[..., AdvertMatch] = find_original_advert,
    harvest: bool = True,
    max_searches: Optional[int] = None,
) -> dict:
    """Resolve original adverts for actionable LinkedIn leads; harvest companies.

    Mutates `entries` in place by setting `original_url`, and returns counters
    for logging/ledger. Never raises on a search or write failure — a lead that
    cannot be resolved keeps its LinkedIn URL.
    """
    cache_dir = Path(cache_dir)
    budget = _max_searches() if max_searches is None else max_searches
    cache = _load_cache(cache_dir)
    counters = {"considered": 0, "searched": 0, "resolved": 0, "cached": 0, "harvested": 0, "budget_exhausted": 0}
    dirty = False

    for entry in entries:
        if getattr(entry, "decision", "") not in RESOLVABLE_DECISIONS:
            continue
        if not linkedin_job_id(getattr(entry, "url", "") or ""):
            continue
        counters["considered"] += 1

        key = _key(entry.org_name, entry.title)
        record = cache.get(key) or {}
        if record and _is_fresh(record):
            match = AdvertMatch(
                advert_url=record.get("advert_url") or "",
                site_url=record.get("site_url") or "",
                provider=record.get("provider") or "",
                rank=int(record.get("rank", -1)),
            )
            counters["cached"] += 1
        else:
            if counters["searched"] >= budget:
                counters["budget_exhausted"] += 1
                continue
            counters["searched"] += 1
            try:
                match = searcher(entry.org_name, entry.title, getattr(entry, "location", "") or "")
            except Exception as exc:
                logger.warning("Original-advert search failed for %s: %s", entry.org_name, exc)
                match = AdvertMatch()
            cache[key] = {
                "advert_url": match.advert_url,
                "site_url": match.site_url,
                "provider": match.provider,
                "rank": match.rank,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            dirty = True

        # Only the employer's own advert is preferred over the LinkedIn link;
        # a bare careers index is a place to check, not an advert.
        if match.advert_url:
            entry.original_url = match.advert_url
            counters["resolved"] += 1

        seed = _seed_for(match)
        if harvest and seed:
            # A watch entry must name a *board*. `_seed_for` hands back the lead's own
            # advert URL (or a tracker URL when the company is already listed), and an
            # advert entry goes inert the day that posting closes — the
            # homepage-vs-board lesson, docs/37 §3b. A company we cannot find a board
            # for is left unharvested and counted, rather than added as coverage that
            # can never produce anything.
            board = resolve_board_url(entry.org_name, candidate_url=seed)
            if not board:
                counters["no_board"] = counters.get("no_board", 0) + 1
                logger.info("Lead harvest skipped, no board found for %s", entry.org_name)
            else:
                try:
                    added = add_entry(
                        profile_name,
                        name=entry.org_name,
                        careers_url=board,
                        reason=HARVEST_REASON,
                        cadence_days=HARVEST_CADENCE_DAYS,
                    )
                except Exception as exc:
                    logger.warning("Company harvest failed for %s: %s", entry.org_name, exc)
                    added = False
                counters["harvested"] += int(bool(added))

    if dirty:
        try:
            _save_cache(cache_dir, cache)
        except OSError as exc:
            logger.warning("Could not write original-advert cache: %s", exc)
    return counters
