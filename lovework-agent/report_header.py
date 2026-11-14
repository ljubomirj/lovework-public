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
import re
from hermes_context import hermes_home_or_none, runtime_identity


# Brief explainer of which sources run in which cadence. Stable
# canonical text -- the rest of the header is dynamic.
EXPLAINER = """\
## What Runs Where

**Incremental** runs the active lead-generation sources -- the things
that surface new roles in near real-time:

- neolabs (all 62 orgs, every run)
- Gmail <principal>-jobs alert label (LinkedIn, Totaljobs, CWJobs, JobServe,
  Lensa, TalentSource, Rec-London, JohnsonJobs, CV-Library) — plus the Jack &
  Jill drop, which arrives in its own archive rather than that label
- HN hiring thread (live, capped to 30 comments for cost)
- HN /jobs page (21-day recency filter)

**Full sweep** runs everything the incremental does, plus the slower-
changing background sources that don't need to be checked daily:

- research_orgs (19 fixed labs, e.g. MATS, Anthropic, DeepMind, FAR.AI)
- HF startups (Alex Izydorczyk's AI-for-HF-startup-tracker)
- LinkedIn related (follows seed URLs harvested from past Gmail alerts)
- Company pages (LJ's curated keep-list, per-entry re-crawl cadence)
- Neoclouds (curated compute list: CoreWeave, Nebius, Lambda, Crusoe, IREN, nscale, Runpod, Vast.ai, Akash, Prime Intellect, Together, Groq, Cerebras, Gimlet)
- Speech companies (curated speech/voice-AI list: TTS/STT/S2S providers, voice-agent platforms, open-source speech labs, arenas)
- Harnham (recruiter search URLs)
- Digital Jobs (anonymous MCP job-board API)

HN hiring and HN /jobs run with larger caps on Sundays (all ~250+ comments).
"""


# ── the report's section headings: one writer, one reader ────────────────
#
# One authority per fact (docs/27 §4b). `section_heading` *writes* a section
# heading and `parse_section_counts` *reads* the counts back, in the same
# module — so the format cannot drift between the two. It did: the reader
# required the heading to end at the count, while `wiki_store` appended an
# annotation after it, and 16 reports across lj/vj/kj lost their DROP count
# (MANUAL.md rendered the miss as "0 DROP" for a report that said 692, and as
# "62" — read from a sentence in the prose — for a report that said 624).

#: `## GO (31)` and `## DROP (692) — 53 ineligible hidden (work authorisation 53)`.
#: The number in brackets is the listed (eligible) count; the annotation after
#: the em dash is commentary, so the reader stops at `)` and ignores the rest of
#: the line.
_SECTION_HEADING_RE = re.compile(
    # The label may contain a slash: the writer emits
    # "## Long-Lasting / Suspicious (12)". Leaving it out silently dropped that
    # section's count — the same class this reader exists to prevent, caught by
    # the round-trip test rather than by production.
    r"^##[ \t]+(?P<label>[A-Za-z][A-Za-z /-]*?)[ \t]*\((?P<n>\d+)\)[^\n]*$",
    re.MULTILINE,
)

#: Heading label -> the key every consumer already uses. The report writes
#: "Long-Lasting / Suspicious"; earlier generations wrote "Long-lasting".
_SECTION_KEYS = {
    "go": "GO",
    "maybe": "MAYBE",
    "flag": "FLAG",
    "drop": "DROP",
    "new listings": "new",
    "still open": "still_open",
    "long-lasting / suspicious": "long_lasting",
    "long-lasting": "long_lasting",
    "long lasting": "long_lasting",
    "longlasting": "long_lasting",
}


def section_heading(section: str, listed: int, hidden: int = 0,
                    reasons: str = "") -> str:
    """The one writer for a report section heading.

    `listed` is the count of adverts the section actually lists; `hidden` are
    the ineligible ones the report records without spending screen space on
    them (LJ, 2026-09-22). The annotation is commentary on the same line as the
    count, which is why the reader tolerates a suffix.
    """
    heading = f"## {section} ({listed})"
    if hidden:
        heading += f" — {hidden} ineligible hidden ({reasons})"
    return heading


