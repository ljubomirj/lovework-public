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
    ../venv/bin/python3 reassess.py --profile kj --role general \
        --from-principal lj --source neolabs --dry-run

`--from-principal` scores adverts retained by *another* principal's registry and
advert cache against this principal's profile.  It is read-only with respect to
the origin principal: nothing in that principal's state is read-modify-written.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import config
from assessment_cache import AssessmentCachingMatcher, assessment_cache_namespace
from principal_evidence import PrincipalEvidenceIndex, EvidenceGroundedMatcher
from writer_boundary import WriterBusy, standalone_writer_guard  # noqa: E402
from principal_runtime import PrincipalRuntime, resolve_principal_runtime
from enrichment import LeadEnricher
from job_registry import ABSENCE_IS_NOT_EVIDENCE, JobRecord, JobRegistry
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
from scoring_policy import apply_run_policy
from report_header import section_heading
from wiki_store import WikiStore, WikiEntry, _format_entry

logger = logging.getLogger(__name__)

REASSESSMENT_SOURCE = "cached_reassessment"
# Reassessment asks for concise JSON fields, not an essay, so this was once held
# below the crawl budget to save latency and tokens. That cap silently broke the
# replay: at 2200 the model returns `finish_reason=length` with ~300 characters
# of truncated JSON, every match fails to parse, and every advert lands
# UNSCORED (2026-09-20, the first full LJ replay). The budget must therefore
# match a crawl's unless an operator deliberately lowers it.
REASSESSMENT_MAX_TOKENS = int(
    os.getenv("LOVEWORK_REASSESSMENT_MAX_TOKENS", str(config.LLM_MAX_TOKENS))
)

#: Log a progress line every N records. A multi-hour replay is otherwise
#: silent, so a stalled LLM call is indistinguishable from slow work.
PROGRESS_EVERY = 25

#: How a replay treats records the source has stopped listing. `drop` (default)
#: decides them without the LLM: they are no longer advertised, so scoring them
#: spends a call re-asking about a dead advert and can hand back APPLY_NOW — VJ
#: 2026-09-21 surfaced Mangroup (7.0) and Iwoca (8.4) that way, both `disappeared`
#: in our own registry and both with dead links. `score` restores the old
#: behaviour for a deliberate re-read of history.
REASSESS_DISAPPEARED = os.getenv("LOVEWORK_REASSESS_DISAPPEARED", "drop").strip().lower()


def select_records(records: Iterable[JobRecord], sources: Optional[str]) -> list[JobRecord]:
    """Restrict records to the named sources (comma-separated), preserving order.

    `sources=None` keeps every record.  Used by the cross-principal replay so a
    principal can consume only the origin's source(s) that are relevant to it.
    """
    items = list(records)
    if not sources:
        return items
    wanted = {name.strip() for name in sources.split(",") if name.strip()}
    return [record for record in items if (record.source or "").strip() in wanted]


def _record_description(record: JobRecord) -> str:
    """Supply minimal retained context alongside a cached primary advert."""
    return (
        "Historical cached-evidence reassessment. "
        f"Advert title: {record.title}. "
        f"Original careers page: {record.careers_url or 'unspecified'}."
    )


