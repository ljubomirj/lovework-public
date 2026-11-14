"""Web search used to resolve a lead back to its original advert.

LinkedIn serves the advert text through its public guest endpoint but keeps the
apply flow on LinkedIn, so the original posting cannot be read off the page —
it has to be found by searching the company, the role title and (when known)
the location.

**The order is configuration, not a constant.** `LOVEWORK_SEARCH_PROVIDERS`
decides it per deployment; the code fallback is `deepseek,firecrawl,brave`.
Firecrawl led from 2026-10-26 (Tavily's monthly quota was spent) and Tavily
became primary again on 2026-11-04 — named in that host's `.env`, not here. Exa
is blacklisted (credits exhausted 2026-10-22) and its endpoints are never
called. An explicit `provider=` argument overrides the order.

Keys resolve from the environment first, then from the host-local credential
file, matching the Gmail-token boundary:

    ~/.lovework/credentials/search/firecrawl_api_key
    ~/.lovework/credentials/search/brave_api_key
    ~/.lovework/credentials/search/tavily_api_key

A missing key is not an error: `search()` returns no results and callers fall
back to whatever the lead already had.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TAVILY_ENDPOINT = "https://api.tavily.com/search"
# Exa is **blacklisted** for all LoveWork web use (2026-10-23, LJ: out of credits).
# Its endpoints are not called.
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
# DeepSeek Search & Fetch (live 2026-10-18): raw JSON, same bearer key as the
# LLM. $0.75 per 1,000 successful requests each; QPS 100 search / 25 fetch
# (plus a per-domain cap on fetch). Search returns up to 50 ranked results.
DEEPSEEK_SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search"
DEEPSEEK_FETCH_ENDPOINT = "https://api.deepseek.com/v1/tools/fetch"
# DeepSeek Search & Fetch (2026-10-18): raw JSON, same bearer key as the LLM.
# $0.75 per 1,000 successful requests each; QPS 100 search / 25 fetch (and a
# per-domain cap on fetch). Search returns up to 50 ranked results.
DEEPSEEK_SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search"
DEEPSEEK_FETCH_ENDPOINT = "https://api.deepseek.com/v1/tools/fetch"
#: Search providers, in preference order. Firecrawl leads since 2026-10-26
#: (Tavily's monthly quota is spent); Tavily stays last-but-wired and returns by
#: setting `LOVEWORK_SEARCH_PROVIDERS=tavily,firecrawl,brave` when it resets.
def providers_from(spec: str) -> tuple[str, ...]:
    """Parse a `LOVEWORK_SEARCH_PROVIDERS` value: comma-separated, lowercased."""
    return tuple(p.strip().lower() for p in spec.split(",") if p.strip())


SEARCH_PROVIDERS = providers_from(
    os.getenv("LOVEWORK_SEARCH_PROVIDERS", "deepseek,firecrawl,brave")
)
SEARCH_TIMEOUT = 25
FETCH_TIMEOUT = 45

CREDENTIALS_DIR = Path(
    os.getenv(
        "LOVEWORK_SEARCH_CREDENTIALS_DIR",
        Path(os.getenv("LOVEWORK_HOME") or Path.home() / ".lovework") / "credentials" / "search",
    )
)

# Applicant-tracking hosts: an advert here belongs to some other company, so it
# is a good "original advert" but not a company website.
ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "workday.com",
    "smartrecruiters.com", "bamboohr.com", "icims.com", "workable.com",
    "teamtailor.com", "recruitee.com", "jobvite.com", "breezy.hr", "personio",
    "successfactors", "taleo.net", "jobs.",
)
# Aggregators, mirrors and social sites: never the original advert, and never a
# seed for periodic checking (the keep-list must not fill with boards).
BLOCKED_HOSTS = (
    "linkedin.com", "licdn.com", "facebook.com", "twitter.com", "x.com",
    "glassdoor.", "indeed.", "ziprecruiter.", "monster.", "totaljobs.",
    "cwjobs.", "reed.co.uk", "talent.com", "jooble.", "adzuna.",
    "simplyhired.", "jobrapido.", "careerjet.", "jobsite.", "jobsora.",
    "whatjobs.", "learn4good.", "jobs.theguardian", "fish4jobs", "cv-library",
    # mirrors that republish employer adverts, and company-profile/review sites
    "builtin", "wallstreetoasis", "efinancialcareers", "levels.fyi",
    "comparably", "teamblind", "blind.", "rocketreach", "zoominfo",
    "crunchbase", "owler", "signalhire", "apollo.io", "lusha", "glassdoor",
)


def api_key(env_name: str) -> str:
    """Resolve a search key: environment first, then the host-local file."""
    value = os.getenv(env_name, "").strip()
    if value:
        return value
    path = CREDENTIALS_DIR / env_name.lower()
    if path.is_file():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("Unreadable search credential %s: %s", path, exc)
    return ""


#: Provider → the env var its key lives in.
KEY_ENV = {
    "firecrawl": "FIRECRAWL_API_KEY",
    "brave": "BRAVE_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}


def key_for(name: str) -> str:
    """Resolve one provider's key: env, credential file, then the project .env.

    Firecrawl's key is the tracked `.env` one (`FIRECRAWL_API_KEY`, loaded into
    the environment by `config`), so it is also read from there — a bare
    `web_search` invocation may run before `config` has loaded it.
    """
    env_name = KEY_ENV.get(name, "")
    value = api_key(env_name) if env_name else ""
    if value:
        return value
    try:
        import config

        return getattr(config, env_name, "") or ""
    except Exception:
        return ""


def available_providers() -> list[str]:
    return [name for name in SEARCH_PROVIDERS if key_for(name)]


def _firecrawl_search(query: str, count: int, key: str) -> list[dict]:
    """Firecrawl Search (`/v1/search` through the SDK).

    The SDK's v2 shape is `SearchData.web` of `SearchResult`; older releases
    returned plain dicts, so both are accepted rather than guessing.
    """
    from firecrawl import Firecrawl

    data = Firecrawl(api_key=key).search(query, limit=max(1, count))

    def _field(item, name):
        value = getattr(item, name, None)
        if value is None and isinstance(item, dict):
            value = item.get(name)
        return value or ""

    items = getattr(data, "web", None)
    if items is None and isinstance(data, dict):
        items = data.get("web") or data.get("data") or []
    return [
        {"url": _field(item, "url"), "title": _field(item, "title"),
         "snippet": _field(item, "description") or _field(item, "snippet")}
        for item in (items or [])
        if _field(item, "url")
    ]


def _brave(query: str, count: int, key: str) -> list[dict]:
    import httpx

    response = httpx.get(
        BRAVE_ENDPOINT,
        timeout=SEARCH_TIMEOUT,
        params={"q": query, "count": count},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
    )
    response.raise_for_status()
    return [
        {
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "snippet": item.get("description", ""),
        }
        for item in ((response.json().get("web") or {}).get("results") or [])
    ]


def _tavily(query: str, count: int, key: str) -> list[dict]:
    """Tavily Search — the one web-search provider since 2026-10-23.

    `search_depth: advanced` is the same reason the page fetcher uses it: the
    query is usually a role title behind an aggregator, and the original advert
    often lives on a script-heavy careers page.
    """
    import httpx

    response = httpx.post(
        TAVILY_ENDPOINT,
        timeout=SEARCH_TIMEOUT,
        json={"api_key": key, "query": query, "max_results": max(1, count),
              "search_depth": "advanced"},
    )
    response.raise_for_status()
    return [
        {"url": item.get("url") or "", "title": item.get("title") or "",
         "snippet": item.get("content") or ""}
        for item in (response.json().get("results") or [])
    ]


def _deepseek(query: str, count: int, key: str) -> list[dict]:
    """DeepSeek Search (`POST /v1/tools/search`) — its own engine.

    Each result carries `authority` (0-4, >=3 highly authoritative) and
    `rerank_score` (>=1.5 high quality), so quality can be filtered instead
    of trusting rank. Query operators pass through (`site:`, `-site:`,
    `after:`, quoted phrases) — what `find_original_advert` needs.
    """
    import httpx

    response = httpx.post(
        DEEPSEEK_SEARCH_ENDPOINT,
        timeout=SEARCH_TIMEOUT,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json={"query": query, "count": max(1, min(50, count))},
    )
    response.raise_for_status()
    return [
        {"url": item.get("url") or "",
         "title": item.get("title") or "",
         "snippet": item.get("summary") or item.get("snippet") or ""}
        for item in (response.json().get("results") or [])
    ]


def search(query: str, count: int = 8, *, provider: Optional[str] = None) -> tuple[str, list[dict]]:
    """Return (provider_used, results). Provider '' means no key/available."""
    if provider == "exa":
        logger.warning("[web_search] exa is blacklisted (credits exhausted 2026-10-22); "
                       "use firecrawl — returning no results")
        return "", []
    runners = {"deepseek": _deepseek, "firecrawl": _firecrawl_search,
               "brave": _brave, "tavily": _tavily}
    order = (provider,) if provider else SEARCH_PROVIDERS
    for name in order:
        runner = runners.get(name)
        if runner is None:
            logger.warning("[web_search] unknown search provider %r — ignored", name)
            continue
        key = key_for(name)
        if not key:
            continue
        try:
            results = runner(query, count, key)
            logger.info("[web_search] %s returned %d result(s) for %r", name, len(results), query[:70])
            return name, results
        except Exception as exc:
            logger.warning("[web_search] %s search failed: %s", name, exc)
    if not provider:
        logger.info("[web_search] no search provider configured (%s)",
                    " / ".join(KEY_ENV.get(n, n) for n in SEARCH_PROVIDERS))
    return "", []


def _host(url: str) -> str:
    return urlparse(url or "").netloc.lower().removeprefix("www.")


def _usable(url: str) -> bool:
    host = _host(url)
    if not host or not url.lower().startswith("http"):
        return False
    return not any(blocked in host for blocked in BLOCKED_HOSTS)


# A candidate must look like an advert, not a company profile page on some
# aggregator: the first live run returned a forum company page for a role whose
# advert had expired (2026-09-12).
POSTING_PATH_HINTS = (
    "/job", "/career", "/vacanc", "/position", "/opening", "/apply",
    "/role", "/opportunit", "/hiring",
)


def _looks_like_posting(url: str) -> bool:
    path = urlparse(url or "").path.lower()
    return any(hint in path for hint in POSTING_PATH_HINTS)


def is_advert_path(url: str) -> bool:
    """True for an individual advert, false for a careers/jobs index.

    `/careers/` and `/jobs/` are places to look; `/careers/<role>` and
    `/jobs/<id>` are the advert itself. Only the latter may become a lead's URL.
    """
    if not _looks_like_posting(url):
        return False
    segments = [seg for seg in urlparse(url or "").path.split("/") if seg]
    return len(segments) > 1


def _rank(url: str, company: str) -> Optional[tuple[int, str]]:
    """Rank a candidate: the employer's own domain, then a known ATS.

    Anything else (a board, a mirror, a company-profile page) is rejected: the
    LinkedIn link we already hold is better than an advert we cannot attribute
    to the employer. Returns (rank, url), or (rank, "") when the URL is useful
    as a *site* to check but is not the advert itself.
    """
    if not _usable(url):
        return None
    host = _host(url)
    tokens = [t for t in "".join(
        char if char.isalnum() else " " for char in company.lower()
    ).split() if len(t) > 3]
    if tokens and any(token in host for token in tokens):
        return (0, url)
    if any(ats in host for ats in ATS_HOSTS):
        return (1, url)
    return None


@dataclass(frozen=True)
class AdvertMatch:
    """What a search found: the advert (if any) and a site worth checking."""

    advert_url: str = ""
    site_url: str = ""
    provider: str = ""
    rank: int = -1

    @property
    def found(self) -> bool:
        return bool(self.advert_url or self.site_url)


def find_original_advert(
    company: str,
    title: str,
    location: str = "",
    *,
    count: int = 8,
    provider: Optional[str] = None,
) -> AdvertMatch:
    """Search for the advert behind a LinkedIn posting.

    Returns the employer's advert when one is found, plus the site to check
    periodically. A careers index yields only a `site_url` (a place to look is
    not an advert); a board or mirror yields nothing.
    """
    company = (company or "").strip()
    title = (title or "").strip()
    if not company or not title:
        return AdvertMatch(provider=provider or "")

    query = f'"{title}" {company}'.strip()
    if location:
        query = f"{query} {location.strip()}"
    used, results = search(query, count=count, provider=provider)
    if not results:
        return AdvertMatch(provider=used)

    ranked = [r for r in (_rank(item.get("url", ""), company) for item in results) if r]
    if not ranked:
        logger.info("[web_search] %d result(s) for %r, none usable", len(results), query[:70])
        return AdvertMatch(provider=used)
    ranked.sort(key=lambda item: item[0])
    rank, best = ranked[0]
    if rank == 1:                       # ATS posting: the advert *is* the seed
        match = AdvertMatch(advert_url=best, site_url=best, provider=used, rank=rank)
    else:
        # The employer's own domain: an individual advert leads the report, a
        # careers/jobs index only tells us where to look next time.
        match = AdvertMatch(
            advert_url=best if is_advert_path(best) else "",
            site_url=site_root(best),
            provider=used,
            rank=rank,
        )
    logger.info(
        "[web_search] %r -> advert=%r site=%r (provider %s, rank %d)",
        query[:60], match.advert_url, match.site_url, used, rank,
    )
    return match


def site_root(url: str) -> str:
    """Return `https://host` for a URL — the seed used for periodic checking."""
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"
