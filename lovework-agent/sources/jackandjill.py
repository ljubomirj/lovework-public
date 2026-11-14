"""
Source: Jack & Jill (https://www.jackandjill.ai).

They are an AI agent on the *other* side of the market. Their own words:
"Jack is fiduciary to the candidate; Jill works for companies." LoveWork's
principals are what Jack & Jill call **candidates** — we read their
employer-side mandates (Jill's roles, published by Jack's network) as leads
for a principal, which is the mirror image of what they are built to do.

Permissions (checked 2026-09-12, `.well-known/ai.txt`):

    Crawling: allowed
    Training: allowed
    Retrieval: allowed
    Attribution-Required: yes

`llms.txt` publishes the machine surface under "Optional": the XML jobs feed
at /jobs/feed.xml is described there as the "Live featured-role feed". ai.txt
states there is no public API, so the feed *is* the sanctioned interface.

Robots discipline (robots.txt, same date) — this source only ever touches
allowed paths:

    Disallowed: /api/ · /cv · /salary · /jobs · /jobs/<category>
    Used here:  /jobs/feed.xml  (and nothing else)

Per-job pages (/jobs/<category>/<slug>-<uuid>) are not covered by those
`$`-anchored disallows, but this source never fetches them: the feed already
carries the full advert HTML, so one GET ingests every offering. There is no
per-job request and therefore no N+1 fetch pattern — the whole run is a single
HTTP request. `_assert_crawlable` enforces this for whatever URL we do fetch.

Deduplication: every offering carries a stable `<referencenumber>` UUID, which
is the identity used for offer state and for the periodic letter (see
`jnj_letter.py`). The feed's `<url>` carries LinkedIn utm_* parameters whose
values change between feed runs, so URLs are canonicalised before they reach
the registry or the wiki — otherwise the registry hash would churn weekly.

Assessment queue: a 342-offering feed republished continuously would otherwise
be re-scored on every run. Offerings are recorded once in
`cache/jackandjill/offerings.jsonl` (append-only, folded on read) and only
unassessed offerings are sent to the matcher, newest first, capped by
LOVEWORK_JNJ_MAX_MATCHES. The cap defers the remainder to a later run rather
than stranding it: an offering leaves the queue only once it has a verdict.
"""

import html
import json
import logging
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import config
from crawler import SmartCrawler
from job_registry import JobRegistry
from matcher import JobMatcher
from wiki_store import WikiEntry, match_fields

logger = logging.getLogger(__name__)

FEED_URL = os.getenv(
    "LOVEWORK_JNJ_FEED_URL", "https://www.jackandjill.ai/jobs/feed.xml"
)
ROBOTS_URL = os.getenv(
    "LOVEWORK_JNJ_ROBOTS_URL", "https://www.jackandjill.ai/robots.txt"
)
USER_AGENT = os.getenv(
    "LOVEWORK_JNJ_USER_AGENT",
    "Mozilla/5.0 (compatible; LoveWork/0.1; +https://lovework.be)",
)
ATTRIBUTION = "Jack & Jill — https://www.jackandjill.ai"

# robots.txt Disallow rules for this host, verbatim (2026-09-12). The live
# robots.txt is consulted as well; these are the belt to its braces, so a
# robots fetch failure can never turn into a request to a disallowed path.
_DISALLOWED_EXACT = frozenset({
    "/jobs", "/jobs/engineer", "/jobs/sales", "/jobs/marketing",
    "/jobs/design", "/jobs/finance", "/jobs/product", "/jobs/ops",
    "/jobs/strategy", "/jobs/talent", "/jobs/founder", "/jobs/data",
    "/cv", "/salary",
})
_DISALLOWED_PREFIXES = ("/api/",)

# Only unassessed offerings are sent to the matcher; the rest are deferred.
MAX_NEW_MATCHES = int(os.getenv("LOVEWORK_JNJ_MAX_MATCHES", "60"))

