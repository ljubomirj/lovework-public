#!/usr/bin/env python3
"""
Generate MANUAL.md (in the state dir) — the human-facing operator's page.
It is a projection of state, so it is written beside the state, not into the
code checkout, and it is not tracked in git (2026-11-03).

The manual is a self-contained markdown file with the most actionable
information LJ needs to make a decision: what's the latest, how to
refresh, and what happened in the last run. It's regenerated from the
current state of the registry, the latest wiki report, and the run
history. Re-run any time to update.

Sections produced:
  1. Headline numbers (registry stats + last-run summary)
  2. Latest greatest — the top 10 GOs from the most recent report
  3. Refresh now — copy-pasteable CLI commands
  4. Run log — the last 7 reports with date + decision counts
  5. Cross-check log — orgs flagged with prior contact in the last report
  6. Pointers — files, env vars, skills, cron
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import config
from report_header import parse_legacy_counts, parse_section_counts
from writer_boundary import WriterBusy, standalone_writer_guard
from job_registry import JobRegistry


# ── Helpers ───────────────────────────────────────────────────────────────

# H1 forms seen in the report series (all end with "— YYYY-MM-DD HH:MM:SS TZ"):
#   legacy full sweep: "# FULL SWEEP — 2026-06-29 14:28:13 BST"
#   current full:      "# FULL — 2026-09-12 14:30:57 BST"
#   incremental:       "# INCREMENTAL — 2026-06-29 14:33:37 BST"
_H1_RE = re.compile(
    r"^#\s+(?P<kind>FULL SWEEP|FULL|INCREMENTAL|REPLAY|CONSUME|REASSESSMENT)"
    r"\s+—\s+(?P<date>\d{4}-\d{2}-\d{2})",
    re.MULTILINE,
)

# Decision counts are read from the report's own declarations, not parsed here:
# `wiki_store` writes the section headings, so it owns the reader for them
# (`parse_section_counts` / `parse_legacy_counts`). A local copy of that regex
# was the 2026-10-07 defect — it required the heading to end at the count and
# silently lost `## DROP (692) — 53 ineligible hidden (…)`.


def _count_scorelines(text: str) -> dict:
    """Derive counts by counting `9.0/10 (GO)` score lines — the last resort.

    Only used when the report declares no counts at all (an incremental report
    lists its decisions inline). It is a *derived* count, and the caller records
    that (`counts_source`): one advert can carry a score line in more than one
    section, so this can over-count where the headings cannot.
    """
    counts: dict = {}
    for k in ("GO", "MAYBE", "FLAG", "DROP"):
        n = len(re.findall(rf"\d+(?:\.\d+)?/10\s*\({k}\)", text))
        if n:
            counts[k] = n
    return counts


def _read_report_header(path: Path) -> dict:
    """Extract date + decision counts from a wiki report.

    The counts come from what the report *declares*, read through the writer's
    own reader (`wiki_store.parse_section_counts`) — the current runs state them
    in the section headings (`## GO (137)`), legacy full sweeps in a `GO: 137`
    block, incremental reports only in `9.0/10 (GO)` score lines.

    A count the report does not state stays **absent** from the result, and the
    renderers print `unknown` for it. Never `0`: that is the defect this reader
    exists to prevent (LEARNINGS 2026-09-14 — the manual advertised "0 GO,
    0 MAYBE, 0 FLAG, 0 DROP" for a report holding 137 GOs, and on 2026-10-07 it
    printed "0 DROP" for a report heading that said `## DROP (692) — 53
    ineligible hidden (…)`).

    `counts_source` records how the counts were obtained — `headings`,
    `legacy`, `scorelines` or `none` — so a declared count is never confused
    with a derived one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    out = {}
    # Profile is on a line after the H1: "Profile: LJ-general"
    m = _H1_RE.search(text)
    if m:
        out["date"] = m.group("date")
        out["kind"] = m.group("kind")
        pm = re.search(r"^Profile:\s*(.+)$", text, re.MULTILINE)
        out["profile"] = pm.group(1).strip() if pm else "unknown"

    # Declared first, headings second, score lines last. A legacy report states
    # its counts on a `**Decisions:** …` line — a complete declaration, so it
    # wins per key — while a heading states only that section's listed count,
    # and a score line is derived. Nothing is guessed, and a key no source
    # states stays absent (the renderers print `unknown`).
    counts: dict = {}
    used: list[str] = []
    for name, reader in (("declared", parse_legacy_counts),
                         ("headings", parse_section_counts)):
        found = reader(text)
        if found:
            used.append(name)
        for key, value in found.items():
            counts.setdefault(key, value)
    if not counts:
        counts = _count_scorelines(text)
        used = ["scorelines"] if counts else []
    out.update(counts)
    out["counts_source"] = "+".join(used) or "none"
    return out


