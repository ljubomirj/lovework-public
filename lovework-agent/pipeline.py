#!/usr/bin/env python3
"""
LoveWork — core crawl→registry→match→wiki pipeline, as an importable library.

This module exists so the pipeline can be driven three ways without duplication:
  1. The CLI (``main.py``) — a thin argparse wrapper that calls ``run_pipeline``.
  2. The interactive agent (``agent.run_autonomous``) — calls ``run_pipeline`` directly.
  3. A future FastAPI service (Phase 3, lovework.be) — imports ``run_pipeline`` and
     passes per-user ``registry`` / ``wiki`` instead of subprocess-ing the CLI.

Design rule (Phase-3 discipline): every collaborator is optional and defaults to a
config-driven singleton. A caller that wants isolation (multi-tenant server, tests)
injects its own ``registry`` / ``llm`` / ``wiki``. No globals are read inside the
pipeline body except via the injected objects.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Tuple

import config
from assessment_cache import AssessmentCachingMatcher, assessment_cache_namespace
from principal_runtime import PrincipalRuntime, resolve_principal_runtime
from principal_evidence import PrincipalEvidenceIndex, EvidenceGroundedMatcher
from crawler import SmartCrawler
from enrichment import EnrichingMatcher, LeadEnricher
from job_registry import JobRegistry
from llm_client import LLMClient
from matcher import JobMatcher
from wiki_store import WikiEntry, WikiStore

from sources.research_orgs import ResearchOrgsSource
from sources.neolabs import NeolabsSource
from sources.hf_startups import HFStartupsSource
from sources.hn_hiring import HNHiringSource
from sources.hn_jobs import HNHiringJobsSource
from sources.gmail_lj_jobs import GmailJobsSource, GmailLjJobsSource
from sources.linkedin_related import LinkedInRelatedSource
from sources.company_pages import CompanyPagesSource
from sources.harnham import HarnhamSource

logger = logging.getLogger("lovework-agent")

ALL_SOURCES = [
    "research_orgs",
    "neolabs",
    "hf_startups",
    "hn_hiring",
    "hn_jobs",
    "gmail_lj_jobs",
    "linkedin_related",
    "company_pages",
    "harnham",
]

# The local research-org, NeoLab and HF-startup trackers were curated for LJ's
# AI/ML search. They are not neutral “all jobs” sources. VJ's revised
# statistics/pricing/actuarial search must not inherit that AI-lab bias.
VJ_SOURCES = [
    "hn_hiring",
    "hn_jobs",
    "linkedin_related",
]


def sources_for_profile(profile_name: str, runtime: PrincipalRuntime) -> List[str]:
    """Return sources that have an explicit policy for this principal."""
    if profile_name.lower() == "lj":
        return ALL_SOURCES.copy()

    if profile_name.lower() == "vj":
        sources = VJ_SOURCES.copy()
        if runtime.gmail_mailbox is not None:
            sources.insert(0, runtime.gmail_mailbox.source_name)
        return sources

    # Company pages and Harnham still write LJ-maintained source policy. Do
    # not quietly run either for another person until they accept a profile
    # parameter. The public sources and principal-owned LinkedIn seeds are safe.
    sources = [
        "research_orgs", "neolabs", "hf_startups", "hn_hiring", "hn_jobs", "linkedin_related",
    ]
    if runtime.gmail_mailbox is not None:
        sources.insert(5, runtime.gmail_mailbox.source_name)
    return sources


def run_source(
    source_name: str,
    crawler: SmartCrawler,
    matcher: JobMatcher,
    registry: JobRegistry,
    runtime: PrincipalRuntime,
    dry_run: bool = False,
) -> List[WikiEntry]:
    """Run a single data source, returning its wiki entries."""
    if source_name == "research_orgs":
        return ResearchOrgsSource(crawler, matcher, registry).run()
    if source_name == "neolabs":
        return NeolabsSource(crawler, matcher, registry).run()
    if source_name == "hf_startups":
        return HFStartupsSource(crawler, matcher, registry).run()
    if source_name == "hn_hiring":
        return HNHiringSource(crawler, matcher, registry).run()
    if source_name == "hn_jobs":
        return HNHiringJobsSource(crawler, matcher, registry).run()
    if source_name == "gmail_lj_jobs":
        # Gmail source needs no crawler (jobs come from email); passed for uniformity.
        mailbox = runtime.gmail_mailbox
        return GmailLjJobsSource(
            crawler,
            matcher,
            registry,
            mark_read=not dry_run,
            label=mailbox.label if mailbox is not None else None,
            credential_home=mailbox.credential_home if mailbox is not None else None,
            sources_dir=runtime.sources_dir,
            capture_seeds=not dry_run,
        ).run()
    if runtime.gmail_mailbox is not None and source_name == runtime.gmail_mailbox.source_name:
        mailbox = runtime.gmail_mailbox
        return GmailJobsSource(
            crawler,
            matcher,
            registry,
            mark_read=not dry_run,
            label=mailbox.label,
            credential_home=mailbox.credential_home,
            source_name=mailbox.source_name,
            sources_dir=runtime.sources_dir,
            capture_seeds=not dry_run,
        ).run()
    if source_name == "linkedin_related":
        return LinkedInRelatedSource(crawler, matcher, registry, sources_dir=runtime.sources_dir).run()
    if source_name == "company_pages":
        return CompanyPagesSource(crawler, matcher, registry).run()
    if source_name == "harnham":
        return HarnhamSource(crawler, matcher, registry).run()
    raise ValueError(f"Unknown source: {source_name}")


def run_pipeline(
    profile_name: str,
    role: Optional[str] = None,
    source: str = "all",
    *,
    use_dspy: bool = False,
    dry_run: bool = False,
    write_report: bool = True,
    snapshot: bool = True,
    registry: Optional[JobRegistry] = None,
    llm: Optional[LLMClient] = None,
    crawler: Optional[SmartCrawler] = None,
    matcher: Optional[JobMatcher] = None,
    wiki: Optional[WikiStore] = None,
    runtime: Optional[PrincipalRuntime] = None,
) -> Tuple[List[WikiEntry], int]:
    """Run the full discovery pipeline.

    Flow: load profile → for each source, crawl org sites (LLM-guided) → upsert each
    job into the registry (lifecycle tracking) → match against the profile (LLM-scored,
    with prior-contact context) → mark disappeared jobs → write the wiki.

    Args:
        profile_name: principal profile (e.g. "lj", "vj").
        role: optional role file under profiles/<name>/roles/.
        source: "all" or one of ALL_SOURCES.
        use_dspy: use DSPy typed signatures instead of legacy prompts.
        dry_run: run a non-persistent preview. It does not update principal
            state, registry lifecycle, Gmail read state, source seeds, reports,
            ledgers, or caches.
        write_report: write the dated markdown report (org pages + index are always
            written when not dry-run; the report is the only optional piece).
        snapshot: archive cache state before mutation. Incremental crawl takes one
            outer snapshot and disables the inner per-source snapshots.
        registry/llm/crawler/matcher/wiki: optional injected collaborators. When
            omitted, config-driven defaults are constructed. A Phase-3 server passes
            per-user ``registry`` and ``wiki``.
        runtime: optional principal state boundary. When omitted, VJ/KJ/PK use
            visible ``state/<principal>/`` data; LJ retains legacy paths pending
            the separately documented migration.

    Returns:
        (all_entries, disappeared_count) — the collected wiki entries and how many
        jobs were marked disappeared at the end of this run.
    """
    profile_text = config.load_profile_text(profile_name, role=role)
    runtime = runtime or resolve_principal_runtime(profile_name)
    preview_dir = None
    if dry_run:
        preview_dir = tempfile.TemporaryDirectory(prefix="lovework-preview-")
        preview_root = Path(preview_dir.name)
        runtime = replace(
            runtime,
            cache_dir=preview_root / "cache",
            wiki_root=preview_root / "wiki",
            dataset_dir=preview_root / "dataset",
            sources_dir=preview_root / "sources",
        )
    available_sources = sources_for_profile(profile_name, runtime)
    sources = available_sources if source == "all" else [source]
    unknown_sources = set(sources) - set(available_sources)
    if unknown_sources:
        raise ValueError(
            f"Source(s) unavailable for {profile_name}: {sorted(unknown_sources)}. "
            f"Available: {available_sources}"
        )
    run_id = ""

    # Inject collaborators or construct config-driven defaults.
    registry = registry if registry is not None else JobRegistry(runtime.cache_dir / "jobs.csv")
    llm = llm if llm is not None else LLMClient()
    crawler = crawler if crawler is not None else SmartCrawler(
        llm, use_dspy=use_dspy, cache_dir=runtime.cache_dir
    )
    if matcher is None:
        if use_dspy:
            from matcher import JobMatcherDSPyAdapter

            matcher = JobMatcherDSPyAdapter(
                profile_text, registry=registry, use_history=True,
                history_kwargs=_history_kwargs(runtime),
            )
        else:
            matcher = JobMatcher(
                llm, profile_text, registry=registry, use_history=True,
                history_kwargs=_history_kwargs(runtime),
            )
    cache_namespace = assessment_cache_namespace(
        profile_name,
        role,
        getattr(llm, "model", config.LLM_MODEL),
        profile_text,
    )
    matcher = AssessmentCachingMatcher(
        matcher, namespace=cache_namespace, cache_dir=runtime.cache_dir / "assessments"
    )
    matcher = EvidenceGroundedMatcher(
        matcher,
        PrincipalEvidenceIndex(profile_text, config.load_bio(profile_name)),
    )
    matcher = EnrichingMatcher(
        matcher,
        LeadEnricher(
            cache_dir=runtime.cache_dir / "enrichment",
            crawler_cache_dir=runtime.cache_dir,
        ),
    )
    wiki = wiki if wiki is not None else WikiStore(root=runtime.wiki_root)

    if not dry_run:
        try:
            from snapshot import append_run, new_run_id

            run_id = new_run_id()
            append_run(
                runtime.dataset_dir,
                run_id=run_id,
                profile_name=profile_name,
                role=role or "default",
                sources=sources,
                profile_text=profile_text,
                model=getattr(llm, "model", ""),
                provider=getattr(config, "LLM_PROVIDER", ""),
            )
        except Exception as e:
            logger.warning(f"Run ledger append failed: {e}")

    # Snapshot the registry before the crawl modifies it.
    if snapshot and not dry_run:
        try:
            from snapshot import snapshot_cache
            snapshot_cache(runtime.cache_dir)
        except Exception as e:
            logger.warning(f"Cache snapshot failed: {e}")

    all_entries: List[WikiEntry] = []

    for src in sources:
        logger.info(f"Running source: {src}")
        try:
            # A preview must not advance registry lifecycle state or consume
            # principal Gmail. Sources see no writable registry and Gmail gets
            # mark_read=False/capture_seeds=False above.
            source_registry = None if dry_run else registry
            entries = run_source(src, crawler, matcher, source_registry, runtime, dry_run)
            all_entries.extend(entries)
            logger.info(f"Source {src} produced {len(entries)} entries")
        except Exception as e:
            logger.error(f"Source {src} failed: {e}")

    # Mark run complete: still_open jobs from the sources we DIDN'T run
    # are left untouched (their absence is not a signal). Only jobs from
    # `sources` get the disappeared / long_lasting treatment.
    if dry_run:
        disappeared_count = 0
        logger.info("Preview completed without registry lifecycle updates")
    else:
        disappeared_count = registry.mark_run_complete(sources_run=sources)
        logger.info(f"Marked {disappeared_count} jobs as disappeared (from {sources})")

    if not dry_run:
        try:
            from snapshot import append_assessments, append_passive_outcomes
            append_assessments(
                runtime.dataset_dir,
                all_entries,
                run_id=run_id,
                profile_name=profile_name,
                role=role or "default",
                sources=sources,
            )
            append_passive_outcomes(
                runtime.dataset_dir,
                all_entries,
                run_id=run_id,
                use_gmail=True,
                history_kwargs=_history_kwargs(runtime),
            )
        except Exception as e:
            logger.warning(f"Dataset append failed: {e}")
        pack_results = []
        if profile_name.lower() == "lj":
            try:
                from application_packs import prepare_go_cases

                pack_results = prepare_go_cases(all_entries, only_new=True)
                for result in pack_results:
                    result.entry.case_pack_result = result
            except Exception as e:
                logger.warning(f"LoveWork application-pack preparation failed: {e}")
        for entry in all_entries:
            try:
                wiki.update_org_page(entry)
            except Exception as e:
                logger.warning(f"Wiki update failed for {entry.org_name}: {e}")
        if write_report:
            try:
                role_label = role or "default"
                profile_label = f"{profile_name.upper()}-{role_label}"
                wiki.save_report(
                    all_entries,
                    profile_name=profile_label,
                    run_type="full",
                    pack_results=pack_results,
                )
            except Exception as e:
                logger.warning(f"Wiki report failed: {e}")
        try:
            wiki.rebuild_index(all_entries)
        except Exception as e:
            logger.warning(f"Wiki index rebuild failed: {e}")

    if preview_dir is not None:
        preview_dir.cleanup()
    return all_entries, disappeared_count


def _history_kwargs(runtime: PrincipalRuntime) -> dict:
    """Pass one principal's prior-contact scope into the matcher."""
    kwargs = {"applications_dir": runtime.applications_dir}
    if runtime.gmail_mailbox is not None:
        kwargs["gmail_label"] = runtime.gmail_mailbox.label
        kwargs["gmail_credential_home"] = runtime.gmail_mailbox.credential_home
    return kwargs
