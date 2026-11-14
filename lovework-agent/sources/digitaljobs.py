"""
Source: Digital Jobs MCP (https://www.digitaljobs.com/skill.md).

Digital Jobs exposes a public, anonymous MCP JSON-RPC endpoint for job
search (`search_jobs`) and a feed of AI-agent opportunities
(`list_ai_opportunities`). No account, API key, or OAuth token is needed
for the public tools; this source uses only those.

Description strategy (explicit): the API returns structured records only
(normalised GBP salary, skills, seniority, categories) and has no job-detail
tool, so the advert excerpt here is *synthesized from structured fields* —
it is discovery fallback text, NOT the scoring text. Scoring evidence comes
from the pipeline-wide EnrichingMatcher, which fetches the advert URL
(cheap HTTP first; the pages sit behind a Cloudflare challenge so it
normally lands on the Firecrawl fallback — verified: ~10K chars of real
advert text per page) and the matcher scores on that. Do not add scraping
to this source; keep it API-only.

Filter names are validated against the live `tools/list` inputSchema
(`additionalProperties: false` — the server rejects unknown fields, so this
module drops them client-side before sending). Schema verified 2026-09-04;
if the server adds fields, extend `_sanitize_filters` from the live schema,
not from memory.

Search queries are principal-maintained under
profiles/<principal>/digitaljobs_searches.yaml (same policy shape as
harnham_searches.yaml). A principal without a list is skipped rather than
inheriting another principal's queries. The MCP endpoint is the
discovery surface; each advert's own URL is used as discovery provenance.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

import config
from crawler import SmartCrawler
from job_registry import JobRegistry
from matcher import JobMatcher
from wiki_store import WikiEntry, match_fields

logger = logging.getLogger(__name__)

MCP_URL = os.getenv(
    "LOVEWORK_DIGITALJOBS_MCP_URL", "https://www.digitaljobs.com/mcp"
)
MCP_PROTOCOL_VERSION = "2026-07-28"
CLIENT_INFO = {"name": "lovework", "version": "0.1.0"}

SEARCHES_FILENAME = "digitaljobs_searches.yaml"

# Record fields the provider documents, in the order the report presents them.
RECORD_FIELDS = (
    "title", "company", "url", "location", "description", "salary_text",
    "seniority", "work_mode", "employment_type", "skills", "categories",
    "company_industries",
)
MAX_SEARCHES_PER_RUN = int(os.getenv("LOVEWORK_DIGITALJOBS_MAX_SEARCHES", "10"))
MAX_PAGES_PER_SEARCH = int(os.getenv("LOVEWORK_DIGITALJOBS_MAX_PAGES", "4"))
PAGE_SIZE = min(int(os.getenv("LOVEWORK_DIGITALJOBS_PAGE_SIZE", "50")), 50)
REQUEST_TIMEOUT = int(os.getenv("LOVEWORK_DIGITALJOBS_TIMEOUT", "30"))
# The anonymous endpoint rate-limits per short window: a 10-search burst with
# pagination returned HTTP 429 mid-run on 2026-09-11. Space the requests, and
# retry a 429 with backoff — otherwise the search is silently dropped for the run.
REQUEST_DELAY_SECONDS = float(os.getenv("LOVEWORK_DIGITALJOBS_REQUEST_DELAY", "1.0"))
MAX_RETRIES = int(os.getenv("LOVEWORK_DIGITALJOBS_MAX_RETRIES", "3"))
RETRY_BACKOFF_SECONDS = float(os.getenv("LOVEWORK_DIGITALJOBS_RETRY_BACKOFF", "10.0"))
MAX_RETRY_WAIT_SECONDS = float(os.getenv("LOVEWORK_DIGITALJOBS_MAX_RETRY_WAIT", "90"))

# Server rejects unknown fields and wrong JSON types outright; sanitise
# client-side so one stale YAML row cannot kill the whole search.
_FILTER_STRS = {"role", "category", "seniority", "location", "keywords", "company"}
_FILTER_ENUMS = {
    "work_mode": {"remote", "hybrid", "onsite"},
    "employment_type": {"full_time", "part_time", "contract", "freelance"},
}
_FILTER_BOOLS = {"remote", "hybrid", "relax"}
_FILTER_INTS = {"min_salary_gbp", "max_salary_gbp"}
_SKILLS_MAX = 20
_SKILL_MAX_LEN = 80


# Starter searches per principal. Only principals listed here seed a file; a
# principal with no starter list must never inherit another principal's queries
# (LJ's are AI/ML-oriented, VJ's are statistics/pricing/actuarial/risk).
_STARTER_SEARCHES: Dict[str, List[dict]] = {
    "lj": [
        {
            "name": "agentic-engineer-london",
            "filters": {"keywords": "agentic engineer", "location": "London"},
        },
        {
            "name": "ai-ml-engineer-london",
            "filters": {"role": "AI Engineer", "location": "London"},
        },
        {
            "name": "python-llm-remote",
            "filters": {"skills": ["Python", "LLM"], "remote": True},
        },
    ],
    "vj": [
        # Tuned 2026-09-11 against live result counts: the anonymous API is
        # rate-limited, so keep each search narrow. Broad keywords flood
        # (~160-200 adverts each); role/location pairs stay in the tens.
        {"name": "pricing-analyst-london", "filters": {"role": "Pricing Analyst", "location": "London"}},
        {"name": "pricing-data-analyst", "filters": {"keywords": "pricing data analyst"}},
        {"name": "risk-analyst-london", "filters": {"role": "Risk Analyst", "location": "London"}},
        {"name": "quantitative-analyst-london", "filters": {"keywords": "quantitative analyst", "location": "London"}},
        {"name": "actuarial-analyst-remote", "filters": {"keywords": "actuarial analyst", "remote": True}},
        {"name": "catastrophe-analyst", "filters": {"keywords": "catastrophe analyst"}},
        {"name": "insurance-pricing-remote", "filters": {"keywords": "insurance pricing", "remote": True}},
        {"name": "statistician-remote", "filters": {"keywords": "statistician", "remote": True}},
        {"name": "sports-analytics", "filters": {"keywords": "sports analytics"}},
    ],
}


def _profile_dir(profile_name: str) -> Path:
    return config.PROFILES_DIR / profile_name.lower()


def _searches_path(profile_name: str) -> Path:
    return _profile_dir(profile_name) / SEARCHES_FILENAME


def _seed_searches(profile_name: str) -> None:
    """Create a principal's starter Digital Jobs search list. Idempotent.

    Principals without a starter list get no file: the source then skips rather
    than silently searching with another principal's queries.
    """
    starters = _STARTER_SEARCHES.get(profile_name.lower())
    if not starters:
        return
    path = _searches_path(profile_name)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(starters, sort_keys=False), encoding="utf-8")
    logger.info(f"[digitaljobs] Seeded search list for {profile_name} at {path}")


def _load_searches(profile_name: str) -> List[dict]:
    _seed_searches(profile_name)
    path = _searches_path(profile_name)
    if not path.exists():
        logger.warning(f"[digitaljobs] No search list for {profile_name} ({path}); skipping.")
        return []
    rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [r for r in rows if isinstance(r, dict)]


def _sanitize_filters(raw: Any) -> Dict[str, Any]:
    """Validate a YAML filter dict against the documented search_jobs schema.

    Unknown fields and wrong-typed values are dropped with a warning rather
    than raising: the server rejects whole requests containing them.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in raw.items():
        if value is None or value == "":
            continue
        try:
            if key in _FILTER_STRS:
                out[key] = str(value)
            elif key in _FILTER_ENUMS:
                norm = str(value).lower()
                if norm not in _FILTER_ENUMS[key]:
                    raise ValueError(f"{value!r} not in {sorted(_FILTER_ENUMS[key])}")
                out[key] = norm
            elif key in _FILTER_BOOLS:
                out[key] = bool(value)
            elif key in _FILTER_INTS:
                out[key] = int(value)
            elif key == "skills":
                skills = [str(s)[:_SKILL_MAX_LEN] for s in list(value)[:_SKILLS_MAX]]
                if skills:
                    out[key] = skills
            else:
                logger.warning(f"[digitaljobs] Dropping unknown filter {key!r}")
                continue
        except (TypeError, ValueError) as e:
            logger.warning(f"[digitaljobs] Dropping filter {key}={value!r}: {e}")
    return out


