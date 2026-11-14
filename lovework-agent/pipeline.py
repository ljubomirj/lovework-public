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
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Tuple

import config
import yaml

from assessment_cache import AssessmentCachingMatcher, assessment_cache_namespace
from principal_runtime import PrincipalRuntime, resolve_principal_runtime
from principal_evidence import PrincipalEvidenceIndex, EvidenceGroundedMatcher
from crawler import SmartCrawler, ensure_fetcher_available
from enrichment import EnrichingMatcher, LeadEnricher
from git_context import describe as describe_git, render_line as render_git_line
from job_registry import JobRegistry
from llm_client import LLMClient
from matcher import JobMatcher
from wiki_store import WikiEntry, WikiStore

from sources.research_orgs import ResearchOrgsSource
from sources.neolabs import NeolabsSource
from sources.neoclouds import NeocloudsSource
from sources.speech_companies import SpeechCompaniesSource
from sources.cheminfo_companies import CheminfoCompaniesSource
from sources.compbiojobs import CompBioJobsSource
from sources.market_players import MarketPlayersSource, available_for as market_players_available
from sources.hf_startups import HFStartupsSource
from sources.hn_hiring import HNHiringSource
from sources.hn_jobs import HNHiringJobsSource
from sources.gmail_lj_jobs import GmailJobsSource, GmailLjJobsSource
from sources.linkedin_related import LinkedInRelatedSource
from sources.company_pages import CompanyPagesSource
from sources.harnham import HarnhamSource
from sources.digitaljobs import DigitalJobsSource
from sources.jackandjill import JackAndJillSource

logger = logging.getLogger("lovework-agent")

ALL_SOURCES = [
    "research_orgs",
    "neolabs",
    "neoclouds",
    # Speech/voice-AI companies (repo-owned tracker). LJ-only, like the neolab
    # and neocloud trackers: this is a curated AI-adjacent list, not a neutral
    # "all jobs" feed, so it is not inherited by other principals.
    "speech_companies",
    "hf_startups",
    "hn_hiring",
    "hn_jobs",
    "gmail_lj_jobs",
    "linkedin_related",
    "company_pages",
    "harnham",
    "digitaljobs",
    # Employer-side agent (Jill's mandates, published by Jack's network).
    # LJ only: the feed is broad Seed-Series-C tech/GTM hiring, which fits a
    # technical principal. Other principals are not silently given it.
    "jackandjill",
]

# The local research-org, NeoLab and HF-startup trackers were curated for LJ's
# AI/ML search. They are not neutral “all jobs” sources. VJ's revised
# statistics/pricing/actuarial search must not inherit that AI-lab bias.
# KJ's profession additions (2026-10-11): the cheminformatics tracker (repo-owned
# company list) and the CompBioJobs board (cheminformatics & computational-chemistry
# listings). Appended to the generalist base rather than replacing it, so the
# `consumes:` policy stays reversible — delete `profiles/kj/sources.yaml` and KJ
# crawls research_orgs/neolabs/hf_startups himself again.
KJ_EXTRA_SOURCES = [
    "cheminfo_companies",
    "compbiojobs",
]

VJ_SOURCES = [
    "hn_hiring",
    "hn_jobs",
    "linkedin_related",
    # Public structured job-board API; VJ-owned queries live in
    # profiles/vj/digitaljobs_searches.yaml (pricing/actuarial/risk/quant).
    "digitaljobs",
]


POLICY_FILENAME = "sources.yaml"

# The Jack & Jill letter is scheduled on Mondays (`lovework-lj-jnj-letter`); a
# full sweep may write it only when the Monday job has missed, so the hook waits
# longer than the letter's own 7-day cadence. See the hook in `run_pipeline`.
HOOK_MIN_INTERVAL_DAYS = int(os.getenv("LOVEWORK_JNJ_LETTER_HOOK_MIN_INTERVAL_DAYS", "10"))


