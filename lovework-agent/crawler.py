"""
Smart crawler with LLM-in-the-loop for iterative job discovery.

Strategy:
1. Fetch a seed URL via Firecrawl (renders JS, returns markdown).
2. Ask LLM: given this page, did we find jobs? Where should we look next?
3. Iterate up to MAX_PAGES_PER_ORG / MAX_DEPTH.
4. Extract structured job listings with LLM.
"""

import hashlib
import json
import logging
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, Field

import config
import prompts  # the one place the prompts live (2026-10-15)
from llm_client import LLMClient

logger = logging.getLogger(__name__)

# Fetch backends, in preference order. **Firecrawl is primary since 2026-10-26**
# (LJ: *"With the current searching and scraping provider Tavily we have reached
# our monthly quota limit. Let us switch back to firecrawl"*). Tavily's monthly
# quota is spent, so it leaves the default chain — it stays wired (one env var
# away) and returns when the quota resets. Exa is **blacklisted** for crawling
# (credits exhausted 2026-10-22): naming it in `LOVEWORK_FETCH_PROVIDERS` is
# filtered out with a warning rather than silently billing a dead account.
# `http` is the last resort and never an acceptable *only* backend for a real
# crawl (see the preflight).
DEFAULT_FETCH_PROVIDERS = "deepseek,firecrawl,http"
#: Providers that must never be selected again, with the reason in the comment.
BLACKLISTED_PROVIDERS = {"exa"}          # credits exhausted 2026-10-22
PROVIDERS = tuple(
    p.strip().lower()
    for p in os.getenv("LOVEWORK_FETCH_PROVIDERS", DEFAULT_FETCH_PROVIDERS).split(",")
    if p.strip() and p.strip().lower() not in BLACKLISTED_PROVIDERS
)
if any(p.strip().lower() in BLACKLISTED_PROVIDERS
       for p in os.getenv("LOVEWORK_FETCH_PROVIDERS", "").split(",")):
    logger.warning("Ignoring blacklisted fetch provider(s) in "
                   "LOVEWORK_FETCH_PROVIDERS: %s", ", ".join(sorted(BLACKLISTED_PROVIDERS)))

#: Hosts whose pages are only visible to a logged-in visitor. Kept as data, with
#: the reason, because the rule is a fact about the site and not about us.
BROWSER_ONLY_HOSTS = (
    "linkedin.com",
    "www.linkedin.com",
    # LinkedIn's own shortener: the digest emails use it, and it redirects into
    # the wall, so the browser is needed on the first hop.
    "lnkd.in",
)


def _requires_browser(url: str) -> bool:
    """Does this host answer a logged-out reader with a wall instead of a page?"""
    try:
        from urllib.parse import urlsplit

        host = (urlsplit(url).netloc or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in BROWSER_ONLY_HOSTS)


_firecrawl_available = False
_firecrawl_app = None
if config.FIRECRAWL_API_KEY:
    try:
        from firecrawl import FirecrawlApp

        _firecrawl_app = FirecrawlApp(api_key=config.FIRECRAWL_API_KEY)
        _firecrawl_available = True
    except Exception as e:
        logger.warning(f"Firecrawl import failed: {e}")


def _tavily_available() -> bool:
    """Is a key resolvable for Tavily (env first, then the host-local file)?

    Resolved by `web_search.api_key`, not read from config, on purpose: cron
    does not source `~/.bashrc`, so `~/.lovework/credentials/search/tavily_api_key`
    is what a scheduled crawl uses.
    """
    try:
        from web_search import api_key

        return bool(api_key("TAVILY_API_KEY"))
    except Exception as e:                        # an import fault is unavailability
        logger.warning(f"Tavily availability check failed: {e}")
        return False


def _deepseek_available() -> bool:
    """DeepSeek Fetch is usable when its key resolves (env or credential file)."""
    from web_search import api_key

    return bool(api_key("DEEPSEEK_API_KEY"))


def _provider_available(name: str) -> bool:
    if name in BLACKLISTED_PROVIDERS:
        return False
    if name == "deepseek":
        return _deepseek_available()
    return _tavily_available() if name == "tavily" else (
        _firecrawl_available if name == "firecrawl" else name == "http"
    )


# ── a spent quota is a fact about the day, not about this URL ─────────────
#
# Firecrawl answered 402 "Insufficient credits" from 2026-11-03 and the chain kept
# calling it for every page: one "Payment Required" line per URL in the tick's
# stdout, all saying the same thing, and a wasted round-trip each time. LJ: *"we
# should switch to Tavily for time being … YEs this is expected."* A provider that
# says its quota is spent is skipped for the rest of the day, with **one** log
# line; the host-level state file makes that survive the process, and the entry
# expires at the start of the next day so a reset is discovered by one probe
# rather than by a month of silence.
QUOTA_MARKERS = (
    "insufficient credits", "payment required", "insufficient balance",
    "exceeded your current quota", "quota exceeded", "out of credits",
    "credit limit", "monthly limit", "billing",
)

#: The last error each provider reported, set by the `_*_fetch` functions. Read
#: after a failed attempt so the chain can tell "this page is fetchable by nobody
#: today" from "this provider is out of money" without changing every signature.
_PROVIDER_ERRORS: dict[str, str] = {}

#: Providers known to be out of quota for today: name → why. In memory, mirrored
#: into `state/fetch-providers.json`.
_EXHAUSTED: dict[str, str] = {}


def _provider_state_path() -> Path:
    return Path(config.STATE_DIR) / "fetch-providers.json"