def _disappeared_entry(record: JobRecord) -> WikiEntry:
    """A lead the source has stopped listing, decided without the LLM.

    `disappeared` is our own lifecycle fact: the advert was absent from the last
    run that should have carried it, so the role is no longer advertised (a VJ
    GO entry surfaced this way still returned HTTP 404 on 2026-09-21). Scoring
    such a record re-asks the LLM about a dead advert and can hand back
    APPLY_NOW — Mangroup did, at 7.0 — so it is excluded deterministically and
    reported as a DROP that says why.
    """
    return WikiEntry(
        org_name=record.org,
        title=record.title,
        url=record.url,
        location="",
        score=0.0,
        decision="DROP",
        reasoning=(
            f"NO LONGER ADVERTISED: our registry last saw this advert on "
            f"{record.last_seen or '?'} and the source has since dropped it "
            f"(lifecycle: disappeared) — not re-scored"
        ),
        source=record.source or REASSESSMENT_SOURCE,
        lifecycle_status=record.status,
        first_seen=record.first_seen,
        discovery_url=record.discovery_url,
        discovery_date=record.discovery_date,
        recommended_action="DROP",
        assessment_status="UNSCORED",
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


def _empty_coverage(total: int = 0) -> dict[str, int]:
    """The replay's counters — one shape for every return path."""
    return {"total": total, "cached": 0, "guarded": 0, "unscored": 0,
            "disappeared": 0, "seniority_capped": 0, "cap_country": 0}


def reassess_records(
    profile_name: str,
    role: str,
    *,
    runtime: Optional[PrincipalRuntime] = None,
    registry: Optional[JobRegistry] = None,
    llm: Optional[LLMClient] = None,
    records: Optional[Iterable[JobRecord]] = None,
    evidence_cache_dir: Optional[Path] = None,
    write_evidence_cache_dir: Optional[Path] = None,
) -> tuple[list[WikiEntry], dict[str, int], str, str]:
    """Score existing records using cached evidence and the current profile.

    `records` defaults to this principal's registry; `evidence_cache_dir`
    defaults to this principal's enrichment cache.  A cross-principal replay
    passes the origin principal's records and cache directory, while scoring,
    the assessment cache, the report and the ledger stay with this principal.

    The function is read-only with respect to the registry, sources, Gmail and
    network.  It may create reusable assessment-cache entries after successful
    local-evidence scoring; callers that need a zero-write preview should use
    a temporary runtime/cache directory.
    """
    runtime = runtime or resolve_principal_runtime(profile_name)
    registry = registry or JobRegistry(runtime.cache_dir / "jobs.csv")
    profile_text = config.load_profile_text(profile_name, role=role)
    records = list(records if records is not None else registry.all_jobs())
    if not records:
        # Nothing to score. A preview over an empty registry must not need a
        # credential — `LLMClient()` raises without a key, and constructing one
        # to be handed no work is not a check of anything (2026-11-07: the
        # dry-run entry point could not start on a host with no key).
        return [], _empty_coverage(), profile_text, assessment_cache_namespace(
            profile_name, role, config.LLM_MODEL, profile_text)
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
    enricher = LeadEnricher(
        cache_dir=evidence_cache_dir or (runtime.cache_dir / "enrichment"),
        # Reads may come from an owner's sealed bundle; anything NEW is
        # written to this principal's own cache, never into a seal.
        write_cache_dir=write_evidence_cache_dir or (runtime.cache_dir / "enrichment"),
    )

    all_records = list(records) if records is not None else registry.all_jobs()
    entries: list[WikiEntry] = []
    coverage = _empty_coverage(len(all_records))
    started = time.monotonic()
    logger.info(
        "Replay starting: %d registry record(s) to score against %s/%s "
        "(progress every %d; a stall shows as silence)",
        len(all_records), runtime.profile_name, role, PROGRESS_EVERY,
    )
    for index, record in enumerate(all_records, start=1):
        if index % PROGRESS_EVERY == 0 or index == len(all_records):
            elapsed = time.monotonic() - started
            rate = index / elapsed * 60 if elapsed else 0.0
            logger.info(
                "Progress: %d/%d records (%.1f/min, %.0fs elapsed; "
                "cached=%d guarded=%d unscored=%d)",
                index, len(all_records), rate, elapsed,
                coverage["cached"], coverage["guarded"], coverage["unscored"],
            )
        if (REASSESS_DISAPPEARED == "drop"
                and (record.status or "").lower() == "disappeared"
                and (getattr(record, "source", "") or "") not in ABSENCE_IS_NOT_EVIDENCE):
            # Decided before the evidence load and the matcher call: the advert
            # is gone from the source, so there is nothing to re-grade.
            entries.append(_disappeared_entry(record))
            coverage["disappeared"] += 1
            continue
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

    # The same run-level ceilings a crawl applies (an early-career principal is
    # not hired into senior roles; declared countries are not ranked high). The
    # replay builds entries itself, so it has to ask for the policy explicitly —
    # without this a reassessment would rank differently from the crawl it is
    # meant to re-grade.
    policy = apply_run_policy(entries, profile_text)
    coverage["seniority_capped"] = policy["seniority"]
    coverage["cap_country"] = policy["cap_country"]

    return entries, coverage, profile_text, namespace


def write_reassessment_report(
    entries: list[WikiEntry],
    coverage: dict[str, int],
    *,
    profile_name: str,
    role: str,
    namespace: str,
    reports_dir: Path,
    origin_principal: Optional[str] = None,
    sources_filter: Optional[str] = None,
) -> Path:
    """Write a distinct report without overwriting historical crawl output."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    cross = f"-cross-{origin_principal.lower()}" if origin_principal else ""
    path = reports_dir / (
        f"{now.strftime('%Y-%m-%d-%H%M%S')}-{profile_name.lower()}-"
        f"{role}{cross}-reassessment.md"
    )
    lines = [
        f"# LoveWork Cached-Evidence Reassessment — {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        f"Profile: **{profile_name.upper()}-{role}**",
        "",
    ]
    if origin_principal:
        lines.extend([
            f"Evidence origin: **{origin_principal.upper()}** retained registry and cached primary adverts (read-only).",
            (
                f"Sources replayed: `{sources_filter}`."
                if sources_filter
                else "Sources replayed: all sources retained by the origin principal."
            ),
            "",
            f"This is a cross-principal replay: **{profile_name.upper()}** is scored against adverts"
            f" retained by **{origin_principal.upper()}**'s registry and advert cache. Neither registry"
            " was modified, no web/Gmail access occurred, and lifecycle state is unchanged.",
            "Leads are new to this principal unless it already tracked the same advert.",
            "",
        ])
    else:
        lines.extend([
            "This is a profile-revision replay of retained principal state, not a crawl.",
            "It did not fetch the web, access Gmail, change registry lifecycle, or rewrite historical org pages/reports.",
            "",
        ])
    lines.extend([
        "## Coverage",
        "",
        f"- Registry records considered: **{coverage['total']}**",
        f"- Re-scored from cached primary advert evidence: **{coverage['cached']}**",
        f"- Deterministic exclusions without a primary page: **{coverage['guarded']}**",
        f"- UNSCORED for lack of cached primary evidence: **{coverage['unscored']}**",
        f"- No longer advertised — the source dropped it, so not re-scored: "
        f"**{coverage.get('disappeared', 0)}**",
        f"- Demoted by run policy — senior-titled for an early-career principal: "
        f"**{coverage.get('seniority_capped', 0)}**; declared non-English "
        f"location: **{coverage.get('cap_country', 0)}**",
        f"- Assessment cache namespace: `{namespace}`",
        "",
    ])
    principal_key = str(profile_name).split("-")[0].lower()
    store = WikiStore(root=Path(reports_dir).parent)
    for section in ("GO", "MAYBE", "FLAG", "DROP"):
        items = [entry for entry in entries if entry.decision == section]
        lines.extend([section_heading(section, len(items)), ""])
        for index, entry in enumerate(
            sorted(items, key=lambda item: item.score, reverse=True), start=1
        ):
            # Same entry shape as a crawl report: numbered heading (the readers,
            # stale_audit among them, key on the ordinal), the *principal's* own
            # org-page links — not `_format_entry`'s "lj" default, which put VJ
            # and KJ reassessment links under /principals/lj/ — and the case-folded
            # stem so the link matches the page that exists.
            _format_entry(
                lines, entry, index=index, principal=principal_key,
                org_stem=store.org_page_path(entry.org_name).stem,
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Cached-evidence reassessment report saved to %s", path)
    # The operation runner registers the produced artifact by parsing this
    # marker out of stdout (crawl_operation.py) — never "newest by mtime".
    print(f"[REPORT_PATH] {path}", flush=True)
    return path


def _profile_from_argv(argv: list[str]) -> str:
    """The principal named on the command line (`--profile X` / `--profile=X`)."""
    for i, arg in enumerate(argv):
        if arg == "--profile" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--profile="):
            return arg.split("=", 1)[1]
    return "lj"


def main() -> None:
    """Direct invocation: hold the principal's writer lease, then re-score.

    WP1b box ① — a standalone reassessment must not race a seal or another
    crawl; an operation child carries the marker and skips the acquire.
    """
    principal = _profile_from_argv(sys.argv[1:])
    # Rule: the lease means "this invocation may write the principal's state".
    # A `--dry-run` preview matches on a scratch cache and writes nothing, so it
    # is a pure reader and must not contend with a real crawl.
    if "--dry-run" in sys.argv[1:]:
        return _reassess_main()
    state_root = resolve_principal_runtime(principal).cache_dir.parent
    try:
        with standalone_writer_guard(principal, mode="direct", state_root=state_root):
            return _reassess_main()
    except WriterBusy as busy:
        print(f"[LOCK] writer busy: {busy.resource.key} — another operation holds "
              f"this principal's lease (attempt recorded at {busy.attempt_path})")
        raise SystemExit(75)


def _reassess_main() -> None:
    # Only the CLI entry point configures logging (importing this module must
    # have no logging side effects). Without it a replay is silent for its whole
    # multi-hour run: the root logger drops INFO, so the first sign of life was
    # the final report marker and a stalled run could not be diagnosed
    # (2026-09-20, the first full LJ replay).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True, help="Principal profile, e.g. vj")
    parser.add_argument("--role", required=True, help="Current role criteria file")
    parser.add_argument(
        "--from-principal",
        help="Score another principal's retained registry + cached adverts (read-only) instead of this principal's",
    )
    parser.add_argument(
        "--source",
        help="Comma-separated source names to restrict the replay to (e.g. neolabs,neoclouds)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Score and print a summary without writing a report, ledger, or assessment cache",
    )
    args = parser.parse_args()
    config.set_active_profile(args.profile)

    runtime = resolve_principal_runtime(args.profile)
    live_registry = JobRegistry(runtime.cache_dir / "jobs.csv")

    origin = (args.from_principal or "").strip().lower() or None
    if origin and origin == args.profile.lower():
        parser.error("--from-principal must name a different principal than --profile")
    if origin:
        origin_runtime = resolve_principal_runtime(origin)
        records = select_records(
            JobRegistry(origin_runtime.cache_dir / "jobs.csv").all_jobs(), args.source
        )
        evidence_cache_dir = origin_runtime.cache_dir / "enrichment"
        if not records:
            parser.error(
                f"no records selected from {origin} (source filter: {args.source or 'all'})"
            )
    else:
        records = select_records(live_registry.all_jobs(), args.source)
        evidence_cache_dir = None

    if args.dry_run:
        # Use a temporary cache boundary so even successful assessments cannot
        # persist a new cache entry during preview.
        import tempfile
        from dataclasses import replace

        with tempfile.TemporaryDirectory(prefix="lovework-reassess-") as temp_dir:
            preview_runtime = replace(runtime, cache_dir=Path(temp_dir) / "cache")
            entries, coverage, _, namespace = reassess_records(
                args.profile,
                args.role,
                runtime=preview_runtime,
                registry=live_registry,
                records=records,
                evidence_cache_dir=evidence_cache_dir,
            )
    else:
        entries, coverage, profile_text, namespace = reassess_records(
            args.profile,
            args.role,
            runtime=runtime,
            registry=live_registry,
            records=records,
            evidence_cache_dir=evidence_cache_dir,
        )
        report = write_reassessment_report(
            entries,
            coverage,
            profile_name=args.profile,
            role=args.role,
            namespace=namespace,
            reports_dir=runtime.wiki_root / "reports",
            origin_principal=origin,
            sources_filter=args.source,
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
    scope = f"{args.profile}<-{origin}" if origin else args.profile
    print(
        f"Reassessment ({scope}): "
        + ", ".join(f"{decision}={counts[decision]}" for decision in counts)
        + f"; cached={coverage['cached']}; guarded={coverage['guarded']}; "
                  f"unscored={coverage['unscored']}; gone={coverage.get('disappeared', 0)}"
    )
    if args.dry_run:
        print("Preview only: no report, ledger, registry, Gmail, network fetch, or cache writes.")


if __name__ == "__main__":
    main()
