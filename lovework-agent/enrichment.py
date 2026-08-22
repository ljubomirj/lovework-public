"""Primary-page enrichment for job leads.

Sources remain responsible for discovery.  This module provides the single
click-through stage between discovery and matching: fetch the advert URL,
extract useful text (including Next.js streamed data), cache the evidence,
and pass the richer description to any matcher implementation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import config

logger = logging.getLogger(__name__)

MAX_PRIMARY_CHARS = 20_000
MIN_USEFUL_CHARS = 400
ENRICHMENT_VERSION = "v2"
# P5 — liveness is a lifecycle fact. These markers on the primary advert page
# mean the role is dead; enrichment flags it and the matcher drops it before
# any scoring. The Arsenal FC case (careers page: "This position is no longer
# active") is the canonical regression.
EXPIRY_MARKERS = (
    "no longer active",
    "position has been filled",
    "this position is no longer active",
    "this role is no longer",
    "job has expired",
    "advert has expired",
    "no longer accepting applications",
    "position filled",
    "vacancy closed",
    "we are no longer hiring",
)
_NEXT_PUSH_RE = re.compile(
    r"self\.__next_f\.push\((?P<payload>.*?)\)</script>", re.DOTALL
)


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _clean_text(text: str) -> str:
    text = unescape(text).replace("\\n", "\n")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n(?:\s*\n)+", "\n\n", text)
    return text.strip()


def _next_prose(text: str) -> list[str]:
    """Keep human prose paragraphs while discarding Next.js RSC machinery."""
    out: list[str] = []
    for raw in re.split(r"\n\s*\n", text):
        paragraph = _clean_text(raw)
        if len(paragraph) < 70 or len(paragraph.split()) < 8:
            continue
        if any(marker in paragraph for marker in (
            "static/chunks", "className", "parallelRouterKey", "props:",
            "$undefined", "self.__next_f", "dangerouslySetInnerHTML",
        )):
            continue
        alpha_space = sum(char.isalpha() or char.isspace() for char in paragraph)
        if alpha_space / max(len(paragraph), 1) < 0.7:
            continue
        out.append(paragraph)
    return out


def extract_html_text(html: str) -> str:
    """Extract visible HTML plus useful Next.js RSC string payloads."""
    parser = _VisibleTextParser()
    parser.feed(html)
    parts = [_clean_text("\n".join(parser.parts))]

    # Next.js App Router frequently places the useful server-rendered content
    # only in self.__next_f.push([..., "..."]) script payloads.  Decode the
    # JSON array rather than treating JavaScript escapes as literal text.
    for match in _NEXT_PUSH_RE.finditer(html):
        try:
            payload = json.loads(match.group("payload"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, list):
            continue
        for value in payload[1:]:
            if not isinstance(value, str):
                continue
            parts.extend(_next_prose(value))

    # Preserve order while avoiding repeated SSR/RSC copies.
    unique: list[str] = []
    seen: set[str] = set()
    for part in parts:
        fingerprint = hashlib.sha256(part.encode()).hexdigest()
        if part and fingerprint not in seen:
            unique.append(part)
            seen.add(fingerprint)
    return "\n\n".join(unique)[:MAX_PRIMARY_CHARS]


@dataclass
class EnrichedLead:
    original_description: str
    primary_url: str = ""
    primary_text: str = ""
    primary_content_hash: str = ""
    primary_fetched_at: str = ""
    primary_fetch_method: str = ""
    # P5 — liveness. True when the primary page carries an expiry marker
    # ("no longer active", "position filled", ...). The matcher must drop
    # the lead before scoring, and the report must not surface it.
    expired: bool = False
    expiry_evidence: str = ""

    @property
    def matcher_description(self) -> str:
        if not self.primary_text:
            return self.original_description
        return (
            f"{self.original_description}\n\n"
            f"--- PRIMARY ADVERT PAGE ({self.primary_url}) ---\n"
            f"{self.primary_text}"
        )


class LeadEnricher:
    """Fetch and cache one primary advert page for each matchable lead."""

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        crawler_cache_dir: Optional[Path] = None,
    ):
        self.cache_dir = cache_dir or config.CACHE_DIR / "enrichment"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.crawler_cache_dir = crawler_cache_dir

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(f"{ENRICHMENT_VERSION}:{url}".encode()).hexdigest()[:24]
        return self.cache_dir / f"{digest}.json"

    def load_cached(self, description: str, url: str) -> EnrichedLead:
        """Return retained primary evidence without ever fetching the network.

        Historical reassessment deliberately uses this method.  It is safe to
        run against an existing registry because it cannot crawl, alter a
        source, or discover a newer advert version.
        """
        result = EnrichedLead(original_description=description, primary_url=url)
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return result

        cache_path = self._cache_path(url)
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                # The fetched page is reusable, but source snippets can differ
                # between discoveries of the same URL.
                cached["original_description"] = description
                cached["primary_url"] = url
                return EnrichedLead(**cached)
            except (OSError, json.JSONDecodeError, TypeError):
                logger.warning("Ignoring invalid enrichment cache for %s", url)

        return result

    def enrich(self, description: str, url: str) -> EnrichedLead:
        result = self.load_cached(description, url)
        if result.primary_text:
            return result

        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return result

        text, method = self._fetch(url)
        if text:
            result.primary_text = text[:MAX_PRIMARY_CHARS]
            result.primary_content_hash = hashlib.sha256(
                result.primary_text.encode()
            ).hexdigest()
            result.primary_fetched_at = datetime.now(timezone.utc).isoformat()
            result.primary_fetch_method = method
            # P5 — liveness: an expiry marker on the primary page means the
            # lead is dead. Flag it (matcher drops) and do not cache it as
            # reusable live evidence.
            marker = _expiry_marker(text)
            if marker:
                result.expired = True
                result.expiry_evidence = marker
                logger.info("Primary page for %s is expired (marker: %r)", url, marker)
                return result
            try:
                cache_path = self._cache_path(url)
                cache_path.write_text(
                    json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                logger.warning("Could not cache enrichment for %s: %s", url, exc)
        return result

    def _fetch(self, url: str) -> tuple[str, str]:
        try:
            import httpx

            response = httpx.get(
                url,
                timeout=30,
                follow_redirects=True,
                headers={"User-Agent": "lovework-agent/1.0 (+https://lovework.be)"},
            )
            response.raise_for_status()
            text = extract_html_text(response.text)
            if len(text) >= MIN_USEFUL_CHARS:
                logger.info("Primary-page HTTP enrichment %s (%d chars)", url, len(text))
                # P6 — discovery must resolve to the actual advert. Social
                # posts (X/Twitter) bury the real link in the first reply /
                # an embedded short link (t.co). If the fetched page is a
                # social post carrying a job link, follow it and use the
                # advert page's text instead — expiry markers (P5) then
                # apply to the *resolved* page, not the post.
                resolved = self._follow_social_link(url, text)
                if resolved:
                    return resolved, "http-via-post"
                return text, "http"
        except Exception as exc:
            logger.info("Primary-page HTTP enrichment failed for %s: %s", url, exc)

        # Dynamic or blocked pages get the existing Firecrawl-backed fetcher.
        try:
            from crawler import fetch_page

            text = fetch_page(url, use_cache=True, cache_dir=self.crawler_cache_dir)
            if text:
                return text[:MAX_PRIMARY_CHARS], "firecrawl"
        except Exception as exc:
            logger.warning("Primary-page fallback failed for %s: %s", url, exc)
        return "", ""

    def _follow_social_link(self, post_url: str, post_text: str) -> Optional[str]:
        """Follow a job link embedded in a social post (X first-reply / t.co).

        X/Twitter downgrades posts that carry links, so the actual advert
        link is usually in the first reply or an embedded ``t.co`` short
        link. Extract the first external link from the post text and fetch
        that page; if it is a real advert page (not the social site), return
        its text. Returns None when the post has no resolvable link.
        """
        post_host = urlparse(post_url).netloc.lower()
        m = re.search(r"https?://[^\s)\"'>]+", post_text or "")
        if not m:
            return None
        candidate = m.group(0).rstrip(".,;")
        candidate_host = urlparse(candidate).netloc.lower()
        if candidate_host == post_host or "x.com" in candidate_host or "twitter.com" in candidate_host:
            return None
        try:
            import httpx

            response = httpx.get(
                candidate,
                timeout=30,
                follow_redirects=True,
                headers={"User-Agent": "lovework-agent/1.0 (+https://lovework.be)"},
            )
            response.raise_for_status()
            resolved_text = extract_html_text(response.text)
            # A short resolved page is still the truth when it says the role
            # is gone (P5 expiry markers) — keep it so the liveness check
            # runs on the actual advert page, not the post.
            if len(resolved_text) >= MIN_USEFUL_CHARS or _expiry_marker(resolved_text):
                logger.info(
                    "Resolved social-post link %s -> %s (%d chars)",
                    post_url, candidate, len(resolved_text),
                )
                return resolved_text[:MAX_PRIMARY_CHARS]
        except Exception as exc:
            logger.info("Social-link resolution failed for %s: %s", candidate, exc)
        return None


def _expiry_marker(text: str) -> str:
    """Return the first expiry marker found in primary-page text, else ''.

    P5 — liveness is a lifecycle fact. Cheap lowercase substring scan; the
    markers are deliberately specific phrases, not bare words, to avoid
    false positives on "we are no longer hiring for Q4" type copy that
    refers to the future.
    """
    lowered = (text or "").lower()
    for marker in EXPIRY_MARKERS:
        if marker in lowered:
            return marker
    return ""


class EnrichingMatcher:
    """Drop-in matcher wrapper that enriches before legacy or DSPy scoring."""

    def __init__(self, matcher, enricher: LeadEnricher):
        self.matcher = matcher
        self.enricher = enricher

    def match(
        self,
        job_title: str,
        job_description: str,
        org_name: str,
        job_url: str = "",
        location: str = "",
    ):
        evidence = self.enricher.enrich(job_description, job_url)
        # P5 — liveness is a lifecycle fact: an expired advert is dropped
        # before any LLM scoring; it must never surface as a GO/MAYBE.
        if evidence.expired:
            from matcher import MatchResult

            return MatchResult(
                fit_score=0.0, reach_score=0.0, flourish_score=0.0,
                combined_score=0.0, score=0.0, decision="DROP",
                recommended_action="DROP",
                reasoning=(
                    f"AUTO-DROP: advert expired (primary page marker "
                    f"{evidence.expiry_evidence!r}). {job_url}"
                ),
            )
        result = self.matcher.match(
            job_title,
            evidence.matcher_description,
            org_name,
            job_url=job_url,
            location=location,
        )
        for name in (
            "primary_content_hash",
            "primary_fetched_at",
            "primary_fetch_method",
        ):
            try:
                setattr(result, name, getattr(evidence, name))
            except (AttributeError, ValueError):
                # MatchResult is expected to expose these fields; retain
                # compatibility with lightweight test/fake result objects.
                pass
        return result