def _retry_delay_seconds(err: urllib.error.HTTPError, attempt: int) -> float:
    """Wait before retrying a 429: honour Retry-After, else linear backoff."""
    raw = err.headers.get("Retry-After") if err.headers else None
    if raw:
        try:
            return max(0.0, min(float(raw), MAX_RETRY_WAIT_SECONDS))
        except (TypeError, ValueError):
            pass
    return min(RETRY_BACKOFF_SECONDS * attempt, MAX_RETRY_WAIT_SECONDS)


# ── Agent-friendliness instrumentation ───────────────────────────────────
# The provider advertises an agent-first MCP server (public, anonymous,
# structured records, cursor pagination). This records how that promise holds
# up on each run so `digitaljobs_report.py` can write a verdict. Process-local
# and reset per run: the source runs sequentially, one run at a time.
_STATS: Dict[str, Any] = {}


def start_stats() -> None:
    """Begin a fresh instrumentation window."""
    _STATS.clear()
    _STATS.update({
        "requests": 0,
        "ok": 0,
        "http_errors": {},
        "retries": 0,
        "rate_limited": 0,
        "retry_after_seconds": [],
        "backoff_seconds": 0.0,
        "latencies_ms": [],
        "tools_list": {},
        "response_keys": {},
        "searches": [],
        "records": 0,
        "records_with_url": 0,
        "records_with_title": 0,
        "duplicate_urls": 0,
        "fields": {},
        "relaxed": 0,
        "dropped_filters": 0,
    })


