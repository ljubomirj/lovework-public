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

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or config.CACHE_DIR / "enrichment"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(f"{ENRICHMENT_VERSION}:{url}".encode()).hexdigest()[:24]
        return self.cache_dir / f"{digest}.json"

    def enrich(self, description: str, url: str) -> EnrichedLead:
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

        text, method = self._fetch(url)
        if text:
            result.primary_text = text[:MAX_PRIMARY_CHARS]
            result.primary_content_hash = hashlib.sha256(
                result.primary_text.encode()
            ).hexdigest()
            result.primary_fetched_at = datetime.now(timezone.utc).isoformat()
            result.primary_fetch_method = method
            try:
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
                return text, "http"
        except Exception as exc:
            logger.info("Primary-page HTTP enrichment failed for %s: %s", url, exc)

        # Dynamic or blocked pages get the existing Firecrawl-backed fetcher.
        try:
            from crawler import fetch_page

            text = fetch_page(url, use_cache=True)
            if text:
                return text[:MAX_PRIMARY_CHARS], "firecrawl"
        except Exception as exc:
            logger.warning("Primary-page fallback failed for %s: %s", url, exc)
        return "", ""


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