def section_heading_label(line: str) -> str | None:
    """The section name if `line` is a report section heading, else None.

    Tolerant of the annotation after the count, for readers that walk a report
    line by line and need the *name* (`stale_audit` opens a section on it and
    does not care about the number). Requiring the line to end at the count made
    `stale_audit` miss `## DROP (692) — 53 ineligible hidden (…)` entirely, so
    every DROP listing was attributed to the preceding FLAG section (842 "FLAG"
    listings in a report with 150 FLAG and 692 DROP, 2026-10-07).
    """
    m = _SECTION_HEADING_RE.match(line)
    return m.group("label").strip() if m else None


def parse_section_counts(text: str) -> dict:
    """The decision counts a report *declares*, read from its section headings.

    Returns only the counts the report actually states. A heading the reader
    cannot read leaves its key **absent** — deliberately: a caller must be able
    to say `unknown`, and must never render "not found in the parsed source" as
    "zero observed" (LEARNINGS 2026-09-14).

    Never guesses a count from prose: a previous fallback searched for a
    `DROP:`-plus-digits pattern anywhere in the report and matched the sentence
    "Staleness alone forces DROP: 62 days old", printing 62 where the report
    said 624 (MANUAL.md, 2026-09-26).
    """
    counts: dict = {}
    for m in _SECTION_HEADING_RE.finditer(text):
        key = _SECTION_KEYS.get(" ".join(m.group("label").lower().split()))
        if key and key not in counts:
            counts[key] = int(m.group("n"))
    return counts


#: Legacy reports declare the counts on a summary line of their own:
#:     **Decisions:** GO: 19 · MAYBE: 48 · FLAG: 44 · DROP: 113
#:     **Lifecycle:** New: 224 · Still open: 0 · Long-lasting: 0
#: or as bare count lines ("GO: 137"). Only such a line may be scanned — an
#: unanchored search over the whole report read the sentence
#: "Staleness alone forces DROP: 62 days old" as a count.
_DECLARED_COUNT_LINE_RE = re.compile(
    r"^(?:\*\*(?:Decisions|Lifecycle):\*\*"
    r"|(?:GO|MAYBE|FLAG|DROP|New|Still open|Long-lasting):)"
    r"[^\n]*$",
    re.MULTILINE,
)

_COUNT_PAIR_RE = re.compile(
    r"\b(?P<label>GO|MAYBE|FLAG|DROP|New|Still open|Long-lasting):[ \t]*(?P<n>\d+)\b"
)

_LEGACY_KEYS = {
    "go": "GO", "maybe": "MAYBE", "flag": "FLAG", "drop": "DROP",
    "new": "new", "still open": "still_open", "long-lasting": "long_lasting",
}


def parse_legacy_counts(text: str) -> dict:
    """Counts from a pre-heading report — declared, never guessed.

    Reads `LABEL: n` pairs only from lines that declare counts (`**Decisions:**`,
    `**Lifecycle:**`, or a bare `GO: 137`). Same contract as
    `parse_section_counts`: only what is stated, and only where it is stated
    *as a count*.
    """
    counts: dict = {}
    for line in _DECLARED_COUNT_LINE_RE.finditer(text):
        for m in _COUNT_PAIR_RE.finditer(line.group(0)):
            key = _LEGACY_KEYS.get(m.group("label").lower())
            if key and key not in counts:
                counts[key] = int(m.group("n"))
    return counts