# The drop: their hand-picked, personal mail (see docs/24). Read through a
# query because it arrives in no label — `label:LJ-jobs from:jackandjill.ai`
# matches nothing. Bounded to recent mail so the year-long back-catalogue is
# not scored as if it were today's leads.
DROP_QUERY_TEMPLATE = os.getenv(
    "LOVEWORK_JNJ_DROP_QUERY", "from:jackandjill.ai newer_than:{days}d"
)
DROP_WINDOW_DAYS = int(os.getenv("LOVEWORK_JNJ_DROP_DAYS", "21"))
DROP_ARCHIVE_LABEL = "Jack-and-Jill"

# Query parameters that vary between feed runs and must not reach the registry.
_VOLATILE_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_content",
                    "utm_term", "feed_run", "applied_from")

OFFERINGS_FILENAME = "offerings.jsonl"


@dataclass
class Offer:
    """One offering as published in the feed."""

    referencenumber: str
    title: str
    company: str
    url: str
    apply_url: str
    date: str                 # feed publication date, ISO
    expiration_date: str
    location: str
    city: str
    country: str
    workplace_types: str
    salary: str
    job_type: str
    description: str          # plain text of the advert body
    alternate_locations: List[str]   # extra countries the role is open in
    raw_title: str = ""       # title exactly as published

    def state_row(self, *, today: str) -> Dict[str, object]:
        """Row written to offerings.jsonl on first sighting."""
        return {
            "channel": "feed",
            "referencenumber": self.referencenumber,
            "title": self.title,
            "company": self.company,
            "url": self.url,
            "date": self.date,
            "expiration_date": self.expiration_date,
            "location": self.location,
            "city": self.city,
            "country": self.country,
            "alternate_locations": list(self.alternate_locations),
            "workplace_types": self.workplace_types,
            "salary": self.salary,
            "job_type": self.job_type,
            "first_seen": today,
        }


def today_iso() -> str:
    """Today's date, ISO. One place, so rows and windows cannot disagree."""
    return date.today().isoformat()


def _canonical_url(url: str) -> str:
    """Drop volatile tracking parameters; keep any stable ones.

    The feed's URLs carry `utm_*`/`feed_run` values that change between runs
    (they encode the syndication run id), so a raw URL is not a stable key.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in _VOLATILE_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(kept), ""))


def _split_title(raw: str, company: str) -> str:
    """Feed titles read "Product Manager ($100k) at Allus AI" — drop the company.

    The company has its own field, and repeating it in the title pollutes the
    registry hash and the wiki listing.
    """
    title = (raw or "").strip()
    if company:
        suffix = f" at {company.strip()}"
        if title.lower().endswith(suffix.lower()):
            title = title[: -len(suffix)].strip()
    return title


def _text_of(node: Optional[ET.Element]) -> str:
    if node is None or node.text is None:
        return ""
    return html.unescape(node.text).strip()


def parse_feed(xml_text: str) -> List[Offer]:
    """Parse the /jobs/feed.xml document into offers, deduped by reference number."""
    root = ET.fromstring(xml_text)
    offers: List[Offer] = []
    seen: set = set()
    for job in root.iter("job"):
        reference = _text_of(job.find("referencenumber"))
        raw_title = _text_of(job.find("title"))
        company = _text_of(job.find("company"))
        if not reference or not raw_title:
            continue
        if reference in seen:
            continue
        seen.add(reference)
        description_html = _text_of(job.find("description"))
        alternates = [
            _text_of(node)
            for node in job.findall("alternateLocations/alternateLocation")
            if _text_of(node)
        ]
        offers.append(Offer(
            referencenumber=reference,
            title=_split_title(raw_title, company),
            company=company,
            url=_canonical_url(_text_of(job.find("url"))),
            apply_url=_canonical_url(_text_of(job.find("applyurl"))),
            date=_text_of(job.find("date")),
            expiration_date=_text_of(job.find("expirationdate")),
            location=_text_of(job.find("location")),
            city=_text_of(job.find("city")),
            country=_text_of(job.find("country")),
            workplace_types=_text_of(job.find("workplaceTypes")),
            salary=_text_of(job.find("salary")),
            job_type=_text_of(job.find("jobtype")),
            description=_describe_html(description_html),
            alternate_locations=alternates,
            raw_title=raw_title,
        ))
    return offers


def _describe_html(description_html: str) -> str:
    """Plain text of the advert body (the feed ships HTML in a CDATA block)."""
    if not description_html:
        return ""
    try:
        from enrichment import extract_html_text
        return extract_html_text(description_html).strip()
    except Exception:  # pragma: no cover - enrichment is best-effort here
        text = re.sub(r"<[^>]+>", " ", description_html)
        return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _assert_crawlable(url: str) -> None:
    """Refuse any URL outside this host's published crawl policy."""
    path = urlsplit(url).path or "/"
    if path in _DISALLOWED_EXACT or path.startswith(_DISALLOWED_PREFIXES):
        raise RuntimeError(f"Jack & Jill robots.txt disallows {path}; source refuses it")
    parser = _robots_parser()
    if parser is not None and not parser.can_fetch(USER_AGENT, url):
        raise RuntimeError(f"Jack & Jill robots.txt disallows {url}")


