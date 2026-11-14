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
import re
from datetime import datetime, timedelta
from functools import lru_cache
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
from crawler import SmartCrawler, job_evidence_text

from urllib.parse import urlparse

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


def _profile_dir(profile_name: str = "lj") -> Path:
    return config.PROFILES_DIR / profile_name.lower()


def _list_path(profile_name: str = "lj") -> Path:
    return _profile_dir(profile_name) / DEFAULT_FILENAME


def _seed_starter_list(profile_name: str = "lj") -> None:
    """Write a principal's starter list, if it has one. Idempotent.

    Only LJ has a starter template. Writing LJ's AI-lab list into another
    principal's file would silently inject his curated targets into their
    crawl (the bug class fixed in digitaljobs, 2026-09-12); other principals
    start empty and fill up from harvesting.
    """
    starter = _STARTER_LISTS.get(profile_name.lower())
    if starter is None:
        return
    p = _list_path(profile_name)
    if p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(starter, sort_keys=False), encoding="utf-8")
    logger.info(f"[company_pages] Seeded starter list for {profile_name} at {p}")


def _lj_starter_list() -> List[dict]:
    return [
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


_STARTER_LISTS = {"lj": _lj_starter_list()}


def _load_list(profile_name: str = "lj") -> List[dict]:
    """Load the watch list, raising loudly on a broken file.

    2026-10-02: VJ's list silently read as empty for days because a
    hand-written ``reason`` contained an unquoted colon (YAML scanner
    error) and the old code let the exception escape to callers that
    treated "cannot parse" as "no entries". A watch list that fails to
    parse is broken, not empty — the crawl must not degrade quietly
    (the quiet-vs-broken lesson, LEARNINGS 2026-09-10). Callers that
    genuinely want best-effort may catch the error themselves."""
    _seed_starter_list(profile_name)
    p = _list_path(profile_name)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    # YAML is the canonical format; JSON is accepted as a convenience.
    if p.suffix.lower() == ".json":
        return json.loads(text)
    return list(yaml.load(text, Loader=_YAML_LOADER) or [])


def _save_list(rows: List[dict], profile_name: str = "lj") -> None:
    p = _list_path(profile_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(rows, sort_keys=False), encoding="utf-8")


# ── what deserves watching: boards, not adverts (2026-10-01) ─────────────────
# A watch-list entry must name a *board*. An advert URL under `careers_url` makes the
# entry inert — the crawl re-reads one posting, sees no jobs once it closes (the
# homepage-vs-board lesson, docs/37 §3b), and the company silently stops producing
# anything. Alert-mail links are worse: they carry the reader's own tracking
# identifiers (lipi/midToken/trackingId). Both callers that create entries —
# promote_demote (cube promotions) and lead_resolution (harvested leads) — resolve
# through `resolve_board_url`, so the rule lives in exactly one place.
_MAIL_URL_MARKERS = ("linkedin.com/comm/", "lipi=", "midtoken=", "trackingid=", "trkemail=")

_BOARD_HOSTS = {
    "jobs.ashbyhq.com": "https://jobs.ashbyhq.com/{slug}",
    "job-boards.greenhouse.io": "https://job-boards.greenhouse.io/{slug}",
    "boards.greenhouse.io": "https://job-boards.greenhouse.io/{slug}",
    "jobs.lever.co": "https://jobs.lever.co/{slug}",
    "apply.workable.com": "https://apply.workable.com/{slug}",
}

#: Path shapes that name one posting rather than a place to look. A careers index
#: ("/careers", "/jobs", even "/jobs/") is a fine watch target — the trackers list
#: those — but "/jobs/12345" or "/job/4875778/data-scientist" is not. So the marker
#: only counts when a non-empty segment follows it: matching bare "jobs" would
#: reject the indexes, and requiring a following segment without allowing the
#: trailing slash would false-positive on "/jobs/".
_ADVERT_PATH = re.compile(
    r"/(?:jobs?|positions?|vacanc\w*|openings?|listings?|apply)/[^/]"
    r"|/(?:jobs?|positions?|vacanc\w*|openings?|listings?)-[0-9a-f]{6,}",
    re.IGNORECASE,
)


def is_mail_url(url: str) -> bool:
    """True for alert-mail links, which are never crawl targets."""
    low = (url or "").lower()
    return any(marker in low for marker in _MAIL_URL_MARKERS)


def board_from_advert(url: str) -> Optional[str]:
    """The board an advert belongs to, when the URL says so plainly.

    Nothing here guesses: an unknown shape returns None so the caller can decline to
    create an entry, rather than create one that can never find work.
    """
    parsed = urlparse(url or "")
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")

    # Per-company board subdomains: the subdomain *is* the board.
    if host.endswith(".homerun.co") or host.endswith(".recruitee.com"):
        return f"https://{host}"

    # A company's own listing page: the tail after "…/careers/jobs/" is one posting.
    tail = re.match(r"^(?P<base>.*?)/?(?:careers|jobs)/(?:job|jobs|position|positions)/[^/]+/?$",
                    path, re.IGNORECASE)
    if tail:
        base = tail.group("base").strip("/")
        return f"https://{host}/{base}/careers" if base else f"https://{host}/careers"

    template = _BOARD_HOSTS.get(host)
    if not template:
        return None
    parts = [p for p in path.split("/") if p]
    return template.format(slug=parts[0]) if parts else None


def _norm_key(name: str) -> str:
    """Case/punctuation-folded company key, so spellings can be compared."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").casefold())


@lru_cache(maxsize=8)
def verified_boards(trackers_root: Optional[str] = None) -> dict:
    """{normalised name: verified careers url} from every principal's market tracker."""
    root = Path(trackers_root) if trackers_root else config.PROFILES_DIR
    boards: dict = {}
    try:
        from .market_players import MarketPlayersSource
    except Exception:  # a policy pass must never die on an import
        return boards
    for name in ("lj", "vj", "kj", "pk"):
        if not (root / name / "market_tracker.txt").is_file():
            continue
        try:
            source = MarketPlayersSource(crawler=None, matcher=None, profile_name=name)
            # Read the file from *this* root: the source would otherwise resolve the
            # path through config and quietly ignore the one we were pointed at.
            source.tracker_path = root / name / "market_tracker.txt"
            orgs = source._parse_tracker()
        except Exception:
            continue
        for org in orgs:
            url = org.get("careers_url")
            if url and not is_mail_url(url):
                boards.setdefault(_norm_key(org["name"]), url)
    return boards


def lookup_board(boards: dict, company: str) -> Optional[str]:
    """A verified board for this company, allowing for spelling drift.

    The cube says "Hyper Recruitment Solutions" where KJ's tracker says
    "Hyper Recruitment Solutions (HRS)"; a prefix relation on the folded key, with a
    length floor so short names cannot collide, is enough to reuse the board.
    """
    key = _norm_key(company)
    if len(key) < 6:
        # A short query name matches almost anything by prefix ("X" → XPS Group).
        # Below the floor, only an exact hit counts.
        return boards.get(key)
    if boards.get(key):
        return boards[key]
    for candidate, url in boards.items():
        if len(candidate) >= 6 and (candidate.startswith(key) or key.startswith(candidate)):
            return url
    return None


def resolve_board_url(company: str, *, candidate_url: Optional[str] = None,
                      trackers_root: Optional[str] = None) -> Optional[str]:
    """The board to watch for this company, or None when none can be established.

    Callers decline to create an entry when this returns None: an entry nothing can
    be found on is worse than no entry, because it looks like coverage.
    """
    known = lookup_board(verified_boards(trackers_root), company)
    if known:
        return known
    url = str(candidate_url or "")
    if not url.startswith("http") or is_mail_url(url):
        return None
    board = board_from_advert(url)
    if board:
        return board
    # A place to look is a fine watch target (the trackers themselves list site URLs
    # and let the crawler navigate); one posting is not.
    path = urlparse(url).path or "/"
    if _ADVERT_PATH.search(path):
        return None
    return url


def add_entry(
    profile_name: str,
    *,
    name: str,
    careers_url: str,
    reason: str = "lead-detected",
    cadence_days: int = 30,
) -> bool:
    """Add a company to a principal's periodic-check list. Returns True if added.

    Idempotent by company name (case-insensitive) and by careers host, so a
    company seen in several leads, or on several runs, is recorded once.
    Existing entries keep their own cadence and reason — this only ever adds.
    """
    name = (name or "").strip()
    careers_url = (careers_url or "").strip()
    if not name or not careers_url:
        return False

    rows = _load_list(profile_name)
    wanted_name = name.lower()
    wanted_host = urlparse(careers_url).netloc.lower()
    for row in rows:
        if str(row.get("name") or "").strip().lower() == wanted_name:
            return False
        if wanted_host and urlparse(str(row.get("careers_url") or "")).netloc.lower() == wanted_host:
            return False

    rows.append({
        "name": name,
        "careers_url": careers_url,
        "cadence_days": cadence_days,
        "reason": reason,
        "last_checked": None,
        "last_found": 0,
    })
    _save_list(rows, profile_name)
    logger.info(f"[company_pages] Harvested {name} ({careers_url}) into {profile_name}")
    return True


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
    #: This source's tier, declared here and nowhere else
    #: (docs/28 §WP3b box 1). Primary = the hiring party's own
    #: publication; secondary = someone relaying it.
    primary = True
    #: Set when the run enumerated its *whole* scope (no truncation,
    #: caps or failures). Only then may absence become
    #: `not_listed_in_scope` rather than `disappeared`.
    complete_scope = False

    """Re-crawls known target company careers pages on a per-entry cadence."""

    name = "company_pages"

    def __init__(self, crawler: SmartCrawler, matcher: Optional[JobMatcher] = None,
                 registry: Optional[JobRegistry] = None, profile_name: str = "lj"):
        self.crawler = crawler
        self.matcher = matcher
        self.registry = registry
        self.profile_name = profile_name

    def run(self) -> List[WikiEntry]:
        rows = _load_list(self.profile_name)
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
                            primary=self.primary,
                        )
                    except Exception as e:
                        # A swallowed upsert is data loss, and the exception type is
                        # what separates contract drift from a transient failure
                        # (LEARNINGS 2026-09-20).
                        logger.warning(f"[{self.name}] Registry upsert failed "
                                       f"({type(e).__name__}): {e}")

                # **The advert, not a summary of the list** (2026-10-15): the
                # drill-down read the role's own page, and this is what hands it to
                # the judgement — the score and the eligibility kills alike.
                desc = job_evidence_text(job)
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
        _save_list(rows, self.profile_name)

        logger.info(f"[{self.name}] {len(entries)} entries across {len(due)} re-crawled companies")
        return entries
