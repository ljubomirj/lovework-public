#!/usr/bin/env python3
"""
Run an incremental LoveWork crawl on a cost-bounded subset of sources and
emit a human-readable summary. This is the "at least once" run LJ asked
for — not the full cron sweep.

It runs three sources:
  1. neolabs  (capped to 5 orgs to keep cost down — picks the alphabetically
               first 5 that aren't already-seen-as-GO in today's wiki report)
  2. hn_hiring (live HN Algolia API for the June 2026 thread)
  3. hn_jobs  (live HN /jobs with a 21-day recency filter)

Each source runs the FULL pipeline (registry upsert + matcher + wiki).
After the crawl, a cross-check is performed: for every GO entry produced
this run, look up prior contact in applications/ + Gmail and append a
"prior contact" block to the corresponding wiki/orgs/ page (no
modification to existing entries — append-only).

The summary is written to wiki/reports/YYYY-MM-DD-lj-incremental.md and
echoed to stdout.
"""

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
from history import scan_history
from job_registry import JobRegistry
from pipeline import run_pipeline
from report_header import build_header
from sources.neolabs import NeolabsSource
from wiki_store import WikiEntry, WikiStore


# Module-level cache of the parsed tracker (populated once for the run).
_TRACKER_CACHE: list[dict] = []


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
    date_str = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"=== Incremental crawl starting — {date_str} ===")

    # Snapshot pre-crawl state.
    try:
        from snapshot import snapshot_cache
        snapshot_cache(config.CACHE_DIR)
    except Exception as e:
        logger.warning(f"Cache snapshot failed: {e}")

    # Cost caps for LLM-heavy sources.
    os.environ["LOVEWORK_HN_HIRING_MAX_COMMENTS"] = "30"
    os.environ["LOVEWORK_HN_HIRING_MAX_ENTRIES"] = "30"
    os.environ["LOVEWORK_HN_JOBS_MAX_LISTINGS"] = "20"

    # 1) Run all neolabs orgs (61 orgs — DeepSeek is cheap, quota is plentiful).
    all_entries, disappeared = run_pipeline(
        profile_name="lj",
        role="general",
        source="neolabs",
        write_report=False,
        snapshot=False,
    )
    logger.info(f"neolabs: produced {len(all_entries)} entries (disappeared: {disappeared})")

    # 2) gmail_lj_jobs — LinkedIn + Totaljobs alerts (including Track 3 contract leads)
    gmail_entries, _ = run_pipeline(
        profile_name="lj", role="general", source="gmail_lj_jobs",
        write_report=False, snapshot=False,
    )
    logger.info(f"gmail_lj_jobs: produced {len(gmail_entries)} entries")

    # 3) hn_hiring (live)
    hn_entries, _ = run_pipeline(
        profile_name="lj", role="general", source="hn_hiring",
        write_report=False, snapshot=False,
    )
    logger.info(f"hn_hiring: produced {len(hn_entries)} entries")

    # 4) hn_jobs (live)
    jobs_entries, _ = run_pipeline(
        profile_name="lj", role="general", source="hn_jobs",
        write_report=False, snapshot=False,
    )
    logger.info(f"hn_jobs: produced {len(jobs_entries)} entries")

    # 5) Cross-check every GO entry produced this run against
    #    applications/ + Gmail, and append "prior contact" blocks to
    #    the relevant wiki/orgs/ pages (append-only).
    wiki = WikiStore()
    go_entries = [e for e in all_entries + gmail_entries + hn_entries + jobs_entries
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
            prior = scan_history(e.org_name, use_gmail=True)
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
    report_path = config.WIKI_ROOT / "reports" / f"{date_str}-{time_suffix}-lj-incremental.md"
    sources_run = ["neolabs", "gmail_lj_jobs", "hn_hiring", "hn_jobs"]
    report_lines = build_header(
        run_type="INCREMENTAL",
        profile_label="LJ / general",
        sources=sources_run,
    )
    report_lines += [
        f"**Sources run:** neolabs (all 61), gmail_lj_jobs, hn_hiring (live), hn_jobs (live)",
        "",
        "## Summary",
        "",
        f"- neolabs:  {len(all_entries)} entries",
        f"- gmail:    {len(gmail_entries)} entries",
        f"- hn_hiring: {len(hn_entries)} entries",
        f"- hn_jobs:  {len(jobs_entries)} entries",
        f"- **Total entries**: {len(all_entries) + len(gmail_entries) + len(hn_entries) + len(jobs_entries)}",
        f"- **Disappeared this run**: {disappeared}",
        "",
    ]
    for label, entries in (("neolabs", all_entries),
                            ("gmail_lj_jobs", gmail_entries),
                            ("hn_hiring", hn_entries),
                            ("hn_jobs", jobs_entries)):
        if not entries:
            continue
        report_lines.append(f"## {label}\n")
        for e in sorted(entries, key=lambda x: x.score, reverse=True):
            if e.decision not in ("GO", "MAYBE"):
                continue
            report_lines.append(f"### {e.org_name} — {e.title}\n")
            report_lines.append(f"- **Score**: {e.score:.1f}/10 ({e.decision})")
            if e.url:
                report_lines.append(f"- **URL**: {e.url}")
            if e.location:
                report_lines.append(f"- **Location**: {e.location}")
            report_lines.append(f"- **Reasoning**: {e.reasoning}")
            if e.lifecycle_status:
                report_lines.append(f"- **Lifecycle**: {e.lifecycle_status}")
            report_lines.append("")

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
    print(f"LoveWork — Incremental Crawl — {date_str}")
    print("=" * 70)
    print(f"Sources: neolabs (all 61) + gmail (LinkedIn+Totaljobs) + hn_hiring + hn_jobs")
    print(f"Total entries:    {len(all_entries) + len(gmail_entries) + len(hn_entries) + len(jobs_entries)}")
    print(f"  neolabs:        {len(all_entries)}")
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