_ROBOTS_CACHE: Dict[str, Optional[RobotFileParser]] = {}


def _robots_parser() -> Optional[RobotFileParser]:
    """Fetch and cache robots.txt once per process; None if unavailable.

    Unavailability is not treated as permission to ignore the file — the
    hard-coded rule set above still applies.
    """
    host = urlsplit(ROBOTS_URL).netloc
    if host in _ROBOTS_CACHE:
        return _ROBOTS_CACHE[host]
    parser: Optional[RobotFileParser] = None
    try:
        request = urllib.request.Request(ROBOTS_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            parser = RobotFileParser()
            parser.parse(response.read().decode("utf-8", "replace").splitlines())
    except Exception as e:
        logger.warning(f"[jackandjill] robots.txt unavailable ({e}); using built-in rules")
    _ROBOTS_CACHE[host] = parser
    return parser


def fetch_feed(url: str = FEED_URL) -> str:
    """One HTTP request per run: the published XML feed."""
    _assert_crawlable(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8", "replace")


def _load_state(path: Path) -> Dict[str, dict]:
    """Fold the append-only offer log; the last row for a reference wins."""
    rows: Dict[str, dict] = {}
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        reference = str(row.get("referencenumber") or "")
        if not reference:
            continue
        merged = dict(rows.get(reference) or {})
        merged.update(row)
        rows[reference] = merged
    return rows


def append_state(path: Path, rows: List[dict]) -> None:
    """Append rows to the offer log (never rewrites, never deletes)."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


class JackAndJillSource:
    #: This source's tier, declared here and nowhere else
    #: (docs/28 §WP3b box 1). Primary = the hiring party's own
    #: publication; secondary = someone relaying it.
    primary = True
    #: Set when the run enumerated its *whole* scope (no truncation,
    #: caps or failures). Only then may absence become
    #: `not_listed_in_scope` rather than `disappeared`.
    complete_scope = False

    """Jack & Jill's published featured-role feed as a LoveWork source."""

    name = "jackandjill"

    def __init__(
        self,
        crawler: SmartCrawler,
        matcher: Optional[JobMatcher] = None,
        registry: Optional[JobRegistry] = None,
        profile_name: str = "lj",
        cache_dir: Optional[Path] = None,
        feed_url: str = FEED_URL,
    ):
        self.crawler = crawler
        self.matcher = matcher
        self.registry = registry
        self.profile_name = profile_name
        self.feed_url = feed_url
        if cache_dir is None:
            from principal_runtime import resolve_principal_runtime
            cache_dir = resolve_principal_runtime(profile_name).cache_dir
        self.state_path = Path(cache_dir) / "jackandjill" / OFFERINGS_FILENAME

    # The registry is the pipeline's "this run may write" signal: a dry run
    # passes no registry, and a preview must not mutate principal state.
    @property
    def _writable(self) -> bool:
        return self.registry is not None

    def run(self) -> List[WikiEntry]:
        """The drop first — it is the relationship — then the public feed.

        A failure in either channel must not silence the other: the drop is the
        correspondence we answer, the feed is market data, and both are
        swallow-on-error so one broken channel cannot cost a crawl its leads.
        """
        entries: List[WikiEntry] = []
        try:
            drop = self.ingest_drop()
            entries.extend(drop["entries"])
        except Exception as e:
            logger.warning(f"[jackandjill] Drop ingest failed: {e}")
        try:
            entries.extend(self.run_feed()["entries"])
        except Exception as e:
            logger.warning(f"[jackandjill] Feed ingest failed: {e}")
        return entries

    def ingest_drop(self, days: int = DROP_WINDOW_DAYS,
                    msg_ids: Optional[Iterable[str]] = None) -> Dict[str, object]:
        """Read Jack's personal leads from the mailbox and log them.

        Query mode never marks mail read, so the principal keeps his own unread
        marker; progress is tracked by the email archive instead (re-running is
        free). Ledger rows carry `channel: "drop"` and our own verdict so the
        letter can answer each lead individually.

        `msg_ids` re-reads named messages even though they are archived — the
        only way a parser or gate fix reaches a drop that was already ingested.
        """
        from principal_runtime import resolve_principal_runtime
        from sources.gmail_lj_jobs import GmailJobsSource

        runtime = resolve_principal_runtime(self.profile_name)
        mailbox = runtime.gmail_mailbox
        if mailbox is None:
            logger.info(f"[jackandjill] {self.profile_name} has no mailbox; no drop to read")
            return {"entries": [], "leads": 0}

        reader = GmailJobsSource(
            crawler=None,
            matcher=self.matcher,
            registry=self.registry,
            mark_read=False,                       # the drop is personal mail
            label=DROP_ARCHIVE_LABEL,
            credential_home=mailbox.credential_home,
            source_name=self.name,
            sources_dir=runtime.sources_dir,
            capture_seeds=False,
            capture_raw=True,
            query=DROP_QUERY_TEMPLATE.format(days=days),
            force_ids=msg_ids,
        )
        entries = reader.run()
        if self._writable:
            append_state(self.state_path, [
                {
                    "channel": "drop",
                    "referencenumber": self._drop_uuid(entry.url or ""),
                    "title": entry.title,
                    "company": entry.org_name,
                    "url": entry.url,
                    "date": today_iso(),
                    "first_seen": today_iso(),
                    "score": entry.score,
                    "decision": entry.decision,
                    "reasoning": (entry.reasoning or "")[:600],
                }
                for entry in entries
            ])
        logger.info(f"[jackandjill] Drop produced {len(entries)} lead(s)")
        return {"entries": entries, "leads": len(entries)}

    @staticmethod
    def _drop_uuid(url: str) -> str:
        """The drop lead's identity: the uuid in app.jackandjill.ai/jobs/<uuid>/post."""
        match = re.search(r"/jobs/([0-9a-fA-F-]{36})/post", url or "")
        return match.group(1) if match else (url or "")

    def run_feed(self) -> Dict[str, object]:
        """Ingest the feed; returns entries plus a summary for the run log."""
        xml_text = fetch_feed(self.feed_url)
        offers = parse_feed(xml_text)
        # The feed is this source's entire scope — one document, no pagination —
        # so a successful fetch and parse means we enumerated everything it
        # lists. Only this declaration lets absence become
        # `not_listed_in_scope` rather than `disappeared` (docs/28 §WP3b).
        self.complete_scope = True
        if self.registry is not None:
            self.registry.note_complete_scope(self.name)
        today = date.today().isoformat()
        state = _load_state(self.state_path) if self._writable else {}
        logger.info(
            f"[jackandjill] Feed carries {len(offers)} offerings; "
            f"{len(state)} already recorded for {self.profile_name}"
        )

        new_offers = [o for o in offers if o.referencenumber not in state]
        unassessed = [
            o for o in offers
            if o.referencenumber in state and not state[o.referencenumber].get("decision")
        ]
        # Newest first: the letter's window is recent offerings, and a backlog
        # is drained behind it rather than in front of it.
        queue = sorted(new_offers + unassessed, key=lambda o: o.date, reverse=True)
        to_match = queue[:MAX_NEW_MATCHES]
        deferred = len(queue) - len(to_match)
        if deferred:
            logger.info(
                f"[jackandjill] {len(queue)} unassessed offerings, "
                f"matching {len(to_match)} this run, deferring {deferred}"
            )

        if self._writable:
            # Every offering is recorded on first sighting, matched or not, so
            # the letter sees the full feed rather than only the scored slice.
            append_state(self.state_path, [o.state_row(today=today) for o in new_offers])

        # One upsert per offering: presence in the feed is the lifecycle signal
        # (mark_run_complete treats absence as disappeared), and the returned
        # record carries status/first_seen onto the entry without a second write.
        records: Dict[str, object] = {}
        if self._writable:
            for offer in offers:
                try:
                    records[offer.referencenumber] = self.registry.upsert(
                        org=offer.company or "Jack & Jill",
                        title=offer.title,
                        url=offer.url,
                        source=self.name,
                        primary=self.primary,
                        discovery_url=offer.url,
                        discovery_date=offer.date[:10] or today,
                        # The feed's own publication date is a real posting date
                        # (Offer.date, "feed publication date") — never the
                        # `today` fallback, which is ingest, not posting (§WP3a).
                        posted_at=offer.date[:10],
                    )
                except Exception as e:
                    # A swallowed upsert is data loss, and the exception type is
                    # what separates contract drift from a transient failure
                    # (LEARNINGS 2026-09-20).
                    logger.warning(f"[jackandjill] Registry upsert failed "
                                   f"({type(e).__name__}): {e}")

        entries = [
            e for e in (
                self._entry_for(o, today, records.get(o.referencenumber))
                for o in to_match
            ) if e is not None
        ]

        if self._writable:
            append_state(self.state_path, self._verdict_rows(to_match, entries, today))

        logger.info(f"[jackandjill] Produced {len(entries)} entries")
        return {
            "offers": len(offers),
            "new": len(new_offers),
            "matched": len(to_match),
            "deferred": deferred,
            "entries": entries,
        }

    def _verdict_rows(self, offers: List[Offer], entries: List[WikiEntry], today: str) -> List[dict]:
        """Attach each verdict to its offering by URL (stable after canonicalisation)."""
        by_url = {e.url: e for e in entries if e.url}
        rows: List[dict] = []
        for offer in offers:
            entry = by_url.get(offer.url)
            if entry is None:
                continue
            rows.append({
                "channel": "feed",
                "referencenumber": offer.referencenumber,
                "score": entry.score,
                "decision": entry.decision,
                "matched_at": today,
            })
        return rows

    def _entry_for(self, offer: Offer, today: str, record=None) -> Optional[WikiEntry]:
        if self.matcher is None:
            return None
        org = offer.company or "Jack & Jill"
        description = self._describe(offer)
        match = self.matcher.match(
            offer.title,
            description,
            org,
            job_url=offer.url,
            location=offer.location,
        )
        entry = WikiEntry(
            org_name=org,
            title=offer.title,
            url=offer.url or None,
            location=offer.location or None,
            score=match.score,
            decision=match.decision,
            reasoning=match.reasoning,
            source=self.name,
            advert_excerpt=description,
            discovery_url=offer.url,
            discovery_date=offer.date[:10] or today,
            posted_at=offer.date[:10],
            salary=offer.salary or None,
            seniority=None,
            **match_fields(match),
        )
        if record is not None:
            entry.lifecycle_status = record.status
            entry.first_seen = record.first_seen
        return entry

    @staticmethod
    def _describe(offer: Offer) -> str:
        """Advert text handed to the matcher: their metadata, then the real body."""
        parts = [f"Jack & Jill offering ({ATTRIBUTION})."]
        if offer.company:
            parts.append(f"Company: {offer.company}.")
        location = ", ".join(
            p for p in (offer.city, offer.location, offer.country) if p
        )
        if location:
            parts.append(f"Location: {location}.")
        if offer.alternate_locations:
            # Multi-country roles (56/342 on 2026-09-12): the primary location
            # can read US-only while the role is in fact open across Europe.
            parts.append(
                "Also open in: " + ", ".join(offer.alternate_locations) + "."
            )
        if offer.workplace_types:
            parts.append(f"Workplace: {offer.workplace_types}.")
        if offer.salary:
            parts.append(f"Salary: {offer.salary}.")
        if offer.job_type:
            parts.append(f"Type: {offer.job_type}.")
        if offer.date:
            parts.append(f"Published: {offer.date[:10]}.")
        if offer.description:
            parts.append(offer.description)
        return " ".join(parts)