def _load_exhausted() -> None:
    """Today's exhausted providers, from the host-level file (once per process)."""
    if _EXHAUSTED:
        return
    path = _provider_state_path()
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    today = date.today().isoformat()
    for name, entry in (data.get("exhausted") or {}).items():
        if str(entry.get("date")) == today and entry.get("why"):
            _EXHAUSTED[name] = str(entry["why"])


def provider_exhausted(name: str) -> str:
    """Why this provider must not be called today, or "" when it may be."""
    _load_exhausted()
    return _EXHAUSTED.get(name, "")


def mark_provider_exhausted(name: str, why: str) -> None:
    """Stop calling a provider for the rest of the day — logged once."""
    if not name or name in _EXHAUSTED:
        return
    _EXHAUSTED[name] = why
    logger.warning("[crawler] fetch provider %r is out of quota — skipped for the "
                   "rest of the day: %s", name, why[:200])
    path = _provider_state_path()
    try:
        data = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        data.setdefault("exhausted", {})[name] = {
            "date": date.today().isoformat(),
            "why": why[:300],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    except Exception as exc:               # the memory still holds for this process
        logger.debug("could not persist the exhausted list: %s", exc)


def _quota_message(text: str) -> str:
    """The quota phrase in a provider's error text, or "" when it is not that."""
    lowered = (text or "").lower()
    for marker in QUOTA_MARKERS:
        if marker in lowered:
            return text.strip()
    return ""


def _record_attempt(provider: str, url: str, text: str, seconds: float,
                    error: str = "", run: str = "chain") -> None:
    """File one attempt in the provider ledger (`fetch_ledger`). Never raises.

    The measurement LJ asked for — *"is some of those web search web fetch
    providers better or worse?"* — is only answerable if every real fetch leaves a
    row, not just the benchmark's.
    """
    try:
        import fetch_ledger

        fetch_ledger.record(provider, url, text, seconds=seconds, run=run, error=error)
    except Exception as exc:               # a ledger fault must not cost the fetch
        logger.debug("[crawler] fetch ledger write skipped: %s", exc)


class CrawlerUnavailableError(RuntimeError):
    """Raised when a crawl cannot start without the crawler service."""


def ensure_fetcher_available() -> None:
    """Fail-fast pre-flight: refuse to run a crawl without a real fetch service.

    Plain-HTTP-only fetching is not an acceptable degraded mode: Cloudflare-
    guarded sites (digitaljobs.com job pages, several careers pages) 403 every
    direct request, which silently degrades scoring evidence for the whole run
    (2026-09-05: a 24h LJ crawl produced hundreds of 403s before anyone noticed).
    A missing key must stop the crawl at once.

    Firecrawl (`FIRECRAWL_API_KEY`, primary since 2026-10-26) or Tavily
    (`TAVILY_API_KEY`, host-local credential file counts) satisfies the gate;
    which one is *used* is decided by `LOVEWORK_FETCH_PROVIDERS`. Exa is
    blacklisted (credits exhausted 2026-10-22); Tavily's monthly quota is spent
    (2026-10-26), so it is out of the default chain but still wired.
    """
    live = [p for p in PROVIDERS if p != "http" and _provider_available(p)]
    if not live:
        raise CrawlerUnavailableError(
            "Crawler service unavailable: no usable fetch provider. "
            "Primary-page fetches would 403 on Cloudflare-guarded sites "
            "(e.g. digitaljobs.com job pages), silently degrading evidence. "
            f"Configured providers: {', '.join(PROVIDERS) or 'none'}. "
            "Set FIRECRAWL_API_KEY (lovework-agent/.env) or TAVILY_API_KEY (env, or "
            "~/.lovework/credentials/search/tavily_api_key), then retry."
        )


def _cache_path(url: str, cache_dir: Optional[Path] = None) -> Path:
    h = hashlib.sha256(url.encode()).hexdigest()[:16]
    return (cache_dir or config.CACHE_DIR) / f"page_{h}.md"


def _tavily_fetch(url: str) -> str:
    """Tavily `/extract` — the primary backend since the Exa credits ran out.

    Returns the page's markdown-ish text, or "" so the caller's fallback chain
    continues. `advanced` depth asks Tavily to render/wait rather than return a
    stub for script-heavy careers pages; it is the same reason Exa was primary.
    """
    try:
        from web_search import api_key

        key = api_key("TAVILY_API_KEY")
        if not key:
            return ""
        import httpx

        response = httpx.post(
            "https://api.tavily.com/extract",
            timeout=60,
            json={"urls": [url], "api_key": key, "extract_depth": "advanced"},
        )
        response.raise_for_status()
        payload = response.json()
        for result in payload.get("results") or []:
            content = (result.get("raw_content") or "").strip()
            if content:
                logger.info("Tavily fetched %s (%d chars)", url, len(content))
                return content
        failed = payload.get("failed_results") or []
        if failed:
            logger.warning("Tavily could not extract %s: %s", url, failed[:1])
            _PROVIDER_ERRORS["tavily"] = str(failed[:1])
        return ""
    except Exception as e:
        logger.warning(f"Tavily failed for {url}: {e}")
        _PROVIDER_ERRORS["tavily"] = str(e)
        return ""


def _deepseek_fetch(url: str) -> str:
    """DeepSeek Fetch (`POST /v1/tools/fetch`) — one page as Markdown.

    Primary fetcher from 2026-10-27 (was Firecrawl): same shape of answer at
    $0.75 per 1,000 successful pages, no SDK. Returns "" on any failure so the
    caller's fallback chain continues. Documented failure modes: 403 = rejected
    by the security policy, 404 = no usable content (blocked, unsupported
    content type, empty Markdown), 429 = the 25 QPS account cap or the
    per-domain cap, 402 = insufficient balance.
    """
    from web_search import api_key

    key = api_key("DEEPSEEK_API_KEY")
    if not key:
        return ""
    try:
        import httpx

        response = httpx.post(
            "https://api.deepseek.com/v1/tools/fetch",
            timeout=60,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"url": url},
        )
        response.raise_for_status()
        content = (response.json().get("content") or "").strip()
        if content:
            logger.info("DeepSeek fetched %s (%d chars)", url, len(content))
            _PROVIDER_ERRORS.pop("deepseek", None)
        return content
    except Exception as e:
        logger.warning(f"DeepSeek fetch failed for {url}: {e}")
        _PROVIDER_ERRORS["deepseek"] = str(e)
        return ""


