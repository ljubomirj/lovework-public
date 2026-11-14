#!/usr/bin/env python3
"""
LoveWork — Personal job discovery agent for LJ (and VJ).

Usage:
    python main.py [--profile lj|vj|kj|pk] [--role ROLE] [--source all|...] [--report]

Thin CLI wrapper around pipeline.run_pipeline(). The pipeline itself lives in
pipeline.py so it can also be driven by agent.run_autonomous() and, later, a
FastAPI service (Phase 3, lovework.be) without subprocess-ing this CLI.

Environment:
    DEEPSEEK_API_KEY   required
    FIRECRAWL_API_KEY  recommended
"""

import argparse
import json
import logging
import os
import sys
from contextlib import nullcontext

import config
from principal_runtime import resolve_principal_runtime
from job_registry import JobRegistry
from pipeline import run_pipeline
from wiki_store import WikiStore
from notify import human_slug
from writer_boundary import WriterBusy, standalone_writer_guard

DASHBOARD_BASE_URL = os.getenv("LOVEWORK_DASHBOARD_URL", "http://192.0.2.10:8765")

# How many MAYBE listings the results block prints (the GO section is never
# capped). The email body is this block, so 0 lists every MAYBE — useful when
# the notification is the reader's whole view of the run.
MAYBE_LIST_MAX = int(os.getenv("LOVEWORK_LIST_MAYBE_MAX", "10"))


