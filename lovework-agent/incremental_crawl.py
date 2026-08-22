#!/usr/bin/env python3
"""
Run an incremental LoveWork crawl on a cost-bounded subset of sources and
emit a human-readable summary. This is the "at least once" run LJ asked
for — not the full cron sweep.

It runs principal-appropriate sources. LJ retains the NeoLabs AI-lab tracker;
VJ intentionally skips it because VJ is searching statistics/pricing/actuarial
and sports analytics rather than AI-lab work. Both principals use their own
Gmail alert mailbox where configured, HN hiring, and HN jobs.

Each source runs the FULL pipeline (registry upsert + matcher + wiki).
After the crawl, a cross-check is performed: for every GO entry produced
this run, look up prior contact in applications/ + Gmail and append a
"prior contact" block to the corresponding wiki/orgs/ page (no
modification to existing entries — append-only).

The summary is written to wiki/reports/YYYY-MM-DD-lj-incremental.md and
echoed to stdout.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("incremental")

import config
from principal_runtime import resolve_principal_runtime
from history import scan_history
from job_registry import JobRegistry
from pipeline import run_pipeline
from report_header import build_header
from sources.neolabs import NeolabsSource
from wiki_store import WikiEntry, WikiStore


# Module-level cache of the parsed tracker (populated once for the run).
_TRACKER_CACHE: list[dict] = []


def _render_entry_block(e: WikiEntry) -> list[str]:
    """Render one scored entry for the incremental report.

    P1 — provenance is mandatory, absence is visible: the entry shows the
    URL, the discovery (Found-via) line, or an explicit ``_not available_``
    — it is never silently omitted (defect class: URL-less entries).
    """
    lines = [f"### {e.org_name} — {e.title}\n"]
    lines.append(f"- **Score**: {e.score:.1f}/10 ({e.decision})")
    if e.url:
        lines.append(f"- **URL**: {e.url}")
    if e.discovery_url:
        date_suffix = f" ({e.discovery_date})" if e.discovery_date else ""
        lines.append(f"- **Found via**: [{e.source}]({e.discovery_url}){date_suffix}")
    if not e.url and not e.discovery_url:
        lines.append("- **URL**: _not available_")
    if e.location:
        lines.append(f"- **Location**: {e.location}")
    lines.append(f"- **Reasoning**: {e.reasoning}")
    if e.lifecycle_status:
        lines.append(f"- **Lifecycle**: {e.lifecycle_status}")
    lines.append("")
    return lines


def _normalise_name(name: str) -> str:
    """Normalise an org name for fuzzy matching against wiki page filenames.

    Strips parentheticals, lower-cases, removes punctuation, collapses
    spaces. "Advanced Machine Intelligence / AMI Labs (Yann LeCun)" and
    "Advanced Machine Intelligence AMI Labs Yann LeCun" should both match
    "advanced machine intelligence ami labs yann lecun".
    """
    import re
    n = name.lower()
    # Remove parentheticals.
    n = re.sub(r"\([^)]*\)", " ", n)
    # Drop punctuation and slashes; keep letters/digits/spaces.
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    # Collapse spaces.
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _names_match(a: str, b: str) -> bool:
    """Loose org-name match: one normalised name is a substring of the other.

    This handles the asymmetry where the org tracker has a shorter form
    ("AMI Labs (Yann LeCun)") and the wiki filename has the full
    long form ("Advanced_Machine_Intelligence___AMI_Labs__Yann_LeCun_").
    """
    a_n, b_n = _normalise_name(a), _normalise_name(b)
    if not a_n or not b_n:
        return False
    return a_n in b_n or b_n in a_n


def main():
    parser = argparse.ArgumentParser(description="Run a principal-scoped incremental LoveWork crawl")
    parser.add_argument("--profile", default="lj", choices=("lj", "vj", "kj", "pk"))
    parser.add_argument("--role", default=None)
    args = parser.parse_args()
    profile_name = args.profile
    default_roles = {"lj": "general", "vj": "data-statistics-pricing"}
    role = args.role or default_roles.get(profile_name, "general")
    runtime = resolve_principal_runtime(profile_name)
    gmail_source = runtime.gmail_mailbox.source_name if runtime.gmail_mailbox else None
    date_str = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"=== Incremental crawl starting for {profile_name}/{role} — {date_str} ===")

    # Snapshot pre-crawl state.
    try:
        from snapshot import snapshot_cache
        snapshot_cache(runtime.cache_dir)
    except Exception as e:
        logger.warning(f"Cache snapshot failed: {e}")

    # Cost caps for LLM-heavy sources.
    os.environ["LOVEWORK_HN_HIRING_MAX_COMMENTS"] = "30"
    os.environ["LOVEWORK_HN_HIRING_MAX_ENTRIES"] = "30"
    os.environ["LOVEWORK_HN_JOBS_MAX_LISTINGS"] = "20"

    # 1) NeoLabs is LJ's AI-lab tracker. It is useful for LJ but intentionally
    # skipped for VJ's statistics/pricing/actuarial search.
    neolab_entries = []
    disappeared = 0
    if profile_name != "vj":
        neolab_entries, disappeared = run_pipeline(
            profile_name=profile_name,
            role=role,
            source="neolabs",
            write_report=False,
            snapshot=False,
        )
        logger.info(f"neolabs: produced {len(neolab_entries)} entries (disappeared: {disappeared})")

    # 2) Principal-approved Gmail alerts. A principal without a mailbox source
    # simply omits this step; it never falls back to LJ's label or token.
    gmail_entries = []
    if gmail_source:
        gmail_entries, _ = run_pipeline(
            profile_name=profile_name, role=role, source=gmail_source,
            write_report=False, snapshot=False,
        )
        logger.info(f"{gmail_source}: produced {len(gmail_entries)} entries")

    # 3) hn_hiring (live)
    hn_entries, _ = run_pipeline(
        profile_name=profile_name, role=role, source="hn_hiring",
        write_report=False, snapshot=False,
    )
    logger.info(f"hn_hiring: produced {len(hn_entries)} entries")

    # 4) hn_jobs (live)
    jobs_entries, _ = run_pipeline(
        profile_name=profile_name, role=role, source="hn_jobs",
        write_report=False, snapshot=False,
    )
    logger.info(f"hn_jobs: produced {len(jobs_entries)} entries")

    # 5) Cross-check every GO entry produced this run against
    #    applications/ + Gmail, and append "prior contact" blocks to
    #    the relevant wiki/orgs/ pages (append-only).
    wiki = WikiStore(root=runtime.wiki_root)
    all_run_entries = neolab_entries + gmail_entries + hn_entries + jobs_entries
    go_entries = [e for e in all_run_entries
                  if e.decision == "GO"]
    logger.info(f"Cross-checking {len(go_entries)} GO entries against history…")

    cross_check_log = []
    seen_orgs = set()
    for e in go_entries:
        key = e.org_name.lower().strip()
        if key in seen_orgs:
            continue
        seen_orgs.add(key)
        try:
            history_kwargs = {
                "applications_dir": runtime.applications_dir,
                "use_gmail": runtime.gmail_mailbox is not None,
            }
            if runtime.gmail_mailbox is not None:
                history_kwargs["gmail_label"] = runtime.gmail_mailbox.label
                history_kwargs["gmail_credential_home"] = runtime.gmail_mailbox.credential_home
            prior = scan_history(e.org_name, **history_kwargs)
        except Exception as ex:
            logger.debug(f"scan_history failed for {e.org_name}: {ex}")
            continue
        if not prior.has_application and not prior.gmail_events:
            continue
        block = (
            f"### {date_str} — prior contact found\n\n"
            f"- **Source entry**: {e.title} ({e.score:.1f}/10) — {e.url or 'no url'}\n"
            f"- **Prior contact**: {prior.summary()}\n"
        )
        # Append to the org's wiki page (wiki_store.update_org_page is
        # append-only by design).
        wiki.update_org_page(WikiEntry(
            org_name=e.org_name, title="(prior-contact cross-check)",
            url=None, location=None, score=0.0, decision="",
            reasoning=block, source="cross_check",
        ))
        cross_check_log.append({
            "org": e.org_name,
            "title": e.title,
            "has_application": prior.has_application,
            "has_rejection": prior.has_rejection,
            "summary": prior.summary(),
        })

    logger.info(f"Cross-check found prior contact for {len(cross_check_log)} org(s)")

    # 6) Write the incremental report.
    time_suffix = datetime.now().strftime("%H%M%S")
    report_path = runtime.wiki_root / "reports" / f"{date_str}-{time_suffix}-{profile_name}-incremental.md"
    sources_run = ["hn_hiring", "hn_jobs"]
    if profile_name != "vj":
        sources_run.insert(0, "neolabs")
    if gmail_source:
        sources_run.insert(1, gmail_source)
    report_lines = build_header(
        run_type="INCREMENTAL",
        profile_label=f"{profile_name.upper()} / {role}",
        sources=sources_run,
    )
    report_lines += [
        f"**Sources run:** {', '.join(sources_run)}",
        "",
        "## Summary",
        "",
        f"- neolabs:  {len(neolab_entries)} entries",
        f"- gmail:    {len(gmail_entries)} entries",
        f"- hn_hiring: {len(hn_entries)} entries",
        f"- hn_jobs:  {len(jobs_entries)} entries",
        f"- **Total entries**: {len(all_run_entries)}",
        f"- **Disappeared this run**: {disappeared}",
        "",
    ]
    report_sections = []
    if profile_name != "vj":
        report_sections.append(("neolabs", neolab_entries))
    report_sections.extend(((gmail_source or "gmail", gmail_entries),
                            ("hn_hiring", hn_entries),
                            ("hn_jobs", jobs_entries)))
    for label, entries in report_sections:
        if not entries:
            continue
        report_lines.append(f"## {label}\n")
        for e in sorted(entries, key=lambda x: x.score, reverse=True):
            if e.decision not in ("GO", "MAYBE"):
                continue
            report_lines.extend(_render_entry_block(e))

    from application_packs import render_pack_report_section

    pack_results = [
        getattr(entry, "case_pack_result")
        for entry in all_run_entries
        if getattr(entry, "case_pack_result", None) is not None
    ]
    report_lines += render_pack_report_section(pack_results)

    if cross_check_log:
        report_lines.append("## Cross-check: prior contact found\n")
        for x in cross_check_log:
            report_lines.append(f"### {x['org']} ({x['title']})")
            report_lines.append(f"- {x['summary']}")
            report_lines.append("")

    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    logger.info(f"Report written to {report_path}")

    # 7) Print a one-page summary to stdout.
    print("=" * 70)
    print(f"LoveWork — {profile_name.upper()} Incremental Crawl — {date_str}")
    print("=" * 70)
    print(f"Sources: {', '.join(sources_run)}")
    print(f"Total entries:    {len(all_run_entries)}")
    if profile_name != "vj":
        print(f"  neolabs:        {len(neolab_entries)}")
    print(f"  gmail:          {len(gmail_entries)}")
    print(f"  hn_hiring:      {len(hn_entries)}")
    print(f"  hn_jobs:        {len(jobs_entries)}")
    print(f"Disappeared:      {disappeared}")
    print(f"Cross-checks:     {len(cross_check_log)}")
    if cross_check_log:
        print()
        print("Prior-contact cross-checks:")
        for x in cross_check_log:
            print(f"  - {x['org']}: {x['summary']}")
    print()
    print(f"Report: {report_path}")


if __name__ == "__main__":
    main()