def _crontab_summary() -> str:
    """The post-severance schedule, one line per lovework crontab entry."""
    path = Path(__file__).resolve().parent.parent / "scripts" / "cron" / "crontab.in"
    try:
        lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines()]
    except OSError:
        return "schedule: scripts/cron/crontab.in (unreadable)"
    entries = []
    for line in lines:
        if not line or line.startswith("#") or "=" in line.split()[0]:
            continue
        parts = line.split()
        if len(parts) >= 6:
            name = Path(parts[5]).name.removesuffix(".sh")
            extra = " ".join(p for p in parts[6:]
                             if not p.startswith(">>") and p != "2>&1"
                             and not p.startswith("/")).strip()
            entries.append(f"{' '.join(parts[:5])}  {name}"
                           f"{(' ' + extra) if extra else ''}")
    return ("crontab (scripts/cron/crontab.in):\n"
            + "\n".join(f"  {e}" for e in entries))


def get_cron_schedule() -> str:
    """Glean the schedule from the active Hermes profile's cron jobs.

    Reads $HERMES_HOME/cron/jobs.json (set by Hermes when cron jobs are
    created). Returns a one-line summary of each scheduled job, or a
    fallback message if the file can't be read.
    """
    hermes_home = hermes_home_or_none()
    if hermes_home is None:
        return _crontab_summary()
    jobs_path = hermes_home / "cron" / "jobs.json"
    if not jobs_path.exists():
        # Try parent (profile fallthrough) — profile-a profile lives under
        # ~/.hermes-host-a/profiles/profile-a/cron/ when default is .hermes-host-a
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


#: The profile files the matcher actually reads, with the question each answers.
#: `load_profile_text` (config.py) combines exactly these, so this list is what a
#: reader must consult to answer "why didn't this advert score higher?".
PROFILE_PARTS = (
    ("soul.md", "identity, wants, avoids — the substantive criteria"),
    ("work_auth.md", "where the principal may live and work"),
    ("market-position.md", "feasibility reality and flourishing signals"),
    ("cv-short.md", "the CV the matcher sees"),
    ("possibilities.md", "the branching tracks"),
)


def profile_reference_lines(profile_label: str) -> list[str]:
    """Where the scoring criteria live, for a reader questioning a verdict.

    The dashboard publishes principals' *results* (`/principals/<p>/…`) and a
    small docs surface; it deliberately does not publish `profiles/`, so these
    are disk paths with that stated plainly rather than a broken web link.
    """
    import config

    principal = (profile_label or "").split("-")[0].lower()
    role = profile_label.split("-", 1)[1] if "-" in (profile_label or "") else "general"
    # Anchored on the repo, never the caller's CWD (a crawl runs from
    # lovework-agent/, a toolbox call from anywhere).
    pdir = config.PROFILES_DIR / principal
    shown = f"profiles/{principal}"
    lines = [f"The matcher scored every advert against the **{profile_label}** profile."]
    for name, purpose in PROFILE_PARTS:
        if (pdir / name).is_file():
            lines.append(f"- `{shown}/{name}` — {purpose}")
    if (pdir / "roles" / f"{role}.md").is_file():
        lines.append(f"- `{shown}/roles/{role}.md` — role-specific criteria for this run")
    lines.append("")
    lines.append("(Disk paths are authoritative; the dashboard publishes results, not profiles.)")
    return lines


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
        f"Profile: **{profile_label}**",
        "",
        "## How this run scored",
        "",
        *profile_reference_lines(profile_label),
    ]
    if sources:
        lines.append(f"Sources this run: {', '.join(sources)}")

    lines.append("")

    return lines


def build_trailer() -> list[str]:
    """The end of a report: what runs where, then the schedule.

    These used to open the report, so the first thing a reader saw was
    operational plumbing instead of findings. They are reference material —
    read once in a while, not per crawl — so they close the report instead
    (LJ, 2026-09-13).
    """
    return [
        f"Runtime that ran this crawl: {runtime_identity()}",
        "",
        EXPLAINER,
        "",
        "## Schedule",
        "",
        get_cron_schedule(),
        "",
        "---",
        "",
    ]