def stats_snapshot() -> Dict[str, Any]:
    """A copy of the current window (safe to persist)."""
    payload = json.loads(json.dumps(_STATS))
    latencies = payload.get("latencies_ms") or []
    if latencies:
        ordered = sorted(latencies)
        payload["latency_ms_p50"] = ordered[len(ordered) // 2]
        payload["latency_ms_max"] = ordered[-1]
    return payload


# A provider can return a URL that is not a public address. On 2026-09-12 the
# Digital Jobs API returned 28 records pointing at `digitaljobslive.local:55021`
# (their staging host) mixed in with real `digitaljobs.com` URLs — enough to put
# .local links into the registry, the wiki index and the reports, where every one
# of them failed enrichment with "Temporary failure in name resolution" and
# link_check marked them unreachable. A well-formed-URL check passes them; a
# *resolvable-in-public* check does not. The lead itself is real, so the record is
# kept and only the unusable URL is dropped — visibly, never silently.
_PRIVATE_HOST_RE = re.compile(
    r"(^|\.)(local|localhost|internal|intranet|corp|lan|home\.arpa|test|invalid|example)$",
    re.I,
)
_PRIVATE_IP_RE = re.compile(
    r"^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|0\.0\.0\.0$|\[?::1\]?$)"
)


def public_url(url: str) -> Tuple[str, str]:
    """Return (usable_url, rejected_host).

    `rejected_host` is non-empty when the URL points somewhere only the provider
    can reach; the caller drops the URL and reports it.
    """
    text = (url or "").strip()
    if not text:
        return "", ""
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return "", ""
    if not host:
        return "", ""
    if _PRIVATE_HOST_RE.search(host) or _PRIVATE_IP_RE.match(host):
        return "", urlsplit(text).netloc or host
    return text, ""


def _record_request(status: Optional[int], latency_ms: int, error: str = "") -> None:
    if not _STATS:
        return
    _STATS["requests"] += 1
    _STATS["latencies_ms"].append(latency_ms)
    if error:
        key = f"http_{status}" if status else "transport"
        _STATS["http_errors"][key] = _STATS["http_errors"].get(key, 0) + 1
    else:
        _STATS["ok"] += 1


def _post(payload: Dict[str, Any], *, method: str, tool: str = "") -> Dict[str, Any]:
    """POST one JSON-RPC message and return the raw envelope.

    `tool` keeps the operator-facing label a tool name (the log said
    `HTTP 429 for search_jobs` before folding tools/list in — never degrade it
    to the bare method).
    """
    label = tool or method
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": method,
            **({"Mcp-Name": tool} if tool else {}),
            # Cloudflare 1010 blocks default urllib/python-requests UAs.
            "User-Agent": "lovework-agent/0.1 (+https://lovework.be)",
        },
        method="POST",
    )
    # Pace every request — searches and pages alike. Most searches return a
    # single page, so sleeping only *between* pages never engages.
    if REQUEST_DELAY_SECONDS:
        time.sleep(REQUEST_DELAY_SECONDS)

    body: Any = None
    for attempt in range(1, MAX_RETRIES + 1):
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            _record_request(getattr(resp, "status", 200), int((time.monotonic() - started) * 1000))
            break
        except urllib.error.HTTPError as e:
            detail = e.read()[:200]
            latency_ms = int((time.monotonic() - started) * 1000)
            _record_request(e.code, latency_ms, error=str(detail))
            if e.code == 429:
                _STATS["rate_limited"] = _STATS.get("rate_limited", 0) + 1
            if e.code != 429 or attempt >= MAX_RETRIES:
                raise RuntimeError(f"Digital Jobs MCP HTTP {e.code} for {label}: {detail}") from e
            delay = _retry_delay_seconds(e, attempt)
            _STATS["retries"] = _STATS.get("retries", 0) + 1
            header = e.headers.get("Retry-After") if e.headers else None
            if header:
                _STATS.setdefault("retry_after_seconds", []).append(header)
            logger.warning(
                f"[digitaljobs] HTTP 429 for {label}; retry {attempt}/{MAX_RETRIES - 1} in {delay:.0f}s"
            )
            # Backoff is our own pacing, not the server's latency: count it
            # separately so the latency picture stays honest.
            _STATS["backoff_seconds"] = round(
                _STATS.get("backoff_seconds", 0.0) + float(delay), 1
            )
            time.sleep(delay)
        except urllib.error.URLError as e:
            _record_request(None, int((time.monotonic() - started) * 1000), error=str(e.reason))
            raise RuntimeError(f"Digital Jobs MCP unreachable for {label}: {e.reason}") from e
    return body if isinstance(body, dict) else {}


