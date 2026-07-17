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
import sys

import config
from job_registry import JobRegistry
from pipeline import run_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("lovework-agent")


def list_profiles() -> None:
    print("Available profiles and roles:")
    for p in sorted(config.PROFILES_DIR.iterdir()):
        if p.is_dir():
            roles = config.list_roles(p.name)
            print(f"  {p.name}/")
            print(f"    soul.md, cv-short.md, bio-long.md")
            for r in roles:
                print(f"    roles/{r}.md")
    print()
    print("Usage: python main.py --profile <name> --role <role> --source all")


def main():
    parser = argparse.ArgumentParser(description="Personal job crawler")
    parser.add_argument("--profile", default="lj", choices=["lj", "vj", "kj", "pk"], help="Candidate profile")
    parser.add_argument("--role", default=None, help="Specific role file under profiles/<name>/roles/")
    parser.add_argument("--source", default="all", help="Source to run (all, research_orgs, neolabs, hf_startups, hn_hiring, hn_jobs, gmail_lj_jobs, linkedin_related, company_pages, harnham)")
    parser.add_argument("--report", action="store_true", help="Generate markdown report")
    parser.add_argument("--json", action="store_true", help="Output findings as JSON to stdout")
    parser.add_argument("--dry-run", action="store_true", help="Skip wiki writes")
    parser.add_argument("--list-profiles", action="store_true", help="List available profiles/roles and exit")
    parser.add_argument("--registry-stats", action="store_true", help="Print job registry stats and exit")
    parser.add_argument("--dspy", action="store_true", help="Use DSPy typed signatures instead of legacy prompts")
    args = parser.parse_args()

    if args.list_profiles:
        list_profiles()
        return

    if args.registry_stats:
        registry = JobRegistry()
        print("Job registry stats:")
        for status, n in registry.stats().items():
            print(f"  {status}: {n}")
        return

    all_entries, disappeared_count = run_pipeline(
        args.profile,
        role=args.role,
        source=args.source,
        use_dspy=args.dspy,
        dry_run=args.dry_run,
        write_report=args.report or not args.json,
    )

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
                print(f"  [{e.score:.1f}] {e.org_name} — {e.title}")
                if e.url:
                    print(f"        {e.url}")
        if go:
            print(f"\n★ GO listings:")
            for e in sorted(go, key=lambda x: x.score, reverse=True):
                if e not in new_jobs:  # Already shown above
                    print(f"  [{e.score:.1f}] {e.org_name} — {e.title}")
                    if e.url:
                        print(f"        {e.url}")
        if maybe:
            print(f"\n◆ MAYBE listings:")
            for e in sorted(maybe, key=lambda x: x.score, reverse=True)[:10]:
                if e not in new_jobs:
                    print(f"  [{e.score:.1f}] {e.org_name} — {e.title}")
        if long_lasting:
            print(f"\n⚠ Long-lasting (suspicious):")
            for e in sorted(long_lasting, key=lambda x: x.score, reverse=True)[:5]:
                print(f"  [{e.score:.1f}] {e.org_name} — {e.title} (open since {e.first_seen})")
        print(f"\nWiki: {config.WIKI_ROOT}")


if __name__ == "__main__":
    main()
