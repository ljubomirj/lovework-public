#!/usr/bin/env python3
"""Replay an existing principal registry against a revised profile.

This is deliberately not a crawl.  It reads only the principal-owned jobs
registry and retained primary-advert cache, scores cached evidence against the
current profile, and writes a separate reassessment report.  It does not
contact Gmail, fetch the web, update lifecycle state, or alter historical org
pages/reports.

Usage:
    ../venv/bin/python3 reassess.py --profile vj --role data-statistics-pricing
    ../venv/bin/python3 reassess.py --profile vj --role data-statistics-pricing --dry-run
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import config
from assessment_cache import AssessmentCachingMatcher, assessment_cache_namespace
from principal_evidence import PrincipalEvidenceIndex, EvidenceGroundedMatcher
from principal_runtime import PrincipalRuntime, resolve_principal_runtime
from enrichment import LeadEnricher
from job_registry import JobRecord, JobRegistry
from llm_client import LLMClient
from matcher import (
    JobMatcher,
    MatchResult,
    _apply_profile_preference_exclusion,
    _apply_work_auth_kill,
    _check_profile_preference_exclusion,
    _check_work_auth_kill,
)
from snapshot import append_assessments, append_run, new_run_id
from wiki_store import WikiEntry, _format_entry

logger = logging.getLogger(__name__)

REASSESSMENT_SOURCE = "cached_reassessment"
# Reassessment asks for concise JSON fields, not an essay.  Keeping this below
# the general crawl budget materially reduces latency and token cost when a
# profile revision replays hundreds of retained adverts.
REASSESSMENT_MAX_TOKENS = 2200


def _record_description(record: JobRecord) -> str:
    """Supply minimal retained context alongside a cached primary advert."""
    return (
        "Historical cached-evidence reassessment. "
        f"Advert title: {record.title}. "
        f"Original careers page: {record.careers_url or 'unspecified'}."
    )


def _unscored_entry(record: JobRecord, reason: str) -> WikiEntry:
    return WikiEntry(
        org_name=record.org,
        title=record.title,
        url=record.url,
        location="",
        score=0.0,
        decision="FLAG",
        reasoning=f"UNSCORED: {reason}",
        source=record.source or REASSESSMENT_SOURCE,
        lifecycle_status=record.status,
        first_seen=record.first_seen,
        discovery_url=record.discovery_url,
        discovery_date=record.discovery_date,
        recommended_action="WATCH",
        assessment_status="UNSCORED",
    )


def _entry_from_match(record: JobRecord, match: MatchResult) -> WikiEntry:
    return WikiEntry(
        org_name=record.org,
        title=record.title,
        url=record.url,
        location="",
        score=match.score,
        decision=match.decision,
        reasoning=match.reasoning,
        source=record.source or REASSESSMENT_SOURCE,
        lifecycle_status=record.status,
        first_seen=record.first_seen,
        discovery_url=record.discovery_url,
        discovery_date=record.discovery_date,
        fit_score=match.fit_score,
        reach_score=match.reach_score,
        flourish_score=match.flourish_score,
        combined_score=match.combined_score,
        recommended_action=match.recommended_action,
        primary_content_hash=match.primary_content_hash,
        primary_fetched_at=match.primary_fetched_at,
        primary_fetch_method=match.primary_fetch_method,
        alignment_matrix=match.alignment_matrix,
        gaps=match.gaps,
        application_angle=match.application_angle,
        screening_story=match.screening_story,
        likely_day_to_day=match.likely_day_to_day,
        prestige_trap_risk=match.prestige_trap_risk,
        assessment_status=match.assessment_status,
    )


def _deterministic_cached_only_result(
    profile_text: str,
    record: JobRecord,
) -> Optional[MatchResult]:
    """Apply no-network deterministic gates when evidence is unavailable."""
    description = _record_description(record)
    work_auth_kill = _check_work_auth_kill("", description)
    if work_auth_kill:
        return _apply_work_auth_kill(MatchResult(), work_auth_kill)
    preference_exclusion = _check_profile_preference_exclusion(
        profile_text, record.title, description
    )
    if preference_exclusion:
        return _apply_profile_preference_exclusion(MatchResult(), preference_exclusion)
    return None


def reassess_records(
    profile_name: str,
    role: str,
    *,
    runtime: Optional[PrincipalRuntime] = None,
    registry: Optional[JobRegistry] = None,
    llm: Optional[LLMClient] = None,
    records: Optional[Iterable[JobRecord]] = None,
) -> tuple[list[WikiEntry], dict[str, int], str, str]:
    """Score existing records using cached evidence and the current profile.

    The function is read-only with respect to the registry, sources, Gmail and
    network.  It may create reusable assessment-cache entries after successful
    local-evidence scoring; callers that need a zero-write preview should use
    a temporary runtime/cache directory.
    """
    runtime = runtime or resolve_principal_runtime(profile_name)
    registry = registry or JobRegistry(runtime.cache_dir / "jobs.csv")
    profile_text = config.load_profile_text(profile_name, role=role)
    llm = llm or LLMClient(max_tokens=min(config.LLM_MAX_TOKENS, REASSESSMENT_MAX_TOKENS))
    namespace = assessment_cache_namespace(
        profile_name, role, getattr(llm, "model", config.LLM_MODEL), profile_text
    )
    matcher = JobMatcher(
        llm,
        profile_text,
        registry=registry,
        # This is a replay of retained material, not an opportunity to inspect
        # the principal's mailbox or change a Gmail read state.
        use_history=False,
        history_kwargs={"applications_dir": runtime.applications_dir},
    )
    matcher = AssessmentCachingMatcher(
        matcher, namespace=namespace, cache_dir=runtime.cache_dir / "assessments"
    )
    matcher = EvidenceGroundedMatcher(
        matcher, PrincipalEvidenceIndex(profile_text, config.load_bio(profile_name))
    )
    enricher = LeadEnricher(cache_dir=runtime.cache_dir / "enrichment")

    all_records = list(records) if records is not None else registry.all_jobs()
    entries: list[WikiEntry] = []
    coverage = {"total": len(all_records), "cached": 0, "guarded": 0, "unscored": 0}
    for record in all_records:
        description = _record_description(record)
        evidence = enricher.load_cached(description, record.url)
        if not evidence.primary_text:
            deterministic = _deterministic_cached_only_result(profile_text, record)
            if deterministic is not None:
                entries.append(_entry_from_match(record, deterministic))
                coverage["guarded"] += 1
            else:
                entries.append(
                    _unscored_entry(
                        record,
                        "No retained primary advert text. Cached-only replay did not fetch the web.",
                    )
                )
                coverage["unscored"] += 1
            continue

        result = matcher.match(
            record.title,
            evidence.matcher_description,
            record.org,
            job_url=record.url,
            location="",
        )
        result.primary_content_hash = evidence.primary_content_hash
        result.primary_fetched_at = evidence.primary_fetched_at
        result.primary_fetch_method = evidence.primary_fetch_method
        entries.append(_entry_from_match(record, result))
        coverage["cached"] += 1

    return entries, coverage, profile_text, namespace


def write_reassessment_report(
    entries: list[WikiEntry],
    coverage: dict[str, int],
    *,
    profile_name: str,
    role: str,
    namespace: str,
    reports_dir: Path,
) -> Path:
    """Write a distinct report without overwriting historical crawl output."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    path = reports_dir / (
        f"{now.strftime('%Y-%m-%d-%H%M%S')}-{profile_name.lower()}-"
        f"{role}-reassessment.md"
    )
    lines = [
        f"# LoveWork Cached-Evidence Reassessment — {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        f"Profile: **{profile_name.upper()}-{role}**",
        "",
        "This is a profile-revision replay of retained principal state, not a crawl.",
        "It did not fetch the web, access Gmail, change registry lifecycle, or rewrite historical org pages/reports.",
        "",
        "## Coverage",
        "",
        f"- Registry records considered: **{coverage['total']}**",
        f"- Re-scored from cached primary advert evidence: **{coverage['cached']}**",
        f"- Deterministic exclusions without a primary page: **{coverage['guarded']}**",
        f"- UNSCORED for lack of cached primary evidence: **{coverage['unscored']}**",
        f"- Assessment cache namespace: `{namespace}`",
        "",
    ]
    for section in ("GO", "MAYBE", "FLAG", "DROP"):
        items = [entry for entry in entries if entry.decision == section]
        lines.extend([f"## {section} ({len(items)})", ""])
        for entry in sorted(items, key=lambda item: item.score, reverse=True):
            _format_entry(lines, entry)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Cached-evidence reassessment report saved to %s", path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True, help="Principal profile, e.g. vj")
    parser.add_argument("--role", required=True, help="Current role criteria file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Score and print a summary without writing a report, ledger, or assessment cache",
    )
    args = parser.parse_args()

    runtime = resolve_principal_runtime(args.profile)
    live_registry = JobRegistry(runtime.cache_dir / "jobs.csv")
    if args.dry_run:
        # Use a temporary cache boundary so even successful assessments cannot
        # persist a new cache entry during preview.
        import tempfile
        from dataclasses import replace

        with tempfile.TemporaryDirectory(prefix="lovework-reassess-") as temp_dir:
            preview_runtime = replace(runtime, cache_dir=Path(temp_dir) / "cache")
            entries, coverage, _, namespace = reassess_records(
                args.profile, args.role, runtime=preview_runtime, registry=live_registry
            )
    else:
        entries, coverage, profile_text, namespace = reassess_records(
            args.profile, args.role, runtime=runtime, registry=live_registry
        )
        report = write_reassessment_report(
            entries,
            coverage,
            profile_name=args.profile,
            role=args.role,
            namespace=namespace,
            reports_dir=runtime.wiki_root / "reports",
        )
        run_id = new_run_id()
        append_run(
            runtime.dataset_dir,
            run_id=run_id,
            profile_name=args.profile,
            role=args.role,
            sources=[REASSESSMENT_SOURCE],
            profile_text=profile_text,
            model=getattr(LLMClient(), "model", ""),
            provider=getattr(config, "LLM_PROVIDER", ""),
        )
        append_assessments(
            runtime.dataset_dir,
            entries,
            run_id=run_id,
            profile_name=args.profile,
            role=args.role,
            sources=[REASSESSMENT_SOURCE],
        )
        print(f"Report: {report}")

    counts = {decision: sum(e.decision == decision for e in entries) for decision in ("GO", "MAYBE", "FLAG", "DROP")}
    print(
        "Reassessment: "
        + ", ".join(f"{decision}={counts[decision]}" for decision in counts)
        + f"; cached={coverage['cached']}; guarded={coverage['guarded']}; unscored={coverage['unscored']}"
    )
    if args.dry_run:
        print("Preview only: no report, ledger, registry, Gmail, network fetch, or cache writes.")


if __name__ == "__main__":
    main()
