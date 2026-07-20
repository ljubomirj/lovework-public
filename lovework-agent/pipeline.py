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
from typing import List, Optional, Tuple

import config
from assessment_cache import AssessmentCachingMatcher
from candidate_evidence import CandidateEvidenceIndex, EvidenceGroundedMatcher
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
from sources.gmail_lj_jobs import GmailLjJobsSource
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


def run_source(
    source_name: str,
    crawler: SmartCrawler,
    matcher: JobMatcher,
    registry: JobRegistry,
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
        return GmailLjJobsSource(crawler, matcher, registry).run()
    if source_name == "linkedin_related":
        return LinkedInRelatedSource(crawler, matcher, registry).run()
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
) -> Tuple[List[WikiEntry], int]:
    """Run the full discovery pipeline.

    Flow: load profile → for each source, crawl org sites (LLM-guided) → upsert each
    job into the registry (lifecycle tracking) → match against the profile (LLM-scored,
    with prior-contact context) → mark disappeared jobs → write the wiki.

    Args:
        profile_name: candidate profile (e.g. "lj", "vj").
        role: optional role file under profiles/<name>/roles/.
        source: "all" or one of ALL_SOURCES.
        use_dspy: use DSPy typed signatures instead of legacy prompts.
        dry_run: skip all wiki writes.
        write_report: write the dated markdown report (org pages + index are always
            written when not dry-run; the report is the only optional piece).
        snapshot: archive cache state before mutation. Incremental crawl takes one
            outer snapshot and disables the inner per-source snapshots.
        registry/llm/crawler/matcher/wiki: optional injected collaborators. When
            omitted, config-driven defaults are constructed. A Phase-3 server passes
            per-user ``registry`` and ``wiki``.

    Returns:
        (all_entries, disappeared_count) — the collected wiki entries and how many
        jobs were marked disappeared at the end of this run.
    """
    profile_text = config.load_profile_text(profile_name, role=role)
    sources = ALL_SOURCES if source == "all" else [source]
    run_id = ""

    # Inject collaborators or construct config-driven defaults.
    registry = registry if registry is not None else JobRegistry()
    llm = llm if llm is not None else LLMClient()
    crawler = crawler if crawler is not None else SmartCrawler(llm, use_dspy=use_dspy)
    if matcher is None:
        if use_dspy:
            from matcher import JobMatcherDSPyAdapter

            matcher = JobMatcherDSPyAdapter(profile_text, registry=registry, use_history=True)
        else:
            matcher = JobMatcher(llm, profile_text, registry=registry, use_history=True)
    cache_namespace = (
        f"matcher-v5:{profile_name}:{role or 'default'}:"
        f"{getattr(llm, 'model', config.LLM_MODEL)}"
    )
    matcher = AssessmentCachingMatcher(matcher, namespace=cache_namespace)
    matcher = EvidenceGroundedMatcher(
        matcher,
        CandidateEvidenceIndex(profile_text, config.load_bio(profile_name)),
    )
    matcher = EnrichingMatcher(matcher, LeadEnricher())
    wiki = wiki if wiki is not None else WikiStore()

    if not dry_run:
        try:
            from snapshot import append_run, new_run_id

            run_id = new_run_id()
            append_run(
                config.DATASET_DIR,
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
    if snapshot:
        try:
            from snapshot import snapshot_cache
            snapshot_cache(config.CACHE_DIR)
        except Exception as e:
            logger.warning(f"Cache snapshot failed: {e}")

    all_entries: List[WikiEntry] = []

    for src in sources:
        logger.info(f"Running source: {src}")
        try:
            entries = run_source(src, crawler, matcher, registry)
            all_entries.extend(entries)
            logger.info(f"Source {src} produced {len(entries)} entries")
        except Exception as e:
            logger.error(f"Source {src} failed: {e}")

    # Mark run complete: still_open jobs from the sources we DIDN'T run
    # are left untouched (their absence is not a signal). Only jobs from
    # `sources` get the disappeared / long_lasting treatment.
    disappeared_count = registry.mark_run_complete(sources_run=sources)
    logger.info(f"Marked {disappeared_count} jobs as disappeared (from {sources})")

    if not dry_run:
        try:
            from snapshot import append_assessments, append_passive_outcomes
            append_assessments(
                config.DATASET_DIR,
                all_entries,
                run_id=run_id,
                profile_name=profile_name,
                role=role or "default",
                sources=sources,
            )
            append_passive_outcomes(
                config.DATASET_DIR,
                all_entries,
                run_id=run_id,
                use_gmail=True,
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
                    pack_results=pack_results,
                )
            except Exception as e:
                logger.warning(f"Wiki report failed: {e}")
        try:
            wiki.rebuild_index(all_entries)
        except Exception as e:
            logger.warning(f"Wiki index rebuild failed: {e}")

    return all_entries, disappeared_count
