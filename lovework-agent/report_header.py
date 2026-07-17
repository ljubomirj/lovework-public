"""
Shared report header for incremental and full-sweep runs.

Both runs write their own dated markdown file. This module provides
a single header builder so the format stays consistent:

- Full datetime in the title (e.g. "2026-06-29 14:32:15 BST")
- Run type marker (INCREMENTAL / FULL SWEEP)
- Schedule gleaned from Hermes cron jobs.json at runtime
- Brief explainer of what runs where

Read the schedule from the active Hermes profile so it stays in sync
with whatever the cron is currently set to.
"""
from datetime import datetime
from pathlib import Path
from typing import Optional
import json
import os
from hermes_context import resolve_hermes_home, profile_name


# Brief explainer of which sources run in which cadence. Stable
# canonical text -- the rest of the header is dynamic.
EXPLAINER = """\
## What Runs Where

**Incremental** runs the active lead-generation sources -- the things
that surface new roles in near real-time:

- neolabs (all 61 orgs, every run)
- Gmail LJ-jobs (LinkedIn + Totaljobs + CWJobs job alert emails)
- HN hiring thread (live, capped to 30 comments for cost)
- HN /jobs page (21-day recency filter)

**Full sweep** runs everything the incremental does, plus the slower-
changing background sources that don't need to be checked daily:

- research_orgs (19 fixed labs, e.g. MATS, Anthropic, DeepMind, FAR.AI)
- HF startups (Alex Izydorczyk's AI-for-HF-startup-tracker)
- LinkedIn related (follows seed URLs harvested from past Gmail alerts)
- Company pages (LJ's curated keep-list, per-entry re-crawl cadence)

HN hiring and HN /jobs run with larger caps on Sundays (all ~250+ comments).
"""


def get_cron_schedule() -> str:
    """Glean the schedule from the active Hermes profile's cron jobs.

    Reads $HERMES_HOME/cron/jobs.json (set by Hermes when cron jobs are
    created). Returns a one-line summary of each scheduled job, or a
    fallback message if the file can't be read.
    """
    hermes_home = resolve_hermes_home()
    jobs_path = hermes_home / "cron" / "jobs.json"
    if not jobs_path.exists():
        # Try parent (profile fallthrough) — hermel profile lives under
        # ~/.hermes-gigul2/profiles/hermel/cron/ when default is .hermes-gigul2
        if hermes_home.parent.exists():
            jobs_path = hermes_home.parent / "cron" / "jobs.json"
        if not jobs_path.exists():
            return f"(schedule unavailable — no jobs.json for Hermes profile {profile_name(hermes_home)})"

    try:
        with open(jobs_path) as f:
            data = json.load(f)
    except Exception as e:
        return f"(schedule unavailable — could not parse jobs.json: {e})"

    jobs = data.get("jobs", [])
    if not jobs:
        return "(no scheduled jobs)"

    parts = []
    for j in jobs:
        name = j.get("name", "?")
        sched = j.get("schedule", "?")
        next_run = j.get("next_run_at", "")
        next_str = f" (next: {next_run})" if next_run else ""
        parts.append(f"`{name}`: `{sched}`{next_str}")

    return "\n".join(parts)


def build_header(
    run_type: str,
    profile_label: str = "LJ",
    sources: Optional[list[str]] = None,
) -> list[str]:
    """Build the report header lines.

    Args:
        run_type: "INCREMENTAL" or "FULL SWEEP"
        profile_label: e.g. "LJ-general"
        sources: list of source names actually run this time (e.g. ["neolabs", "gmail_lj_jobs"])

    Returns a list of lines (without trailing newlines) ready to be
    joined with "\\n" in the report writer.
    """
    now = datetime.now().astimezone()  # local time with tz
    iso = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    date = now.strftime("%Y-%m-%d")

    lines = [
        f"# {run_type} — {iso}",
        "",
        f"Run type: **{run_type}**",
        f"Profile: {profile_label}",
        f"Hermes profile: {profile_name(resolve_hermes_home())}",
    ]
    if sources:
        lines.append(f"Sources this run: {', '.join(sources)}")

    lines.append("")
    lines.append("## Schedule")
    lines.append("")
    lines.append(get_cron_schedule())
    lines.append("")
    lines.append(EXPLAINER)
    lines.append("")
    lines.append("---")
    lines.append("")

    return lines