def load_source_policy(profile_name: str) -> dict:
    """Read profiles/<principal>/sources.yaml, if the principal declares one.

    Shape:

        consumes:
          - source: neolabs
            owner: lj

    `consumes` names sources another principal crawls on this principal's
    behalf: they are removed from this principal's crawl set and ingested from
    the owner's corpus by `consume.py` instead. Malformed rows are ignored with
    a warning so a typo cannot silently change what gets crawled.
    """
    path = config.PROFILES_DIR / profile_name.lower() / POLICY_FILENAME
    if not path.is_file():
        return {"consumes": []}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning(f"Unreadable source policy {path}: {e}")
        return {"consumes": []}

    consumes = []
    for row in raw.get("consumes") or []:
        if not isinstance(row, dict) or not row.get("source") or not row.get("owner"):
            logger.warning(f"Ignoring malformed consumes row in {path}: {row!r}")
            continue
        owner = str(row["owner"]).strip().lower()
        source = str(row["source"]).strip()
        if owner == profile_name.lower():
            # Self-consumption would remove the source from this principal's own
            # crawl and then read its own registry as donor: a silent no-op that
            # stops the crawl. Refuse it.
            logger.warning(
                f"Ignoring self-consumption in {path}: {source} cannot be owned by "
                f"{profile_name} itself"
            )
            continue
        consumes.append({"source": source, "owner": owner})
    return {"consumes": consumes}


def consumed_sources(profile_name: str) -> dict:
    """Source -> owner for the sources this principal delegates."""
    return {row["source"]: row["owner"] for row in load_source_policy(profile_name)["consumes"]}


def sources_for_profile(profile_name: str, runtime: PrincipalRuntime) -> List[str]:
    """Return the sources this principal crawls itself.

    Sources the principal declares under `consumes` belong to another
    principal's crawl and are excluded here; `consume.py` ingests them from the
    owner's corpus instead.
    """
    if profile_name.lower() == "lj":
        base = ALL_SOURCES.copy()
    elif profile_name.lower() == "vj":
        base = VJ_SOURCES.copy()
        if runtime.gmail_mailbox is not None:
            base.insert(0, runtime.gmail_mailbox.source_name)
    else:
        # Company pages and Harnham still write LJ-maintained source policy. Do
        # not quietly run either for another person until they accept a profile
        # parameter. The public sources and principal-owned LinkedIn seeds are safe.
        base = [
            "research_orgs", "neolabs", "hf_startups", "hn_hiring", "hn_jobs", "linkedin_related",
        ]
        if runtime.gmail_mailbox is not None:
            base.insert(5, runtime.gmail_mailbox.source_name)
        if profile_name.lower() == "kj":
            base.extend(KJ_EXTRA_SOURCES)

    delegated = consumed_sources(profile_name)
    # The principal's own market-players tracker (`sources/market_players.py`):
    # uniform for every principal, LJ included — he simply has no
    # `profiles/example/market_tracker.txt`, so today this is a no-op for him (his
    # speech list runs as its own source above).
    if "market_players" not in base and market_players_available(profile_name):
        base.append("market_players")
    return [name for name in base if name not in delegated]


def describe_sources(profile_name: str, runtime: PrincipalRuntime) -> dict:
    """Summarise this principal's source position for logs and run records.

    Returns three disjoint lists: what the principal crawls itself, what another
    principal crawls for it (`consumed`, source -> owner), and what exists in the
    engine, is usable in principle, and this principal does not use at all.
    Another principal's mailbox source is neither available nor missing — it is
    simply not this principal's — so it is excluded rather than listed.
    """
    crawling = sources_for_profile(profile_name, runtime)
    consumed = consumed_sources(profile_name)
    foreign_mailboxes = {
        name
        for name in ALL_SOURCES
        if name.startswith("gmail_") and not name.startswith(f"gmail_{profile_name.lower()}_")
    }
    unused = [
        name
        for name in ALL_SOURCES
        if name not in crawling and name not in consumed and name not in foreign_mailboxes
    ]
    return {"crawling": crawling, "consumed": consumed, "unused": unused}