def tools_list() -> Dict[str, Any]:
    """Ask the server what it offers. Tool availability is a kill-switch."""
    try:
        body = _post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                        "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            },
            method="tools/list",
        )
    except Exception as exc:
        if _STATS:
            _STATS["tools_list"] = {"ok": False, "error": str(exc)[:200], "tools": []}
        logger.info("[digitaljobs] tools/list unavailable: %s", exc)
        return _STATS.get("tools_list", {"ok": False, "tools": []})
    tools = [
        t.get("name") for t in (body.get("result", {}).get("tools") or [])
        if isinstance(t, dict)
    ]
    if _STATS:
        _STATS["tools_list"] = {"ok": True, "tools": tools}
    logger.info("[digitaljobs] tools/list offers %d tool(s): %s", len(tools), ", ".join(tools))
    return _STATS.get("tools_list", {"ok": True, "tools": tools})


def _mcp_call(tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """One sessionless JSON-RPC tools/call; returns result.structuredContent."""
    body = _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": arguments,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
        method="tools/call",
        tool=tool,
    )
    if "error" in body:
        raise RuntimeError(f"Digital Jobs MCP JSON-RPC error for {tool}: {body['error']}")
    result = body.get("result", {})
    if result.get("isError"):
        raise RuntimeError(f"Digital Jobs MCP tool error for {tool}: {result}")
    sc = result.get("structuredContent")
    if not isinstance(sc, dict):
        raise RuntimeError(f"Digital Jobs MCP missing structuredContent for {tool}: {result}")
    if _STATS:
        for key in sc:
            _STATS["response_keys"][key] = _STATS["response_keys"].get(key, 0) + 1
        if sc.get("search_relaxed"):
            _STATS["relaxed"] += 1
        if sc.get("dropped_filters"):
            _STATS["dropped_filters"] += 1
    return sc


