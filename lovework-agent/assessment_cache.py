"""Content-addressed cache for stable, repeatable job assessments."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

import config
from matcher import MatchResult

logger = logging.getLogger(__name__)


class AssessmentCachingMatcher:
    """Cache successful MatchResult objects; never cache UNSCORED failures."""

    def __init__(self, matcher, namespace: str, cache_dir: Optional[Path] = None):
        self.matcher = matcher
        self.namespace = namespace
        self.cache_dir = cache_dir or config.CACHE_DIR / "assessments"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(
        self,
        job_title: str,
        job_description: str,
        org_name: str,
        job_url: str,
        location: str,
    ) -> str:
        canonical = json.dumps(
            {
                "namespace": self.namespace,
                "title": job_title,
                "description": job_description,
                "org": org_name,
                "url": job_url,
                "location": location,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def match(
        self,
        job_title: str,
        job_description: str,
        org_name: str,
        job_url: str = "",
        location: str = "",
    ) -> MatchResult:
        key = self._key(job_title, job_description, org_name, job_url, location)
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            try:
                return MatchResult.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Ignoring invalid assessment cache %s: %s", path, exc)

        result = self.matcher.match(
            job_title,
            job_description,
            org_name,
            job_url=job_url,
            location=location,
        )
        if getattr(result, "assessment_status", "SCORED") != "UNSCORED":
            try:
                path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
            except (OSError, AttributeError) as exc:
                logger.warning("Could not cache assessment %s: %s", key, exc)
        return result