def run_source(
    source_name: str,
    crawler: SmartCrawler,
    matcher: JobMatcher,
    registry: JobRegistry,
    runtime: PrincipalRuntime,
    dry_run: bool = False,
    replay_archive: bool = False,
) -> List[WikiEntry]:
    """Run a single data source, returning its wiki entries.

    `replay_archive` (Gmail sources only) re-ingests the emails already saved
    under `state/<principal>/sources/gmail/` instead of contacting the mailbox —
    see `GmailJobsSource.run_from_archive`.
    """
    if source_name == "research_orgs":
        return ResearchOrgsSource(crawler, matcher, registry).run()
    if source_name == "neolabs":
        return NeolabsSource(crawler, matcher, registry).run()
    if source_name == "neoclouds":
        return NeocloudsSource(crawler, matcher, registry).run()
    if source_name == "speech_companies":
        return SpeechCompaniesSource(crawler, matcher, registry).run()
    if source_name == "market_players":
        # The principal's own tracker list; the list and the goal both come from
        # the profile (`sources_for_profile` only offers it when they exist).
        return MarketPlayersSource(
            crawler, matcher, registry, profile_name=runtime.profile_name
        ).run()
    if source_name == "cheminfo_companies":
        return CheminfoCompaniesSource(
            crawler, matcher, registry, profile_name=runtime.profile_name
        ).run()
    if source_name == "compbiojobs":
        # Board source: fetches listing pages + each job's JSON-LD; no crawler.
        return CompBioJobsSource(crawler, matcher, registry).run()
    if source_name == "hf_startups":
        return HFStartupsSource(crawler, matcher, registry).run()
    if source_name == "hn_hiring":
        return HNHiringSource(crawler, matcher, registry).run()
    if source_name == "hn_jobs":
        return HNHiringJobsSource(crawler, matcher, registry).run()
    if source_name == "gmail_lj_jobs":
        # Gmail source needs no crawler (jobs come from email); passed for uniformity.
        mailbox = runtime.gmail_mailbox
        source = GmailLjJobsSource(
            crawler,
            matcher,
            registry,
            mark_read=not dry_run,
            label=mailbox.label if mailbox is not None else None,
            credential_home=mailbox.credential_home if mailbox is not None else None,
            sources_dir=runtime.sources_dir,
            capture_seeds=not dry_run,
        )
        return source.run_from_archive() if replay_archive else source.run()
    if runtime.gmail_mailbox is not None and source_name == runtime.gmail_mailbox.source_name:
        mailbox = runtime.gmail_mailbox
        source = GmailJobsSource(
            crawler,
            matcher,
            registry,
            mark_read=not dry_run,
            label=mailbox.label,
            credential_home=mailbox.credential_home,
            source_name=mailbox.source_name,
            sources_dir=runtime.sources_dir,
            capture_seeds=not dry_run,
        )
        return source.run_from_archive() if replay_archive else source.run()
    if source_name == "linkedin_related":
        return LinkedInRelatedSource(crawler, matcher, registry, sources_dir=runtime.sources_dir).run()
    if source_name == "company_pages":
        return CompanyPagesSource(
            crawler, matcher, registry, profile_name=runtime.profile_name
        ).run()
    if source_name == "harnham":
        return HarnhamSource(crawler, matcher, registry).run()
    if source_name == "digitaljobs":
        # MCP API source; needs no crawler (jobs come from JSON-RPC), kept
        # for interface uniformity.
        return DigitalJobsSource(
            crawler, matcher, registry, profile_name=runtime.profile_name
        ).run()
    if source_name == "jackandjill":
        # XML feed source; needs no crawler (the feed carries full adverts).
        return JackAndJillSource(
            crawler, matcher, registry, profile_name=runtime.profile_name,
            cache_dir=runtime.cache_dir,
        ).run()
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
    replay_archive: bool = False,
    registry: Optional[JobRegistry] = None,
    llm: Optional[LLMClient] = None,
    crawler: Optional[SmartCrawler] = None,
    matcher: Optional[JobMatcher] = None,
    wiki: Optional[WikiStore] = None,
    runtime: Optional[PrincipalRuntime] = None,
    run_id: Optional[str] = None,
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
        replay_archive: re-ingest the principal's locally archived Gmail
            emails (Gmail sources only) instead of contacting the mailbox. The
            registry, matcher, org pages and report are updated as usual; the
            run ledger records `mode=replay`.
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

    # The principal this run is for: every `LLMClient()` built below follows
    # `profiles/<p>/llm.yaml` (2026-10-21, docs/49).
    config.set_active_profile(profile_name)
    ensure_fetcher_available()
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
    # Model-call transcripts follow the run's dataset dir, so a preview writes
    # them into its temp root instead of live state (the one ledger that must
    # stay silent in a dry run).
    import model_transcripts as _mt

    # Every parameter is forwarded: the body was split out of this function, and
    # a body that reads a parameter it was not given fails as an UnboundLocalError
    # at the first use — which is exactly how this was caught.
    with _mt.active_dataset_dir(runtime.dataset_dir):
        return _run_pipeline_body(
            profile_name, role, source, use_dspy=use_dspy, dry_run=dry_run,
            write_report=write_report, snapshot=snapshot,
            replay_archive=replay_archive, registry=registry, llm=llm,
            crawler=crawler, matcher=matcher, wiki=wiki, runtime=runtime,
            run_id=run_id, preview_dir=preview_dir, profile_text=profile_text)