def _firecrawl_fetch(url: str) -> str:
    """Firecrawl scrape. Paused by default (see PROVIDERS) but kept wired."""
    if not (_firecrawl_available and _firecrawl_app is not None):
        return ""
    try:
        result = _firecrawl_app.scrape(url, formats=["markdown"])
        md = getattr(result, "markdown", "") or ""
        if md:
            logger.info(f"Firecrawl fetched {url} ({len(md)} chars)")
            _PROVIDER_ERRORS.pop("firecrawl", None)
        return md
    except Exception as e:
        logger.warning(f"Firecrawl failed for {url}: {e}")
        _PROVIDER_ERRORS["firecrawl"] = str(e)
        return ""


def _http_fetch(url: str) -> str:
    """Last resort: plain HTTP + tag strip. 403s on Cloudflare-guarded pages."""
    try:
        import httpx

        resp = httpx.get(url, timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        from html import unescape

        text = unescape(resp.text)
        import re

        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.S)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S)
        text = re.sub(r"<[^>]+>", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        md = text.strip()
        logger.info(f"HTTP fallback fetched {url} ({len(md)} chars)")
        return md
    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        _PROVIDER_ERRORS["http"] = str(e)
        return ""


def _cache_entry_usable(url: str, text: str) -> bool:
    """Is a cached page something we can hand on as evidence?

    The same two judgements the live path makes, applied to what the cache holds:
    a WAF/Cloudflare block page is not a page, and LinkedIn's sign-in interstitial
    is not an advert. Both were cached before the guards existed, and a cache hit
    used to return them untouched — so the guard protected only *fresh* fetches
    while the poisoned entries kept being served (2026-11-09).

    Deliberately no size floor: a short page can be a real page (an advert's "no
    longer accepting applications" notice), and the callers already own their own
    floors (`tick.MIN_ADVERT_CHARS`). A guard that cannot be loaded returns True —
    the cache hit is the status quo, and a broken import must not turn every hit
    into a miss.
    """
    body = (text or "").strip()
    if not body:
        return False
    try:
        from enrichment import is_linkedin_authwall, looks_blocked

        if looks_blocked(body):
            return False
        if "linkedin.com" in (url or "").lower() and is_linkedin_authwall(body):
            return False
    except Exception as exc:                # never lose the fetch to the guard
        logger.debug("[crawler] cache validation skipped for %s: %s", url, exc)
    return True


def fetch_page(url: str, use_cache: bool = True,
               cache_dir: Optional[Path] = None) -> tuple[str, str]:
    """Fetch a URL and return `(markdown, provider)`. Uses the disk cache.

    The provider travels back with the text so provenance is *reported*, never
    guessed from config — enrichment stores it as `fetch_method`. A cache hit
    reports "cache": the cache holds markdown only, and naming the original
    provider from it would be a guess.
    """
    cache = _cache_path(url, cache_dir)
    if use_cache and cache.exists():
        text = cache.read_text(encoding="utf-8")
        # **A cached page is evidence too** (2026-11-09). The hit used to return
        # before both guards, so a wall cached *before* they existed — a Cloudflare
        # "you have been blocked" page, LinkedIn's sign-in interstitial — was served
        # as advert text forever, and the cache made that permanent: every later
        # fetch short-circuited here and the entry was never re-judged. A rejected
        # entry is left on disk (nothing is deleted) and overwritten by the live
        # fetch that follows it.
        if _cache_entry_usable(url, text):
            logger.info(f"Cache hit {url} ({len(text)} chars)")
            return text, "cache"
        logger.info("[crawler] cached page for %s is a wall (%d chars) — refetching",
                    url, len(text))

    md, used = "", ""
    # **A LinkedIn advert is fetched as a posting, not as a sign-in page**
    # (2026-11-04). LinkedIn answers an anonymous client with the login wall on the
    # alert-email tracking URLs (`/comm/jobs/view/<id>/?trackingId=…`) and with a
    # Cloudflare block elsewhere, so every provider below could only return
    # non-evidence for it — 873 characters, measured 2026-11-03. The public guest
    # endpoint serves the real posting for the same id (and the "No longer accepting
    # applications" notice when it has closed). Enrichment has always fetched
    # LinkedIn this way; the shared chain did not, and the tick's reviews and
    # `link_check` read the block page instead. One authority: the guest fetch.
    try:
        import enrichment

        job_id = enrichment.linkedin_job_id(url)
        if job_id:
            _started = time.monotonic()
            guest = enrichment.fetch_linkedin_guest(job_id)
            _record_attempt("linkedin-guest", url, guest, time.monotonic() - _started,
                            "" if guest else "guest endpoint returned nothing")
            if guest:
                if use_cache:
                    cache.write_text(guest, encoding="utf-8")
                return guest, "linkedin-guest"
    except Exception as exc:                    # never lose the fetch to this
        logger.warning("[crawler] LinkedIn guest fetch failed for %s: %s", url, exc)

    # **A browser-first host.** LinkedIn (and its kind) answers a logged-out
    # reader with a login wall — a *successful* fetch of the wrong page — so the
    # ordinary chain would never fall through to the browser. The host decides,
    # once, and the browser is used first when it is available (docs/46).
    if _requires_browser(url):
        try:
            import browser_fetch

            usable, why = browser_fetch.available()
            if usable:
                md = browser_fetch.fetch(url)
                if md:
                    used = "chrome"
                    if use_cache and md:
                        cache.write_text(md, encoding="utf-8")
                    return md, used
            else:
                logger.info("[crawler] %s wants a browser but none is usable: %s",
                            url, why)
        except Exception as exc:                    # never lose the fetch to this
            logger.warning("[crawler] browser fetch failed for %s: %s", url, exc)

    for provider in PROVIDERS:
        why = provider_exhausted(provider)
        if why:
            # Its quota is spent today: one line was logged when that was learned,
            # and every later page skips it without a round-trip.
            continue
        started = time.monotonic()
        _PROVIDER_ERRORS.pop(provider, None)
        if provider == "deepseek":
            md = _deepseek_fetch(url)
        elif provider == "tavily":
            md = _tavily_fetch(url)
        elif provider == "firecrawl":
            md = _firecrawl_fetch(url)
        elif provider == "http":
            md = _http_fetch(url)
        elif provider == "chrome":
            try:
                import browser_fetch

                md = browser_fetch.fetch(url)
            except Exception as exc:
                logger.warning("[crawler] browser fetch failed for %s: %s", url, exc)
                md = ""
        else:
            logger.warning(f"Unknown fetch provider {provider!r} — ignored")
            continue
        error = "" if md else _PROVIDER_ERRORS.get(provider, "")
        _record_attempt(provider, url, md, time.monotonic() - started, error)
        if md:
            used = provider
            break
        spent = _quota_message(error)
        if spent:
            mark_provider_exhausted(provider, spent)

    # **A browser can read what the open web cannot** (2026-10-14). When every
    # provider returns nothing, the page is not necessarily unreachable — it may
    # simply be one the open web will not serve. The EMBL-EBI Workday advert is the
    # measured case: Exa (live) returned 0 characters and plain HTTP was refused,
    # while Chrome read it in one pass and the page said *"The page you are looking
    # for doesn't exist"* — the closure, invisible to us, that LJ saw in his own
    # browser. Reading a **public** page needs no credential, so this uses the
    # public profile: nothing logged in is involved.
    if not md and os.getenv("LOVEWORK_BROWSER_FALLBACK", "1") not in ("0", "false", ""):
        try:
            import browser_fetch

            usable, why = browser_fetch.available(session=False)
            if usable:
                _started = time.monotonic()
                md = browser_fetch.fetch(url, session=False)
                _record_attempt("chrome-public", url, md, time.monotonic() - _started,
                                "" if md else "the browser returned nothing")
                if md:
                    used = "chrome-public"
            else:
                logger.info("[crawler] %s unreadable and no browser: %s", url, why)
        except Exception as exc:                    # never lose the fetch to this
            logger.warning("[crawler] browser fallback failed for %s: %s", url, exc)

    # **A block page is not a page** (2026-11-04): a Cloudflare challenge answers
    # HTTP 200 with "you have been blocked", so every provider and the browser
    # fallback report a *successful* fetch of a wall — 873 characters, measured
    # 2026-11-03, which a re-score turned into 7.1 → 0.0. Nothing is the honest
    # answer; a wall is not.
    if md:
        try:
            from enrichment import looks_blocked

            if looks_blocked(md):
                logger.info("[crawler] %s returned a block page (%d chars) — discarded",
                            url, len(md))
                md, used = "", ""
        except Exception as exc:                # never lose the fetch to this
            logger.warning("[crawler] block-page check failed for %s: %s", url, exc)

    if use_cache and md:
        cache.write_text(md, encoding="utf-8")
    return md, used


class CrawlDecision(BaseModel):
    """LLM decision after scanning a page."""

    found_jobs: bool = Field(description="Whether job listings were found on this page")
    job_listings: List[dict] = Field(default_factory=list, description="Any job listings found here")
    next_urls: List[str] = Field(default_factory=list, description="URLs to crawl next for jobs")
    confidence: int = Field(ge=0, le=10, description="Confidence in this assessment")
    reasoning: str = Field(description="Brief reasoning for the decision")


class ExtractedJob(BaseModel):
    """Structured job listing extracted by LLM."""

    title: str = Field(description="Job title")
    team: Optional[str] = Field(default=None, description="Team or department")
    location: Optional[str] = Field(default=None, description="Location or remote policy")
    url: Optional[str] = Field(default=None, description="Direct link to the job posting")
    #: **Where the link came from**, because a scraped link and a guessed one are
    #: not the same claim (2026-10-15). `page` = the page carried it. `derived` =
    #: the page is a JavaScript list with no hrefs and this is the role's own URL
    #: *constructed* from the site's pattern — which must be **verified before it is
    #: trusted**, or dropped. Without the distinction we stored
    #: `https://posthog.com/careers` as an advert's link: the careers page, offered
    #: as if it were the job.
    url_source: str = Field(default="page",
                            description="'page' when the page carried the link, "
                                        "'derived' when it was constructed")
    description_snippet: Optional[str] = Field(default=None, description="1-2 sentence summary")
    requirements_snippet: Optional[str] = Field(default=None, description="Key requirements mentioned")
    employment_type: Optional[str] = Field(default=None, description="Full-time, part-time, contract, internship, residency, fellowship")
    posted_date: Optional[str] = Field(default=None, description="When the job was posted (ISO date or relative like '2 weeks ago')")
    #: The **role's own page**, when we went and read it (2026-10-15). A careers
    #: *list* says a role exists; the advert says what it is. PostHog's pack was
    #: built from the list, so nothing in it mentioned Rust, CUDA, vision or what the
    #: company does — all of which are on the role's page and none of which is on the
    #: list. Bounded, because this is one fetch per role.
    advert_text: Optional[str] = Field(default=None,
                                       description="Text of the role's own advert page")


# ── Filters ──────────────────────────────────────────────────────────────

# Cities/regions that count as UK/Europe (LJ is based in London)
UK_EU_KEYWORDS = (
    "london", "uk", "united kingdom", "england", "scotland", "wales", "northern ireland",
    "edinburgh", "cambridge", "oxford", "manchester", "bristol", "dublin", "ireland",
    "paris", "france", "berlin", "germany", "munich", "amsterdam", "netherlands",
    "zurich", "geneva", "switzerland", "stockholm", "sweden", "oslo", "norway",
    "copenhagen", "denmark", "helsinki", "finland", "barcelona", "spain", "madrid",
    "milan", "italy", "vienna", "austria", "warsaw", "poland", "prague", "czech",
    "lisbon", "portugal", "brussels", "belgium", "remote", "anywhere", "global",
    "worldwide", "europe", "emea",
)

# Hard rejections — US-only or Remote (US)
US_KEYWORDS = (
    "united states", " u.s.", "u.s.a", "usa", "us only", "us-based",
    "san francisco", "san jose", "palo alto", "mountain view", "menlo park",
    "cupertino", "sunnyvale", "santa clara", "berkeley", "oakland", "los angeles",
    "san diego", "seattle", "bellevue", "redmond", "austin", "boston", "new york",
    "brooklyn", "chicago", "denver", "portland", "atlanta", "miami", "dallas",
    "houston", "philadelphia", "washington dc", "washington, dc", "north america",
    "americas", "canada", "toronto", "vancouver", "montreal",
)


#: How much of a role's own page we hand to the matcher.
#:
#: **Measured, not guessed** (2026-10-15). The first attempt took the first 4,000
#: characters and lost the advert: PostHog's page opens with navigation — the role
#: name, then *other* roles — so the text that says `Rust`, `CUDA`, `vision`,
#: `self-driving` sits further down (the whole page is 8,980 characters, and those
#: words appear 2, 1, 2, 1 and 1 times in it). A per-role page *is* the advert, so
#: the honest bound is the page, not a prefix of it.
EVIDENCE_CHARS = int(os.getenv("LOVEWORK_EVIDENCE_CHARS", "12000"))

#: What the drill-down keeps of a role's page — the same bound, for the same reason.
DRILLDOWN_CHARS = int(os.getenv("LOVEWORK_DRILLDOWN_CHARS", "12000"))


def job_evidence_text(job: "ExtractedJob", limit: int | None = None) -> str:
    """**The text we judge an advert by** — one authority, used by every source.

    Before this, every source built the matcher's `description` from the extractor's
    20-word snippets (`description_snippet`, `requirements_snippet`,
    `employment_type`). So the score, and the eligibility kills beside it, read a
    *summary of a list* rather than the advert: PostHog's pack could not mention
    Rust, CUDA, vision or self-driving cars, because none of that is on the careers
    page the snippets came from (LJ's spot check, 2026-10-15).

    The drill-down (`_attach_advert_text`) reads the role's own page; this is what
    hands it to the judgement. Falls back to the snippets when we have no page, so
    every existing source keeps working unchanged.
    """
    limit = EVIDENCE_CHARS if limit is None else limit
    advert = (getattr(job, "advert_text", None) or "").strip()
    if advert:
        return advert[:limit]
    return " ".join(filter(None, [
        job.description_snippet, job.requirements_snippet, job.employment_type,
    ]))[:limit]


def is_location_acceptable(location: Optional[str]) -> bool:
    """Return True if the job's location is UK, EU, or explicitly Remote/Global.

    Location strings can be semicolon-separated (e.g. "Berkeley Office; Remote (International);
    Remote (US)"). We split on ; and | and check each option. A job is acceptable if AT LEAST
    ONE option is UK/EU/global-remote (even if others are US-only).
    """
    if not location:
        # No location info — be conservative, allow it (LLM may filter later)
        return True

    # Split into individual options
    options = re.split(r"[;|]", location)
    options = [o.strip() for o in options if o.strip()]

    if not options:
        return True

    # A job is acceptable if ANY option is UK/EU-eligible
    for opt in options:
        if _is_single_location_ok(opt):
            return True

    # If we get here, none of the options were UK/EU-eligible
    return False


def _is_single_location_ok(loc: str) -> bool:
    """Check a single location option (no semicolons)."""
    loc_lower = loc.lower().strip()

    # Hard reject if US-only signals present
    for kw in US_KEYWORDS:
        if kw in loc_lower:
            return False

    # Reject "Remote" without explicit EU/global qualifier (default = US-only)
    if "remote" in loc_lower or "hybrid" in loc_lower:
        for kw in ("eu", "emea", "europe", "global", "worldwide", "anywhere",
                   "uk", "european", "international"):
            if kw in loc_lower:
                return True
        # Plain "Remote" or "Hybrid" with no qualifier — reject
        return False

    # Accept if any UK/EU signal present
    for kw in UK_EU_KEYWORDS:
        if kw in loc_lower:
            return True

    # Unknown — accept (LLM matcher will catch US later)
    return True


def is_recent(posted_date: Optional[str], max_weeks: int = 4) -> bool:
    """Return True if the job was posted within the last max_weeks weeks."""
    if not posted_date:
        # No date info — be conservative and accept (date may not be on the page)
        return True
    from datetime import datetime, timedelta

    text = posted_date.lower().strip()
    now = datetime.now()

    # Relative formats: "2 weeks ago", "3 days ago", "1 month ago"
    import re

    rel = re.match(r"(\d+)\s*(day|week|month)s?\s*ago", text)
    if rel:
        n = int(rel.group(1))
        unit = rel.group(2)
        if unit == "day":
            return n <= max_weeks * 7
        if unit == "week":
            return n <= max_weeks
        if unit == "month":
            return n <= 1  # ~1 month ≈ 4 weeks

    # ISO date: 2026-06-15
    try:
        dt = datetime.fromisoformat(posted_date)
        return (now - dt) <= timedelta(weeks=max_weeks)
    except (ValueError, TypeError):
        pass

    # "Just posted", "Today", "Yesterday"
    if any(t in text for t in ("today", "just posted", "yesterday", "new")):
        return True

    return True  # Unknown format — accept


def filter_jobs(jobs: List["ExtractedJob"], max_weeks: int = 4) -> List["ExtractedJob"]:
    """Filter jobs by recency and location. Returns only acceptable jobs."""
    out = []
    for j in jobs:
        if not is_location_acceptable(j.location):
            logger.debug(f"Filter: dropped (location) {j.title} @ {j.location}")
            continue
        if not is_recent(j.posted_date, max_weeks):
            logger.debug(f"Filter: dropped (stale) {j.title} posted {j.posted_date}")
            continue
        out.append(j)
    return out


class SmartCrawler:
    """Iterative LLM-guided crawler for a single organization.

    Two prompt implementations:
    - use_dspy=False (default): hand-written prompts via LLMClient.structured()
    - use_dspy=True: DSPy typed signatures (compileable, optimisable)

    Both produce the same outputs (CrawlDecision, List[ExtractedJob]).
    """

    def __init__(
        self, llm: LLMClient, use_dspy: bool = False, cache_dir: Optional[Path] = None
    ):
        self.llm = llm
        self.visited: set = set()
        self.use_dspy = use_dspy
        self.cache_dir = cache_dir
        self._dspy = None
        if use_dspy:
            try:
                from dspy_signatures import SmartCrawlerDSPy, configure_dspy
                configure_dspy()
                self._dspy = SmartCrawlerDSPy()
                logger.info("SmartCrawler using DSPy signatures")
            except Exception as e:
                logger.warning(f"DSPy not available, falling back to legacy prompts: {e}")
                self.use_dspy = False

    def crawl_org(
        self,
        org_name: str,
        seed_urls: List[str],
        goal: str = "Find open job or research positions suitable for an experienced ML/AI researcher.",
        max_pages: int = config.MAX_PAGES_PER_ORG,
        max_depth: int = config.MAX_DEPTH,
    ) -> List[ExtractedJob]:
        """
        Crawl an organization's site iteratively to find job listings.
        Returns deduplicated ExtractedJob objects.
        """
        self.visited = set()
        seen_urls = set()
        queue: List[tuple] = []
        for url in seed_urls:
            if url not in seen_urls:
                seen_urls.add(url)
                queue.append((url, 0))
        all_jobs: List[ExtractedJob] = []
        pages_crawled = 0

        while queue and pages_crawled < max_pages:
            url, depth = queue.pop(0)
            if url in self.visited or depth > max_depth:
                continue
            self.visited.add(url)

            logger.info(f"[{org_name}] Crawling {url} (depth={depth})")
            content, provider = fetch_page(url, cache_dir=self.cache_dir)
            if not content:
                continue
            logger.info(f"[{org_name}] fetched via {provider} ({len(content)} chars)")

            # Truncate very long content for LLM
            truncated = content[:12000]

            # Ask LLM what to do with this page
            decision = self._ask_decision(org_name, url, truncated, goal)
            pages_crawled += 1

            if decision.found_jobs:
                # Try extraction even if job_listings is empty (LLM may have
                # returned strings, which fail Pydantic validation)
                jobs = self._extract_jobs_from_page(org_name, url, truncated, goal)
                jobs = self._with_links(org_name, url, jobs, goal)
                jobs = self._clean_job_links(jobs, url)
                jobs = [self._verify_derived_url(job, url) for job in jobs]
                all_jobs.extend(jobs)
            elif decision.confidence == 0 and any(
                kw in url.lower() for kw in ("careers", "jobs", "positions", "openings", "fellows", "residency")
            ):
                # Parse failure on a careers page — try extraction anyway
                logger.info(f"[{org_name}] Parse-failure fallback: extracting from {url}")
                jobs = self._extract_jobs_from_page(org_name, url, truncated, goal)
                jobs = self._with_links(org_name, url, jobs, goal)
                jobs = self._clean_job_links(jobs, url)
                jobs = [self._verify_derived_url(job, url) for job in jobs]
                all_jobs.extend(jobs)

            for next_url in decision.next_urls[:3]:  # Limit branching
                absolute = urljoin(url, next_url)
                # Sanity filter: same domain or known careers domains
                if absolute not in self.visited and absolute not in seen_urls and self._is_sensible_url(absolute, seed_urls):
                    seen_urls.add(absolute)
                    queue.append((absolute, depth + 1))

            time.sleep(config.REQUEST_DELAY_SECONDS)

        # **The drill-down**: a list says a role exists; the advert says what it is.
        # One fetch per role, bounded, and never fatal.
        all_jobs = self._attach_advert_text(all_jobs)

        # Deduplicate by title+company
        seen = set()
        deduped = []
        for job in all_jobs:
            key = (org_name.lower(), job.title.lower())
            if key not in seen:
                seen.add(key)
                deduped.append(job)

        # Filter by recency and location
        before = len(deduped)
        deduped = filter_jobs(deduped, max_weeks=config.MAX_JOB_AGE_WEEKS)
        filtered = before - len(deduped)

        logger.info(
            f"[{org_name}] Crawled {pages_crawled} pages, found {len(deduped)} unique jobs "
            f"({filtered} filtered out as stale or US-only)"
        )
        return deduped

    def _ask_decision(self, org_name: str, url: str, content: str, goal: str) -> CrawlDecision:
        """Ask LLM to assess a page and decide next steps."""
        if self.use_dspy and self._dspy is not None:
            try:
                pred = self._dspy.decide_next(
                    org_name=org_name, url=url, content=content, goal=goal,
                )
                return CrawlDecision(
                    found_jobs=bool(getattr(pred, "found_jobs", False)),
                    job_listings=list(getattr(pred, "job_listings", []) or []),
                    next_urls=list(getattr(pred, "next_urls", []) or []),
                    confidence=int(getattr(pred, "confidence", 0) or 0),
                    reasoning=str(getattr(pred, "reasoning", "") or ""),
                )
            except Exception as e:
                logger.warning(f"DSPy decide failed for {url}: {e}, falling back to legacy")
                # fall through to legacy path

        # Legacy path: the prompt comes from `prompts.py` (one place, 2026-10-15).
        prompt = prompts.crawl_decision_prompt(goal, org_name, url, content)
        messages = [{"role": "user", "content": prompt}]
        try:
            return self.llm.structured(
                messages, CrawlDecision,
                context=f"[{org_name}] decision: {url}",
            )
        except Exception:
            logger.warning(f"Structured decision failed for {url}, continuing")
            return CrawlDecision(found_jobs=False, next_urls=[], confidence=0, reasoning="parse failed")

    def _attach_advert_text(self, jobs: List[ExtractedJob]) -> List[ExtractedJob]:
        """Read each role's **own** page, so the evidence is the advert.

        LJ, 2026-10-15, on the PostHog pack: *"they are working on self-driving cars,
        and doing vision … We should have realised what this is about and highlighted
        the above."* We could not, because the pack was built from the careers *list*
        — which names the role and says nothing about it. The role's own page says
        `Rust`, `CUDA`, `Transformers`, "this is not a Jupyter notebook job", and what
        the company ships; the list says none of it.

        Bounded by `LOVEWORK_DRILLDOWN_LIMIT` (default 12 per organisation) because it
        is one fetch per role, and never fatal: a page we cannot read leaves the field
        empty and the rest of the crawl is unaffected.
        """
        limit = int(os.getenv("LOVEWORK_DRILLDOWN_LIMIT", "12"))
        if limit <= 0:
            return jobs
        read = 0
        for job in jobs:
            if read >= limit:
                break
            if not job.url or job.advert_text:
                continue
            try:
                text, _provider = fetch_page(job.url, cache_dir=self.cache_dir)
            except Exception as exc:              # never lose the crawl to this
                logger.info("[crawler] advert page %s unreadable: %s", job.url, exc)
                continue
            read += 1
            if text:
                job.advert_text = text[:DRILLDOWN_CHARS]
        if read:
            logger.info("[crawler] read %d role page(s) for their own advert text", read)
        return jobs

    def _clean_job_links(self, jobs: List[ExtractedJob],
                         page_url: str) -> List[ExtractedJob]:
        """A job's link must be the **advert**, not the page we found it on.

        The PostHog pack's link was `https://posthog.com/careers` — the careers
        page, offered as if it were the job (2026-10-15). When a page lists roles
        and the extractor has nothing better to offer, the honest answer is *no
        link*: the row already says `NO LINK` and the pack says `_not retained_`,
        which a reader can act on, while a link to a list is a dead end that looks
        alive. Deterministic, because it compares two strings we hold.
        """
        from lead_identity import advert_url_key

        page_key = advert_url_key(page_url or "")
        for job in jobs:
            if not job.url:
                continue
            job_key = advert_url_key(job.url)
            if job_key and (job_key == page_key or job_key.rstrip("/") == page_key):
                logger.info("[crawler] %s is the page itself, not the advert: dropped",
                            job.url)
                job.url = None
        return jobs

    def _with_links(self, org_name: str, url: str, jobs: List[ExtractedJob],
                    goal: str) -> List[ExtractedJob]:
        """Recover the links of a page that gave us titles but no hrefs.

        **The measurement, not a host list** (2026-10-15). A JavaScript-only careers
        site hands the open web its text: `Enigma - Jobs` came back as 3,332
        characters carrying every role title and **no links at all**, because the
        links are anchors and the markdown conversion drops them. We kept the titles
        and lost the hrefs, and LJ's first GO row read `(no advert link)` — an
        advert the principal cannot apply to.

        The rendered DOM still has the anchors: Chrome read the same page and gave
        **15 links including every `/job/<role>/`**. So the rule is a *question about
        what we received* — "did this page give us any link?" — and not a list of
        sites, which would have to be maintained for ever.

        Costs one browser read per link-less jobs page, and only when the browser is
        usable.
        """
        if not jobs or any((job.url or "").strip() for job in jobs):
            return jobs
        try:
            import browser_fetch

            usable, why = browser_fetch.available(session=False)
            if not usable:
                logger.info("[%s] %s: no links and no browser (%s)", org_name, url, why)
                return jobs
            rendered = browser_fetch.fetch_linked_text(url, session=False)
        except Exception as exc:                    # never lose the extraction to this
            logger.warning("[%s] browser link recovery failed for %s: %s",
                           org_name, url, exc)
            return jobs
        if not rendered:
            return jobs
        retried = self._extract_jobs_from_page(org_name, url, rendered[:12000], goal)
        linked = [job for job in retried if (job.url or "").strip()]
        if linked:
            logger.info("[%s] %s: the open web gave no links; the browser found %d",
                        org_name, url, len(linked))
            return linked
        logger.info("[%s] %s: the browser found no links either", org_name, url)
        return jobs

    def _extract_jobs_from_page(self, org_name: str, url: str, content: str, goal: str) -> List[ExtractedJob]:
        """Ask LLM to extract structured job listings from a page known to have jobs."""
        if self.use_dspy and self._dspy is not None:
            try:
                jobs = self._dspy.extract_jobs(
                    org_name=org_name, url=url, content=content, goal=goal,
                )
                # Convert dicts to ExtractedJob
                result: List[ExtractedJob] = []
                for j in jobs or []:
                    if isinstance(j, dict):
                        result.append(ExtractedJob(**j))
                    elif isinstance(j, ExtractedJob):
                        result.append(j)
                return result
            except Exception as e:
                logger.warning(f"DSPy extract failed for {url}: {e}, falling back to legacy")
                # fall through

        # Legacy path: hand-written prompt + LLMClient.structured()
        prompt = prompts.extract_jobs_prompt(org_name, url, goal, content)
        messages = [{"role": "user", "content": prompt}]
        try:
            class JobList(BaseModel):
                jobs: List[ExtractedJob]

            result = self.llm.structured(
                messages,
                JobList,
                # No explicit cap: the budget comes from config.LLM_MAX_TOKENS,
                # which is a generous ceiling on purpose (2026-10-05). A busy
                # careers page can list more jobs than 8,192 tokens of JSON, and
                # the model stops as soon as the list is written.
                context=f"[{org_name}] extract: {url}",
            )
            return result.jobs
        except Exception as e:
            logger.warning(f"Job extraction failed for {url}: {e}")
            return []

    def _verify_derived_url(self, job: ExtractedJob, page_url: str) -> ExtractedJob:
        """A constructed link is a **claim**, and a claim needs checking.

        LJ, 2026-10-15: PostHog's careers page is a JavaScript list — the roles are
        click-handlers, so the fetched markdown carries **0 links** and the browser
        finds **3 anchors, none of them a role**. The per-role URL exists
        (`/careers/ai-research-engineer`) and can only be *derived* from the title.
        A derived link that is not opened is a guess, and a wrong link is worse than
        no link: it sends the principal to a page that is not the advert. So the URL
        is fetched, and the page must carry the role's own words; otherwise the link
        is cleared and the row honestly reads `NO LINK`.
        """
        if not job.url or job.url_source != "derived":
            return job
        try:
            text, _provider = fetch_page(job.url, cache_dir=self.cache_dir,
                                         use_cache=False)
        except Exception as exc:
            logger.info("[crawler] derived link %s could not be opened: %s",
                        job.url, exc)
            text = ""
        if not text:
            logger.info("[crawler] derived link %s did not open; dropping it", job.url)
            job.url = None
            return job
        # The role's own words, folded: a title is written once by the page and once
        # by the extractor, so compare on alphanumerics.
        def _fold(value: str) -> str:
            return re.sub(r"[^a-z0-9]", "", (value or "").lower())

        if _fold(job.title) and _fold(job.title) not in _fold(text):
            logger.info("[crawler] %s does not carry %r; dropping the derived link",
                        job.url, job.title[:40])
            job.url = None
        return job

    def _is_sensible_url(self, url: str, seed_urls: List[str]) -> bool:
        """Basic sanity filter for URLs."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if any(x in url.lower() for x in ("mailto:", "tel:", "javascript:", "#")):
            return False
        # Allow same domain as seeds
        seed_domains = {urlparse(u).netloc for u in seed_urls}
        if parsed.netloc in seed_domains:
            return True
        # Allow known ATS/careers domains
        allowed_hosts = ("jobs.ashbyhq.com", "boards.greenhouse.io", "jobs.lever.co",
                         "apply.workable.com", "careers.", "jobs.", "boards.")
        if any(h in parsed.netloc for h in allowed_hosts):
            return True
        return False