def _extract_top_gos(report_path: Path, n: int = 10) -> list[dict]:
    """Pull the top-n GO listings from a report (### Org — Title sections).

    Two score-line formats are accepted:
      1. `**Score**: 9.0/10`  (older reports)
      2. `**Score**: 9.0/10 (GO)`  (newer reports; decision inline)
    And two decision-line formats:
      1. `**Decision**: GO (9.0/10)`  (very old reports)
    Sections inside "## GO Listings" or "## New Listings" are treated as
    GO-by-default when the report has no explicit decision line.
    """
    SECTION_RE = re.compile(r"^###\s+(?P<org>.+?)\s+—\s+(?P<title>.+?)\s*$")
    URL_RE = re.compile(r"^\s*-\s*\*\*URL\*\*:\s*(?P<url>\S+)")
    LOC_RE = re.compile(r"^\s*-\s*\*\*Location\*\*:\s*(?P<loc>.+)")
    SCORE_RE = re.compile(
        r"^\s*-\s*\*\*Score\*\*:\s*(?P<score>\d+(?:\.\d+)?)/10"
        r"(?:\s*\((?P<decision_inline>GO|MAYBE|FLAG|DROP)\))?"
    )
    DECISION_RE = re.compile(
        r"^\s*-\s*\*\*Decision\*\*:\s*\*?(?P<decision>GO|MAYBE|FLAG|DROP)\*?"
    )
    out: list[dict] = []
    section: dict | None = None
    in_go_or_new_section = False

    def flush() -> None:
        nonlocal section
        if section and section.get("score") is not None:
            decision = section.get("decision")
            if not decision and in_go_or_new_section:
                decision = "GO"
            if decision == "GO":
                out.append(section)
        section = None

    for line in report_path.read_text(encoding="utf-8").splitlines():
        sec = SECTION_RE.match(line.rstrip())
        if sec:
            flush()
            # Report headings carry the section's list ordinal ("### 1. Genesis
            # — Data Agent"); it is not part of the org name.
            org = re.sub(r"^\d+\.\s*", "", sec.group("org").strip())
            section = {"org": org, "title": sec.group("title").strip()}
            continue
        # H2 boundary: flush and update section flags.
        if line.startswith("## ") or line.startswith("# "):
            flush()
            low = line.lower()
            if "## go" in low or "## new listings" in low:
                in_go_or_new_section = True
            else:
                in_go_or_new_section = False
            continue
        if section is None:
            continue
        if not section.get("url"):
            m = URL_RE.match(line)
            if m:
                section["url"] = m.group("url").strip()
        if not section.get("location"):
            m = LOC_RE.match(line)
            if m:
                section["location"] = m.group("loc").strip()
        m = SCORE_RE.match(line)
        if m:
            section["score"] = float(m.group("score"))
            if m.group("decision_inline"):
                section["decision"] = m.group("decision_inline")
        m = DECISION_RE.match(line)
        if m:
            section["decision"] = m.group("decision")
    flush()
    out.sort(key=lambda x: x.get("score") or 0, reverse=True)
    # One advert can appear under both `## GO` and `## New Listings` (and under
    # Long-Lasting), so the ranked list must dedupe by advert identity before
    # slicing — otherwise the manual prints every top GO two or three times.
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict] = []
    for row in out:
        key = (row.get("url") or "", row["org"].lower(), row["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped[:n]


def _run_log(limit: int = 7) -> list[dict]:
    """Return the last N reports, sorted newest first by report DATE
    (not file mtime — mtime gets jumbled by test runs / restoration).
    """
    reports = list(config.WIKI_REPORTS.glob("*.md"))
    dated = []
    for r in reports:
        h = _read_report_header(r)
        # Fall back to the filename's date if the body has no H1.
        date = h.get("date", "")
        if not date:
            m = re.search(r"(\d{4}-\d{2}-\d{2})", r.stem)
            if m:
                date = m.group(1)
        dated.append((date, r, h))
    dated.sort(key=lambda t: (t[0], t[1].name), reverse=True)
    out = []
    for date, r, h in dated[:limit]:
        out.append({"file": r.name, "date": date, **h})
    return out


def _latest_report() -> Path | None:
    """Return the report with the newest DATE in the H1 (not mtime)."""
    reports = list(config.WIKI_REPORTS.glob("*.md"))
    dated = []
    for r in reports:
        h = _read_report_header(r)
        date = h.get("date", "")
        if not date:
            m = re.search(r"(\d{4}-\d{2}-\d{2})", r.stem)
            if m:
                date = m.group(1)
        dated.append((date, r))
    if not dated:
        return None
    dated.sort(key=lambda t: (t[0], t[1].name), reverse=True)
    return dated[0][1]


def _cross_check_blocks() -> list[dict]:
    """Find prior-contact cross-check blocks added today in the wiki orgs pages."""
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for p in sorted(config.WIKI_ORGS.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        if today in text and "prior contact found" in text:
            # Extract the summary line for compact display.
            m = re.search(r"\*\*Prior contact\*\*:\s*(.+)", text)
            if m:
                out.append({"org": p.stem, "summary": m.group(1).strip()})
    return out


# ── Markdown renderers ────────────────────────────────────────────────────

def _render_top_gos(gos: list[dict]) -> str:
    if not gos:
        return "_No GO listings in the most recent report._\n"
    lines = ["| Score | Org | Role | Location |",
             "|------:|-----|------|----------|"]
    for g in gos:
        score = f"{g.get('score', 0):.1f}" if g.get('score') is not None else "—"
        org = g.get("org", "")
        title = g.get("title", "")
        loc = g.get("location", "—")
        lines.append(f"| {score} | {org} | {title} | {loc} |")
    return "\n".join(lines) + "\n"


def _count(row: dict, key: str) -> str:
    """The count as it should be shown: the number, or `unknown`.

    Never `0` for an absent count. "Not found in the parsed source" and "zero
    observed" are different claims (docs/27 §3), and printing the first as the
    second is how the manual advertised "0 DROP" for a report that said 692.
    """
    value = row.get(key)
    return str(value) if value is not None else "unknown"


def _render_run_log(log: list[dict]) -> str:
    if not log:
        return "_No reports yet._\n"
    lines = ["| Date | Report | GO | MAYBE | FLAG | DROP | New |",
             "|------|--------|---:|------:|-----:|-----:|----:|"]
    for r in log:
        date = r.get("date", "—")
        fn = r.get("file", "")
        cells = " | ".join(_count(r, k) for k in ("GO", "MAYBE", "FLAG", "DROP", "new"))
        lines.append(f"| {date} | `{fn}` | {cells} |")
    return "\n".join(lines) + "\n"


def _render_cross_check(blocks: list[dict]) -> str:
    if not blocks:
        return "_No prior-contact cross-checks today._\n"
    lines = ["| Org | Prior contact |", "|-----|---------------|"]
    for b in blocks:
        lines.append(f"| {b['org']} | {b['summary']} |")
    return "\n".join(lines) + "\n"


# ── Main ──────────────────────────────────────────────────────────────────

def main(argv=()):
    """Direct invocation: hold the writer lease, then regenerate MANUAL.md."""
    parser = argparse.ArgumentParser(description="Regenerate the shared LJ operator manual")
    parser.add_argument("--profile", default="lj", choices=["lj"],
                        help="Principal (the shared manual currently describes LJ)")
    args = parser.parse_args(argv)
    principal = args.profile
    state_root = config.STATE_DIR / principal
    try:
        with standalone_writer_guard(principal, mode="direct", state_root=state_root,
                                     shared=("manual",)):
            return _build_manual_body()
    except WriterBusy as busy:
        print(f"[LOCK] writer busy: {busy.resource.key} — another operation holds "
              f"this principal's lease (attempt recorded at {busy.attempt_path})")
        raise SystemExit(75)


def _pct_of(part: int, whole: int) -> str:
    """A share, for a report that must show what it divided (LJ, 2026-10-13)."""
    return f"{100.0 * part / whole:.0f}%" if whole else "—"


def _build_manual_body():
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    registry = JobRegistry()
    stats = registry.stats()

    reports = list(config.WIKI_REPORTS.glob("*.md"))
    dated = []
    for r in reports:
        h = _read_report_header(r)
        date = h.get("date", "")
        if not date:
            m = re.search(r"(\d{4}-\d{2}-\d{2})", r.stem)
            if m:
                date = m.group(1)
        dated.append((date, r, h))
    dated.sort(key=lambda t: (t[0], t[1].name), reverse=True)
    latest = dated[0][1] if dated else None
    latest_header = dated[0][2] if dated else {}
    top_gos = _extract_top_gos(latest) if latest else []
    run_log = _run_log(limit=7)
    cross_check = _cross_check_blocks()

    parts = [
        f"# LoveWork — Operator's Manual",
        "",
        f"_Regenerated {today} from live state. Re-run `python lovework-agent/build_manual.py` to refresh._",
        "",
        "**lovework** (lovework.be) is LJ's personal job-discovery agent.",
        "It crawls org career pages, scores leads against the LJ profile, and writes findings to a markdown wiki.",
        "This page is the human-facing landing — it points to the latest results, the refresh command, the run log, and the cross-check status.",
        "",
        "---",
        "",
        "## 1. Headline",
        "",
        "**Registry:**",
    ]
    for status, n in sorted(stats.items()):
        parts.append(f"- `{status}`: {n}")
    parts.append("")
    if latest:
        parts.append(f"**Last report**: `{latest.name}` — "
                     + ", ".join(f"{_count(latest_header, k)} {label}"
                                 for k, label in (("GO", "GO"), ("MAYBE", "MAYBE"),
                                                  ("FLAG", "FLAG"), ("DROP", "DROP"),
                                                  ("new", "new")))
                     + ("" if latest_header.get("counts_source") in ("headings", None)
                        else f" _(counts {latest_header['counts_source']})_"))
    else:
        parts.append("**Last report**: _no reports yet_")
    parts.append("")

    # What the models cost us (LJ, 2026-10-13: "are there any reports that use
    # these stats?"). A pure read of the ledgers, and the one place the operator
    # manual carries spend; the full per-provider view and the engine's own
    # numbers are the daily `logs/inference-<date>.md` report.
    try:
        import usage_stats

        usage = usage_stats.aggregate(days=30)
        calls = sum(r["calls"] for r in usage["rows"])
        tokens = sum(r["total_tokens"] for r in usage["rows"])
        unmetered = sum(r["unmetered"] for r in usage["rows"])
        # Ratios carry their own numerator and denominator (LJ, 2026-10-13).
        usage_lines = [f"- **{calls:,} model calls**, **{tokens:,} tokens** in the "
                       f"last 30 days"
                       + (f" — **{_pct_of(unmetered, calls)}** unmetered "
                          f"(={unmetered:,} of {calls:,} calls)"
                          if unmetered else "")]
        for row in usage["rows"]:
            usage_lines.append(
                f"- `{row['provider']}` / `{row['model']}`: {row['calls']:,} calls, "
                f"{row['total_tokens']:,} tokens"
                + (f", {row['unmetered']:,} unmetered" if row["unmetered"] else ""))
        usage_lines.append("")
        usage_lines.append("Per-provider detail, the agentic loop's cost per pack, and the "
                           "local engine's own throughput: `state/example/logs/inference-<date>.md` "
                           "(written daily) or `usage_stats.py --report`.")
    except Exception as exc:                       # a manual must still build
        usage_lines = [f"_usage unavailable: {type(exc).__name__}: {exc}_"]

    parts.extend([
        "---",
        "",
        "## 2. Model usage — what the last 30 days cost",
        "",
        *usage_lines,
        "",
        "---",
        "",
        "## 3. Latest greatest — top GOs from the most recent report",
        "",
        _render_top_gos(top_gos).rstrip(),
        "",
        "Full listings are in `state/example/wiki/reports/` and `state/example/wiki/orgs/`.",
        "",
        "---",
        "",
        "## 4. Refresh now",
        "",
        "Pick the right command for the cadence:",
        "",
        "**Quick cross-check** (no LLM, ~5s, reads the latest wiki report + applications/ + Gmail):",
        "",
        "```bash",
        "cd $LOVEWORK_ROOT/lovework-agent",
        "../venv/bin/python3 crosscheck.py                          # all reports, all GOs",
        "../venv/bin/python3 crosscheck.py --org Poolside           # specific org",
        "../venv/bin/python3 crosscheck.py --include-maybe          # also check MAYBEs",
        "```",
        "",
        "**Incremental crawl** (live HN + neolabs, bounded by cost caps — "
        "measured 44-90 min, ~540-600 LLM calls ≈ ~$1 at DeepSeek off-peak list):",
        "",
        "```bash",
        "cd $LOVEWORK_ROOT/lovework-agent",
        "../venv/bin/python3 incremental_crawl.py                   # see incremental_crawl.py for cost caps",
        "```",
        "",
        "**Full pipeline** (all 12 sources; measured 1.8-14 h for LJ full sweeps — "
        "the 2026-10-10 run took 14 h 00 m 01 s, which is why it starts at 08:00 now — "
        "1,855-2,113 LLM calls ≈ $3.7 at DeepSeek off-peak list — docs/30 §4):",
        "",
        "```bash",
        "cd $LOVEWORK_ROOT/lovework-agent",
        "../venv/bin/python3 main.py --profile lj --role general --source all --report",
        "```",
        "",
        "**Regenerate this manual** (after any pipeline run):",
        "",
        "```bash",
        "cd $LOVEWORK_ROOT/lovework-agent",
        "../venv/bin/python3 build_manual.py",
        "```",
        "",
        "**Re-distribute the agent skill** (after editing `agents/skills/lovework-hydrate/SKILL.md`):",
        "",
        "```bash",
        "cd $LOVEWORK_ROOT",
        "bash sync_skills.sh            # in-repo symlinks + copy to Hermes per-host",
        "bash sync_skills.sh --symlink # symlinks everywhere (one-way repo -> per-host)",
        "```",
        "",
        "**Re-score historical entries** (after tightening matcher rules, e.g. the org-level cooldown):",
        "",
        "```bash",
        "cd $LOVEWORK_ROOT/lovework-agent",
        "../venv/bin/python3 rescore.py                    # all orgs",
        "../venv/bin/python3 rescore.py --org Poolside    # just one org",
        "../venv/bin/python3 rescore.py --dry-run         # show what would change without writing",
        "```",
        "",
        "---",
        "",
        "## 5. Run log — last 7 reports",
        "",
        _render_run_log(run_log).rstrip(),
        "",
        "All reports live under `state/example/wiki/reports/`.",
        "",
        "---",
        "",
        "## 6. Cross-check log — prior contact found today",
        "",
        _render_cross_check(cross_check).rstrip(),
        "",
        "If a GO appears here, check the wiki/orgs/ page (linked from the org name) for the full prior-contact block. The `crosscheck.py --org <name>` command refreshes it.",
        "",
        "---",
        "",
        "## 7. Pointers",
        "",
        "**Engine**",
        "- `lovework-agent/pipeline.py` — importable core (`run_pipeline()`); the CLI, the agent REPL, and the future FastAPI all call it.",
        "- `lovework-agent/main.py` — CLI wrapper (`--profile lj --role general --source all --report`).",
        "- `lovework-agent/agent.py` — interactive `LoveWorkAgent` (REPL).",
        "- `lovework-agent/cases.py` — lead → case slug convention (`YYYYMMDD-Company-Role`).",
        "",
        "**Sources** (`lovework-agent/sources/`)",
        "- `research_orgs.py` — 19 research orgs (juleslogs).",
        "- `neolabs.py` — `neolab-and-emerging-ai-lab-tracker.txt` (cleverhack).",
        "- `neoclouds.py` — `neocloud-and-compute-tracker.txt` (curated neocloud/compute list).",
        "- `speech_companies.py` — `speech-and-voice-ai-companies-tracker.txt` (curated speech/voice-AI company list).",
        "- `hf_startups.py` — `AI-for-HF-startup-tracker/` (Alex Izydorczyk).",
        "- `hn_hiring.py` — live HN Algolia API for the monthly \"Ask HN: Who is hiring?\" thread.",
        "- `hn_jobs.py` — live `news.ycombinator.com/jobs` (21-day recency filter).",
        "- `gmail_lj_jobs.py` — polls the Gmail `LJ-jobs` label; parses LinkedIn, JobServe, Totaljobs, CWJobs, TalentSource, and Rec-London alerts; captures supported search URLs as seeds.",
        "- `linkedin_related.py` — follows LJ-maintained LinkedIn seeds, harvests related jobs (JSON-LD parser), logs auth walls.",
        "- `company_pages.py` — LJ's curated `company_pages.yaml` with per-entry re-crawl cadence.",
        "- `harnham.py` — LJ's Harnham recruiter search URLs (`harnham_searches.yaml`).",
        "- `digitaljobs.py` — Digital Jobs MCP job-board API (anonymous JSON-RPC).",
        "",
        "**Auxiliary**",
        "- `crosscheck.py` — append-only prior-contact check across wiki reports, applications/, and Gmail.",
        "- `incremental_crawl.py` — bounded incremental sweep (cost-capped; designed for ad-hoc runs).",
        "- `build_manual.py` — regenerates this `MANUAL.md` from live state.",
        "- `rescore.py` — re-runs the pre-LLM kills (org-level cooldown, work-auth) over historical wiki entries; rebuilds the index. Use after tightening matcher rules.",
        "- `sync_skills.sh` — re-distributes the canonical `agents/skills/lovework-hydrate/SKILL.md` to `.claude/skills/`, `.codex/skills/`, and the per-host Hermes path. Run after editing the canonical.",
        "- `cases.py` — lead → case helpers (`slug_for`, `make_case_dir`, `case_status`).",
        "- `outcome_link.py` — outcome → assessment join: case-dossier identity, the resolution ladder, `outcome_joins.jsonl`, and the per-run join-rate metric (`join_metrics.jsonl`).",
        "",
        "**Skill distribution** (D16)",
        "- `agents/skills/lovework-hydrate/SKILL.md` — canonical (single source of truth).",
        "- `.claude/skills/lovework-hydrate/SKILL.md` — symlink → canonical (for Claude Code, OpenCode).",
        "- `.codex/skills/lovework-hydrate/SKILL.md` — symlink → canonical (for Codex).",
        "- `~/.hermes-<host>/profiles/<profile>/skills/productivity/lovework-hydrate/SKILL.md` — per-host Hermes copy (re-synced by `sync_skills.sh`).",
        "",
        "**Profile** (`profiles/example/`)",
        "- `soul.md` — who LJ is, what LJ wants, what LJ avoids.",
        "- `work_auth.md` — work-authorization / visa rules (drives the matcher's pre-LLM hard-kill).",
        "- `cv-short.md`, `bio-long.md` — context for the matcher.",
        "- `roles/*.md` — role-specific criteria (general, contract-ai, cofounder, ai-finance).",
        "- `company_pages.yaml` — curated keep-list with re-crawl cadence (auto-seeded on first run).",
        "- `linkedin_seeds.md` — LinkedIn search URLs to harvest related jobs from (auto-populated by `gmail_lj_jobs`).",
        "- `linkedin_needs_auth.md` — LinkedIn URLs that hit an auth wall (manual follow-up).",
        "",
        "**Cron** (Hermes, profile-scoped — the real schedule)",
        "- host-a / `profile-a`: LJ full sweep Sat 08:00 (+ watchdog 22:30, meta-investigator 22:35),",
        "  VJ full sweep Sun 10:00 (+ watchdog 12:00, investigator 12:05), KJ full sweep Sun 14:00",
        "  (+ watchdog 16:00, investigator 16:05), `lovework-kj-consume` Sun 06:30, and a",
        "  link re-check ~3 h after each sweep. No incremental jobs are loaded.",
        "- `lovework-agent/com.lj.lovework.plist` (launchd, Mon/Wed/Fri 09:00) exists but is **not loaded**.",
        "- Completion is proven by the run record in `state/<p>/cache/runs/` plus a Gmail message id —",
        "  never by the launcher's acknowledgement.",
        "",
        "**Skills**",
        "- Canonical: `agents/skills/lovework-hydrate/SKILL.md` (agent) + `agents/skills/report-heal/SKILL.md`",
        "- Per-host Hermes copies are re-synced by `sync_skills.sh`.",
        "",
        "**Docs**",
        "- `lovework/DECISIONS.md` — 21 decisions (D1-D21), with why, plus Phase 1/2 progress.",
        "- `lovework/README.LJ` — agent turn log (the conversation that built it).",
        "- `lovework-agent/ARCHITECTURE.md` — inherited design doc.",
        "- `lovework-agent/README.md` — quickstart + source list.",
        "",
    ])

    # MANUAL.md is a projection of state, so it lives with the state (2026-11-03).
    # Writing it to LOVEWORK_ROOT dirtied a **tracked** file on every lj crawl, and
    # deploy.sh's code-dirty gate then refused to deploy until it was reverted.
    out_path = config.STATE_DIR / "MANUAL.md"
    out_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(f"  registry: {stats}")
    print(f"  latest report: {latest.name if latest else '(none)'}")
    print(f"  top GOs rendered: {len(top_gos)}")
    print(f"  cross-checks today: {len(cross_check)}")


if __name__ == "__main__":
    main()