def _print_listing(e, runtime) -> None:
    """One lead with every follow-up surface available (2026-09-10 feedback):
    the public advert when the source captured one, the dashboard org page
    (private view), the local file, and the structured fields we have."""
    print(f"  [{e.score:.1f}] {e.org_name} — {e.title}")
    if e.location:
        print(f"        location: {e.location}")
    if getattr(e, "salary", ""):
        print(f"        salary: {e.salary}")
    if getattr(e, "seniority", ""):
        print(f"        seniority: {e.seniority}")
    if getattr(e, "original_url", "") and e.original_url != e.url:
        # Resolved past an intermediary (LinkedIn): show the original advert,
        # with where we found it in parentheses.
        print(f"        ad: {e.original_url}")
        print(f"        via: {human_slug(e.url)}")
    elif e.url:
        print(f"        ad: {e.url}")
    safe = WikiStore._safe_filename(e.org_name)
    print(f"        more: {DASHBOARD_BASE_URL}/principals/{runtime.profile_name}/wiki/orgs/{safe}.md")
    local_page = runtime.wiki_root / "orgs" / f"{safe}.md"
    if local_page.exists():
        print(f"        local: {local_page}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("lovework-agent")


def list_profiles() -> None:
    print("Available profiles and roles:")
    for name in sorted(config.profile_dirs()):
        roles = config.list_roles(name)
        print(f"  {name}/")
        print(f"    soul.md, cv-short.md, bio-long.md")
        for r in roles:
            print(f"    roles/{r}.md")
    print()
    print("Usage: python main.py --profile <name> --role <role> --source all")


def main():
    parser = argparse.ArgumentParser(description="Personal job crawler")
    parser.add_argument("--profile", default="lj", choices=["lj", "vj", "kj", "pk"], help="Principal profile")
    parser.add_argument("--role", default=None, help="Specific role file under profiles/<name>/roles/")
    parser.add_argument("--source", default="all", help="Source to run; available sources depend on --profile")
    parser.add_argument("--report", action="store_true", help="Generate markdown report")
    parser.add_argument("--json", action="store_true", help="Output findings as JSON to stdout")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Non-persistent preview; Gmail stays unread and state is untouched",
    )
    parser.add_argument("--list-profiles", action="store_true", help="List available profiles/roles and exit")
    parser.add_argument("--registry-stats", action="store_true", help="Print job registry stats and exit")
    parser.add_argument("--dspy", action="store_true", help="Use DSPy typed signatures instead of legacy prompts")
    parser.add_argument(
        "--run-id",
        default=None,
        help="Outer operation id (crawl_operation.py): the ledger row shares "
             "this identity instead of minting a second UUID",
    )
    parser.add_argument(
        "--replay-archive",
        action="store_true",
        help="Re-ingest the principal's archived Gmail emails instead of contacting "
             "Gmail (requires --source <gmail source>, e.g. gmail_kj_jobs)",
    )
    args = parser.parse_args()

    if args.replay_archive and args.source == "all":
        parser.error(
            "--replay-archive needs an explicit Gmail source, e.g. "
            "--source gmail_kj_jobs (its archive is state/<principal>/sources/gmail/)"
        )

    if args.list_profiles:
        list_profiles()
        return

    if args.registry_stats:
        runtime = resolve_principal_runtime(args.profile)
        registry = JobRegistry(runtime.cache_dir / "jobs.csv")
        print("Job registry stats:")
        for status, n in registry.stats().items():
            print(f"  {status}: {n}")
        return

    from wiki_store import WikiStore
    from principal_runtime import resolve_principal_runtime

    runtime = resolve_principal_runtime(args.profile)
    wiki = WikiStore(root=runtime.wiki_root)
    # WP1b box ①: a direct invocation takes the principal's writer lease so it
    # cannot race a seal or another crawl; a child of an operation carries the
    # marker and skips this. `--dry-run` uses temporary state and writes nothing,
    # so it must not contend for the real principal's lease.
    if args.dry_run:
        guard = nullcontext()
    else:
        guard = standalone_writer_guard(
            args.profile, mode="direct", state_root=runtime.cache_dir.parent,
            run_id=args.run_id)
    try:
        with guard:
            all_entries, disappeared_count = run_pipeline(
                args.profile,
                role=args.role,
                source=args.source,
                use_dspy=args.dspy,
                dry_run=args.dry_run,
                write_report=args.report or not args.json,
                replay_archive=args.replay_archive,
                wiki=wiki,
                run_id=args.run_id,
            )
    except WriterBusy as busy:
        print(f"[LOCK] writer busy: {busy.resource.key} — another operation holds "
              f"this principal's lease (attempt recorded at {busy.attempt_path})")
        raise SystemExit(75)
    if wiki.last_report_path is not None:
        # Machine-readable marker: the outer operation registers THIS file
        # as its sealed original output — no "newest report by mtime".
        print(f"[REPORT_PATH] {wiki.last_report_path}")

    role_label = args.role or "default"
    profile_label = f"{args.profile.upper()}-{role_label}"

    if args.json:
        data = [
            {
                "org": e.org_name,
                "title": e.title,
                "url": e.url,
                "location": e.location,
                "score": e.score,
                "decision": e.decision,
                "reasoning": e.reasoning,
                "source": e.source,
                "date": e.date,
                "lifecycle_status": e.lifecycle_status,
                "first_seen": e.first_seen,
            }
            for e in all_entries
        ]
        print(json.dumps(data, indent=2))
    else:
        go = [e for e in all_entries if e.decision == "GO"]
        maybe = [e for e in all_entries if e.decision == "MAYBE"]
        new_jobs = [e for e in all_entries if e.lifecycle_status == "new"]
        long_lasting = [e for e in all_entries if e.lifecycle_status == "long_lasting"]
        runtime = resolve_principal_runtime(args.profile)

        def _dedupe(entries):
            """One row per (org, title): the same advert via several sources
            printed five times in the 2026-09-10 KJ email (WikiEntry has no
            __eq__, so `not in` never filtered)."""
            seen, out = set(), []
            for e in sorted(entries, key=lambda x: x.score, reverse=True):
                key = (e.org_name, e.title)
                if key not in seen:
                    seen.add(key)
                    out.append(e)
            return out

        print(f"\n{'='*60}")
        print(f"LoveWork Results — {profile_label}")
        print(f"{'='*60}")
        print(f"GO:     {len(go)}")
        print(f"MAYBE:  {len(maybe)}")
        print(f"FLAG:   {len([e for e in all_entries if e.decision == 'FLAG'])}")
        print(f"DROP:   {len([e for e in all_entries if e.decision == 'DROP'])}")
        print(f"Total:  {len(all_entries)}")
        print(f"\nNew (first time seen):      {len(new_jobs)}")
        print(f"Long-lasting (>30d open):   {len(long_lasting)}")
        print(f"Disappeared this run:       {disappeared_count}")

        if new_jobs:
            print(f"\n★ New GO/MAYBE listings:")
            for e in [j for j in new_jobs if j.decision in ("GO", "MAYBE")]:
                _print_listing(e, runtime)
        go_rest = [e for e in _dedupe(go) if (e.org_name, e.title) not in {(j.org_name, j.title) for j in new_jobs}]
        if go_rest:
            print(f"\n★ GO listings:")
            for e in go_rest:
                _print_listing(e, runtime)
        pack_results = [
            getattr(entry, "case_pack_result") for entry in all_entries
            if getattr(entry, "case_pack_result", None) is not None
        ]
        if pack_results:
            print("\n★ LoveWork application packs:")
            for result in pack_results:
                location = str(result.path) if result.path else "-"
                print(f"  [{result.status}] {result.entry.org_name} — {result.entry.title}")
                print(f"        {location} ({result.reason})")
        maybe_rest = [e for e in _dedupe(maybe) if (e.org_name, e.title) not in {(j.org_name, j.title) for j in new_jobs}]
        if maybe_rest:
            print(f"\n◆ MAYBE listings:")
            for e in maybe_rest[:MAYBE_LIST_MAX or None]:
                _print_listing(e, runtime)
        if long_lasting:
            print(f"\n⚠ Long-lasting (suspicious):")
            for e in sorted(long_lasting, key=lambda x: x.score, reverse=True)[:5]:
                print(f"  [{e.score:.1f}] {e.org_name} — {e.title} (open since {e.first_seen})")
        if args.dry_run:
            print("\nPreview only: no principal wiki or state was written.")
        else:
            print(f"\nWiki: {runtime.wiki_root}")


if __name__ == "__main__":
    main()