def _run_pipeline_body(
    profile_name: str,
    role: Optional[str],
    source: str,
    *,
    use_dspy: bool = False,
    dry_run: bool = False,
    write_report: bool = True,
    snapshot: bool = True,
    replay_archive: bool = False,
    registry: Optional[JobRegistry] = None,
    llm: Optional[LLMClient] = None,
    crawler: Optional[SmartCrawler] = None,
    matcher: Optional[JobMatcher] = None,
    wiki: Optional[WikiStore] = None,
    runtime: Optional[PrincipalRuntime] = None,
    run_id: Optional[str] = None,
    preview_dir=None,
    profile_text: str = "",
):
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
                principal=profile_name,
            )
        else:
            matcher = JobMatcher(
                llm, profile_text, registry=registry, use_history=True,
                history_kwargs=_history_kwargs(runtime),
                principal=profile_name,
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

            # An outer operation (crawl_operation.py) may pass its own
            # identity so the ledger row, stratum manifest and index share
            # ONE run id instead of two families joined by timestamps.
            run_id = run_id or new_run_id()
            append_run(
                runtime.dataset_dir,
                run_id=run_id,
                profile_name=profile_name,
                role=role or "default",
                sources=sources,
                profile_text=profile_text,
                model=getattr(llm, "model", ""),
                provider=getattr(config, "LLM_PROVIDER", ""),
                mode="replay" if replay_archive else "live",
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

    # Crawl context: which principal, which lead sources, which agent. The
    # notification email reads the JSON side (2026-09-10 feedback: the KJ
    # email never said whose crawl it was or where the leads came from).
    gmail_label = runtime.gmail_mailbox.label if runtime.gmail_mailbox else ""
    logger.info(
        "Crawl context: principal=%s role=%s sources=%s gmail_label=%s",
        profile_name, role or "default", ",".join(sources), gmail_label or "-",
    )
    # Revision of the code producing this result. Every host runs its own
    # checkout and the git dirs drift: on 2026-09-19 a host-a sweep ran a tree
    # without the WP1b wiring host-b already had, and nothing in the log said
    # so. Best-effort — revision evidence must never fail a crawl.
    git_info: dict = {}
    try:
        git_info = describe_git()
        logger.info("Git context: %s", render_git_line(git_info))
    except Exception as e:  # pragma: no cover - the helper is already best-effort
        logger.warning(f"Git context unavailable: {e}")
    # Per-principal source policy, stated explicitly in the log: what this
    # principal crawls, what another principal crawls for it, and what exists
    # but is not used (2026-09-12 feedback).
    policy = describe_sources(profile_name, runtime)
    logger.info(
        "Source policy: crawling=[%s] consumed=[%s] available-unused=[%s]",
        ",".join(policy["crawling"]) or "-",
        ",".join(f"{name}<-{owner}" for name, owner in sorted(policy["consumed"].items())) or "-",
        ",".join(policy["unused"]) or "-",
    )
    if not dry_run:
        try:
            import json as _json
            runtime.cache_dir.mkdir(parents=True, exist_ok=True)
            (runtime.cache_dir / "last_run_context.json").write_text(
                _json.dumps({
                    "principal": profile_name,
                    "role": role or "default",
                    "sources": list(sources),
                    "gmail_label": gmail_label,
                    "consumed": dict(sorted(policy["consumed"].items())),
                    "available_unused": policy["unused"],
                    "git": git_info,
                }, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"Crawl context write failed: {e}")

    for src in sources:
        logger.info(f"Running source: {src}")
        try:
            # A preview must not advance registry lifecycle state or consume
            # principal Gmail. Sources see no writable registry and Gmail gets
            # mark_read=False/capture_seeds=False above.
            source_registry = None if dry_run else registry
            entries = run_source(
                src, crawler, matcher, source_registry, runtime, dry_run,
                replay_archive=replay_archive,
            )
            all_entries.extend(entries)
            logger.info(f"Source {src} produced {len(entries)} entries")
        except Exception as e:
            logger.error(f"Source {src} failed: {e}")

    # Attach the advert's observed age from the registry, in ONE place rather
    # than in every source: without it the report cannot show how long an advert
    # has been open, which is how stale GO links reached PK unnoticed
    # (2026-09-20). `first_seen` is the entry's date only when we first observed
    # the advert — a lower bound on its real age. See agents/skills/stale-adverts.
    if registry is not None and all_entries:
        attached = 0
        for entry in all_entries:
            if entry.first_seen:
                continue
            try:
                record = registry.get(entry.org_name, entry.title, entry.url or "")
            except Exception:
                record = None
            if record is not None:
                entry.first_seen = record.first_seen
                # The advert's own posting date, when the source gave one: the
                # true age signal for the report and the matcher (§WP3a).
                if not getattr(entry, "posted_at", ""):
                    entry.posted_at = getattr(record, "posted_at", "")
                # The report's Lifecycle line and the Long-Lasting grouping read
                # the entry, not the registry: keep them consistent with the age
                # we just attached (advisory, 2026-09-20).
                if not entry.lifecycle_status:
                    entry.lifecycle_status = record.status
                attached += 1
        logger.info(f"Attached registry age to {attached} entries")

    # Run-level scoring policy: ceilings the profile declares (an early-career
    # principal is not hired into senior roles; countries whose working language
    # he does not speak are not ranked high). They need the run's entries — the
    # source's own `seniority` metadata and each entry's location — and they act
    # before anything is written, so the report, wiki pages and review packs all
    # see the same verdicts (LJ, 2026-09-22).
    if all_entries:
        try:
            from scoring_policy import apply_run_policy

            apply_run_policy(all_entries, profile_text)
        except Exception as e:  # noqa: BLE001 - a policy pass must not kill a run
            logger.warning(f"run policy pass failed: {e}")

    # The Digital Jobs MCP advertises itself as agent-first. Record how that
    # claim held up on this run, while the source's statistics are fresh
    # (2026-09-12). A report, deliberately not an email.
    if not dry_run and "digitaljobs" in sources:
        try:
            from digitaljobs_report import write_report
            from sources.digitaljobs import stats_snapshot

            snapshot = stats_snapshot()
            if snapshot.get("requests"):
                friendliness = write_report(
                    runtime, snapshot, principal=profile_name,
                    role=role or "general", run_id=run_id,
                )
                logger.info(f"Digital Jobs agent-friendliness report: {friendliness}")
        except Exception as e:
            logger.warning(f"Digital Jobs friendliness report failed: {e}")

    # The weekly letter to Jack & Jill: what their featured-role feed offered
    # in the last week or two, never repeating a position already reported.
    # Full sweeps only (the weekly cadence is enforced inside the letter), and
    # never on a preview — it writes into the principal's letters directory.
    #
    # A sweep is the *fallback*, not the schedule: the Monday cron job writes
    # the letter, and LJ's sweep runs on Saturday, 5 days later. With the plain
    # 7-day gate that Saturday would be due again and every letter would drift
    # onto Saturdays. The hook therefore waits longer, so it only fires when the
    # Monday job has missed (the letter is then still weekly, if late).
    if not dry_run and source == "all" and "jackandjill" in sources:
        try:
            from jnj_letter import write_letter

            letter = write_letter(
                runtime, principal=profile_name, role=role or "general", run_id=run_id,
                min_interval_days=HOOK_MIN_INTERVAL_DAYS,
            )
            if letter.written:
                logger.info(
                    f"Jack & Jill letter: {letter.path} "
                    f"({letter.positions} named, {letter.covered} covered, "
                    f"{letter.skipped_already_lettered} already lettered)"
                )
            else:
                logger.info(f"Jack & Jill letter skipped: {letter.reason}")
        except Exception as e:
            logger.warning(f"Jack & Jill letter failed: {e}")

    # Per-source post-crawl report: yield, lifecycle, verdict mix, and the
    # mailbox's per-site breakdown (2026-09-13). No LLM calls — it reads the
    # registry, the assessment ledger and the email archive. A report, not an
    # email, and never on a preview.
    if not dry_run:
        try:
            from source_report import write_report as write_source_report

            sources_report = write_source_report(
                runtime, principal=profile_name, role=role or "general", run_id=run_id
            )
            logger.info(f"Source report: {sources_report}")
        except Exception as e:
            logger.warning(f"Source report failed: {e}")

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
        # Resolve actionable LinkedIn leads back to their original advert and
        # harvest the company into the periodic-check list, before the report
        # and ledger are written (2026-09-12).
        try:
            from lead_resolution import enabled, resolve_actionable

            if enabled():
                resolution = resolve_actionable(
                    all_entries, profile_name, cache_dir=runtime.cache_dir
                )
                if resolution["considered"]:
                    logger.info(
                        "Original-advert resolution: considered=%d resolved=%d cached=%d "
                        "harvested=%d%s",
                        resolution["considered"],
                        resolution["resolved"],
                        resolution["cached"],
                        resolution["harvested"],
                        f" budget_exhausted={resolution['budget_exhausted']}"
                        if resolution["budget_exhausted"] else "",
                    )
        except Exception as e:
            logger.warning(f"Original-advert resolution failed: {e}")

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
                principal=profile_name,
            )
        except Exception as e:
            logger.warning(f"Dataset append failed: {e}")

        # Outcome -> assessment join and the join-rate metric (business plan
        # step 0, docs/29-business-model.md §6). Read-only over both ledgers
        # and additive: it appends outcome_joins.jsonl and one join_metrics
        # row. Failures are logged, never fatal - the crawl's real output is
        # already durable at this point.
        try:
            from outcome_link import join_dataset

            join_dataset(
                runtime.dataset_dir,
                run_id=run_id,
                profile_name=profile_name,
                applications_dir=runtime.applications_dir,
                write=True,
            )
        except Exception as e:
            logger.warning(f"Outcome join failed: {e}")

        # Packs are per-principal: each crawl prepares its own principal's GO dossiers.
        # (LJ-only until 2026-10-02, which is why VJ's and KJ's crawls produced none.)
        # prepare_crawl_packs skips a principal with no state dir, so a not-yet-onboarded
        # principal can never have its cases materialised in the unified — LJ's — root.
        from application_packs import prepare_crawl_packs
        from wiki_store import dedupe_entries

        all_entries, junk = drop_junk_entries(all_entries)
        if junk:
            logger.warning(
                "Dropped %d finding(s) with a non-finding identity (junk "
                "title/company/location) before the wiki, report and packs: %s",
                len(junk), "; ".join(
                    f"{e.org_name!r}/{e.title!r}/{e.location!r}"[:120]
                    for e in junk[:5]))

        # One advert, one row on every presentation surface. The ledger above
        # keeps every assessment (it is the record); the packs, the wiki pages,
        # the report and the index show the advert once, with the most useful
        # verdict — a source can emit the same advert many times in one run
        # (2026-10-07 audit: 1,888 redundant lj assessments, 3,431 redundant
        # org-page blocks).
        all_entries, collapsed = dedupe_entries(all_entries)
        if collapsed:
            logger.info(
                "Collapsed %d duplicate advert row(s) across %d unique finding(s)",
                collapsed, len(all_entries))

        pack_results = prepare_crawl_packs(all_entries, profile_name)
        for result in pack_results:
            result.entry.case_pack_result = result
        try:
            wiki.update_org_pages(all_entries)
        except Exception as e:
            logger.warning(f"Wiki update failed: {e}")
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


def drop_junk_entries(entries: List[WikiEntry]) -> tuple[List[WikiEntry], List[WikiEntry]]:
    """Split findings into (real, junk-identity) using the identity gates.

    One place, every source. The gates existed from the start but only the
    Gmail parser called them, so `hn_hiring`, `digitaljobs`, `company_pages`,
    `market_players` and `linkedin_related` could all hand the presentation a
    row whose title is an employment type, whose company is a salary band, or
    whose location is a posting age — 560 such titles on lj's pages at the
    2026-10-07 audit, still being produced that day. Dropping here is *not*
    silent: the ledger keeps the row (it is the record of what the parser
    produced) and the drop is logged with examples, so a source that starts
    emitting them is visible instead of laundered into the wiki.
    """
    from lead_identity import is_noise_company, is_noise_location, is_noise_title

    real: List[WikiEntry] = []
    junk: List[WikiEntry] = []
    for entry in entries:
        if (is_noise_title(entry.title)
                or is_noise_company(entry.org_name)
                or is_noise_location(entry.location)):
            junk.append(entry)
        else:
            real.append(entry)
    return real, junk


def _history_kwargs(runtime: PrincipalRuntime) -> dict:
    """Pass one principal's prior-contact scope into the matcher."""
    kwargs = {
        "applications_dir": runtime.applications_dir,
        # The outcomes ledger is the cooldowns' second prior-application
        # source: LoveWork-cut cases are not mirrored into the applications
        # view (MIRROR_PACKS off), so only the ledger sees them (PostHog,
        # 2026-10-17).
        "dataset_dir": runtime.dataset_dir,
    }
    if runtime.gmail_mailbox is not None:
        kwargs["gmail_label"] = runtime.gmail_mailbox.label
        kwargs["gmail_credential_home"] = runtime.gmail_mailbox.credential_home
    return kwargs