def _paginate(tool: str, first_args: Dict[str, Any], items_key: str) -> List[dict]:
    items: List[dict] = []
    args: Optional[Dict[str, Any]] = first_args
    pages = 0
    for _ in range(MAX_PAGES_PER_SEARCH):
        sc = _mcp_call(tool, args)
        pages += 1
        page = sc.get(items_key, [])
        items.extend(p for p in page if isinstance(p, dict))
        if not sc.get("has_more"):
            break
        cursor = sc.get("next_cursor")
        if not cursor:
            break
        # Cursor requests must carry ONLY the cursor: the signed cursor
        # preserves the effective filters and page size.
        args = {"cursor": cursor}
    if _STATS:
        # `run()` reads this straight after the call to attribute pages to a
        # search; the source is sequential, so there is no interleaving.
        _STATS["_last_pages"] = pages
    return items


class DigitalJobsSource:
    #: This source's tier, declared here and nowhere else
    #: (docs/28 §WP3b box 1). Primary = the hiring party's own
    #: publication; secondary = someone relaying it.
    primary = False
    #: Set when the run enumerated its *whole* scope (no truncation,
    #: caps or failures). Only then may absence become
    #: `not_listed_in_scope` rather than `disappeared`.
    complete_scope = False
    """Searches the public Digital Jobs MCP feed for one principal."""

    name = "digitaljobs"

    def __init__(
        self,
        crawler: SmartCrawler,
        matcher: Optional[JobMatcher] = None,
        registry: Optional[JobRegistry] = None,
        profile_name: str = "lj",
    ):
        self.crawler = crawler
        self.matcher = matcher
        self.registry = registry
        self.profile_name = profile_name

    def run(self) -> List[WikiEntry]:
        searches = _load_searches(self.profile_name)[:MAX_SEARCHES_PER_RUN]
        if not searches:
            logger.info("[digitaljobs] No searches configured; skipping.")
            return []

        start_stats()
        tools_list()                      # what does the server offer today?
        entries: List[WikiEntry] = []
        today = date.today().isoformat()
        seen_urls: set = set()
        for row in searches:
            search_name = str(row.get("name") or "digitaljobs-search")
            feed = str(row.get("feed") or "jobs").lower()
            logger.info(f"[digitaljobs] Running search {search_name} (feed={feed})")
            try:
                if feed == "opportunities":
                    limit = min(int(row.get("limit", PAGE_SIZE)), PAGE_SIZE)
                    listings = _paginate(
                        "list_ai_opportunities", {"limit": limit}, "opportunities"
                    )
                else:
                    filters = _sanitize_filters(row.get("filters"))
                    filters["limit"] = PAGE_SIZE
                    listings = _paginate("search_jobs", filters, "jobs")
            except Exception as e:
                logger.error(f"[digitaljobs] Search {search_name} failed: {e}")
                continue

            if _STATS:
                _STATS["searches"].append({
                    "name": search_name,
                    "feed": feed,
                    "pages": _STATS.pop("_last_pages", 0),
                    "returned": len(listings),
                })
            for listing in listings:
                if _STATS:
                    _STATS["records"] += 1
                    url = str(listing.get("url") or "").strip()
                    if url:
                        _STATS["records_with_url"] += 1
                        usable, bad_host = public_url(url)
                        if bad_host:
                            counts = _STATS.setdefault("unusable_hosts", {})
                            counts[bad_host] = counts.get(bad_host, 0) + 1
                        if url in seen_urls:
                            _STATS["duplicate_urls"] += 1
                        seen_urls.add(url)
                    if str(listing.get("title") or "").strip():
                        _STATS["records_with_title"] += 1
                    for field in RECORD_FIELDS:
                        if listing.get(field):
                            _STATS["fields"][field] = _STATS["fields"].get(field, 0) + 1
                entry = self._entry_for(listing, search_name, today)
                if entry is not None:
                    entries.append(entry)

        logger.info(f"[digitaljobs] Produced {len(entries)} entries")
        return entries

    @staticmethod
    def _describe(listing: dict, search_name: str) -> str:
        """Advert excerpt from the compact structured record."""
        parts = [f"Digital Jobs listing (search: {search_name})."]
        if listing.get("salary_text"):
            parts.append(f"Salary: {listing['salary_text']}.")
        gbp_min, gbp_max = listing.get("salary_min_gbp"), listing.get("salary_max_gbp")
        if gbp_min or gbp_max:
            parts.append(f"Normalised GBP: {gbp_min or '?'}–{gbp_max or '?'}.")
        for key in ("work_mode", "employment_type", "seniority",
                    "skills", "categories", "company_industries"):
            value = listing.get(key)
            if value:
                parts.append(f"{key}: {value}.")
        desc = listing.get("description")
        if desc:
            parts.append(str(desc)[:2000])
        else:
            parts.append(
                "API returns structured fields only; full advert lives at the URL."
            )
        return " ".join(parts)

    def _entry_for(
        self, listing: dict, search_name: str, today: str
    ) -> Optional[WikiEntry]:
        title = str(listing.get("title") or "").strip()
        if not title:
            return None
        org = str(listing.get("company") or "").strip() or "Digital Jobs"
        url, rejected_host = public_url(str(listing.get("url") or "").strip())
        if rejected_host:
            logger.warning(
                f"[digitaljobs] dropping unusable host {rejected_host!r} for "
                f"{title!r} ({org}); the lead is kept, the URL is not"
            )
        location = str(listing.get("location") or "").strip()
        desc = self._describe(listing, search_name)
        # Structured fields the MCP record carries (item 14, 2026-09-10):
        # keep them on the entry so reports/emails show salary + seniority
        # without re-parsing the synthesized description.
        salary = str(listing.get("salary_text") or "").strip()
        seniority_raw = listing.get("seniority") or []
        seniority = (
            ", ".join(str(s) for s in seniority_raw)
            if isinstance(seniority_raw, (list, tuple))
            else str(seniority_raw).strip()
        )

        record = None
        if self.registry is not None:
            try:
                record = self.registry.upsert(
                    org=org,
                    title=title,
                    url=url,
                    source=self.name,
                    discovery_url=url,
                    discovery_date=today,
                )
            except Exception as e:
                # A swallowed upsert is data loss, and the exception type is
                # what separates contract drift from a transient failure
                # (LEARNINGS 2026-09-20).
                logger.warning(f"[digitaljobs] Registry upsert failed "
                               f"({type(e).__name__}): {e}")

        if self.matcher is None:
            return None

        match = self.matcher.match(
            title,
            desc,
            org,
            job_url=url,
            location=location,
        )
        entry = WikiEntry(
            org_name=org,
            title=title,
            url=url or None,
            location=location or None,
            score=match.score,
            decision=match.decision,
            reasoning=match.reasoning,
            source=self.name,
            advert_excerpt=desc,
            discovery_url=url,
            discovery_date=today,
            salary=salary or None,
            seniority=seniority or None,
            **match_fields(match),
        )
        if record is not None:
            entry.lifecycle_status = record.status
            entry.first_seen = record.first_seen
        return entry
