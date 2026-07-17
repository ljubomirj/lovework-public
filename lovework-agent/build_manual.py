#!/usr/bin/env python3
"""
Generate lovework/MANUAL.md — the human-facing operator's page.

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

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import config
from job_registry import JobRegistry


# ── Helpers ───────────────────────────────────────────────────────────────

def _read_report_header(path: Path) -> dict:
    """Extract date + decision counts from the H1 of a wiki report.

    Handles both the full-sweep format ("# FULL SWEEP — YYYY-MM-DD HH:MM:SS TZ")
    and the incremental format ("# INCREMENTAL — YYYY-MM-DD HH:MM:SS TZ").
    For the incremental report, decision counts are taken from the
    "## Summary" table where available.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    out = {}
    # H1 formats (both end with "— YYYY-MM-DD HH:MM:SS TZ"):
    #   FULL SWEEP:  "# FULL SWEEP — 2026-06-29 14:28:13 BST"
    #   INCREMENTAL: "# INCREMENTAL — 2026-06-29 14:33:37 BST"
    # Profile is on a subsequent line: "Profile: LJ-general"
    m = re.search(r"#\s+(FULL SWEEP|INCREMENTAL)\s+—\s+(\d{4}-\d{2}-\d{2})", text)
    if m:
        out["date"] = m.group(2)
        if m.group(1) == "INCREMENTAL":
            out["profile"] = "incremental"
        else:
            # Extract profile label from the "Profile: ..." line after the H1
            pm = re.search(r"^Profile:\s*(.+)$", text, re.MULTILINE)
            out["profile"] = pm.group(1).strip() if pm else "unknown"
    for k in ("GO", "MAYBE", "FLAG", "DROP"):
        m = re.search(rf"\b{k}:\s*(\d+)", text)
        if m:
            out[k] = int(m.group(1))
    for k in ("New", "Still open", "Long-lasting"):
        m = re.search(rf"\b{re.escape(k)}:\s*(\d+)", text)
        if m:
            out[k.lower().replace(" ", "_")] = int(m.group(1))
    # Fallback: for incremental reports, count ### sections with "X.X/10 (DECISION)".
    if "GO" not in out:
        for k in ("GO", "MAYBE", "FLAG", "DROP"):
            n = len(re.findall(rf"\d+(?:\.\d+)?/10\s*\({k}\)", text))
            if n:
                out[k] = n
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
            section = {"org": sec.group("org").strip(), "title": sec.group("title").strip()}
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
    return out[:n]


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


def _render_run_log(log: list[dict]) -> str:
    if not log:
        return "_No reports yet._\n"
    lines = ["| Date | Report | GO | MAYBE | FLAG | DROP | New |",
             "|------|--------|---:|------:|-----:|-----:|----:|"]
    for r in log:
        date = r.get("date", "—")
        fn = r.get("file", "")
        go = r.get("GO", 0)
        maybe = r.get("MAYBE", 0)
        flag = r.get("FLAG", 0)
        drop = r.get("DROP", 0)
        new = r.get("new", 0)
        lines.append(f"| {date} | `{fn}` | {go} | {maybe} | {flag} | {drop} | {new} |")
    return "\n".join(lines) + "\n"


def _render_cross_check(blocks: list[dict]) -> str:
    if not blocks:
        return "_No prior-contact cross-checks today._\n"
    lines = ["| Org | Prior contact |", "|-----|---------------|"]
    for b in blocks:
        lines.append(f"| {b['org']} | {b['summary']} |")
    return "\n".join(lines) + "\n"


# ── Main ──────────────────────────────────────────────────────────────────

def main():
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
                     f"{latest_header.get('GO', 0)} GO, "
                     f"{latest_header.get('MAYBE', 0)} MAYBE, "
                     f"{latest_header.get('FLAG', 0)} FLAG, "
                     f"{latest_header.get('DROP', 0)} DROP, "
                     f"{latest_header.get('new', 0)} new")
    else:
        parts.append("**Last report**: _no reports yet_")
    parts.append("")

    parts.extend([
        "---",
        "",
        "## 2. Latest greatest — top GOs from the most recent report",
        "",
        _render_top_gos(top_gos).rstrip(),
        "",
        "Full listings are in `lovework-agent/wiki/reports/` and `wiki/orgs/`.",
        "",
        "---",
        "",
        "## 3. Refresh now",
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
        "**Incremental crawl** (live HN + neolabs, ~3-15 min, ~$0.05-0.20 in DeepSeek):",
        "",
        "```bash",
        "cd $LOVEWORK_ROOT/lovework-agent",
        "../venv/bin/python3 incremental_crawl.py                   # see incremental_crawl.py for cost caps",
        "```",
        "",
        "**Full pipeline** (all sources, 20-30 min, ~$0.15-0.50):",
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
        "**Re-distribute the agent skill** (after editing `agent/skills/lovework/SKILL.md`):",
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
        "## 4. Run log — last 7 reports",
        "",
        _render_run_log(run_log).rstrip(),
        "",
        "All reports live under `lovework-agent/wiki/reports/`.",
        "",
        "---",
        "",
        "## 5. Cross-check log — prior contact found today",
        "",
        _render_cross_check(cross_check).rstrip(),
        "",
        "If a GO appears here, check the wiki/orgs/ page (linked from the org name) for the full prior-contact block. The `crosscheck.py --org <name>` command refreshes it.",
        "",
        "---",
        "",
        "## 6. Pointers",
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
        "- `hf_startups.py` — `AI-for-HF-startup-tracker/` (Alex Izydorczyk).",
        "- `hn_hiring.py` — live HN Algolia API for the monthly \"Ask HN: Who is hiring?\" thread.",
        "- `hn_jobs.py` — live `news.ycombinator.com/jobs` (21-day recency filter).",
        "- `gmail_lj_jobs.py` — polls the Gmail `LJ-jobs` label; parses LinkedIn, JobServe, Totaljobs, CWJobs, TalentSource, and Rec-London alerts; captures supported search URLs as seeds.",
        "- `linkedin_related.py` — follows LJ-maintained LinkedIn seeds, harvests related jobs (JSON-LD parser), logs auth walls.",
        "- `company_pages.py` — LJ's curated `company_pages.yaml` with per-entry re-crawl cadence.",
        "",
        "**Auxiliary**",
        "- `crosscheck.py` — append-only prior-contact check across wiki reports, applications/, and Gmail.",
        "- `incremental_crawl.py` — bounded incremental sweep (cost-capped; designed for ad-hoc runs).",
        "- `build_manual.py` — regenerates this `MANUAL.md` from live state.",
        "- `rescore.py` — re-runs the pre-LLM kills (org-level cooldown, work-auth) over historical wiki entries; rebuilds the index. Use after tightening matcher rules.",
        "- `sync_skills.sh` — re-distributes the canonical `agent/skills/lovework/SKILL.md` to `.claude/skills/`, `.codex/skills/`, and the per-host Hermes path. Run after editing the canonical.",
        "- `cases.py` — lead → case helpers (`slug_for`, `make_case_dir`, `case_status`).",
        "",
        "**Skill distribution** (D16)",
        "- `agent/skills/lovework/SKILL.md` — canonical (single source of truth).",
        "- `.claude/skills/lovework/SKILL.md` — symlink → canonical (for Claude Code, OpenCode).",
        "- `.codex/skills/lovework/SKILL.md` — symlink → canonical (for Codex).",
        "- `~/.hermes-macbook2/.../skills/productivity/lovework/SKILL.md` — per-host Hermes copy (re-synced by `sync_skills.sh`).",
        "",
        "**Profile** (`profiles/lj/`)",
        "- `soul.md` — who LJ is, what LJ wants, what LJ avoids.",
        "- `work_auth.md` — work-authorization / visa rules (drives the matcher's pre-LLM hard-kill).",
        "- `cv-short.md`, `bio-long.md` — context for the matcher.",
        "- `roles/*.md` — role-specific criteria (general, contract-ai, cofounder, ai-finance).",
        "- `company_pages.yaml` — curated keep-list with re-crawl cadence (auto-seeded on first run).",
        "- `linkedin_seeds.md` — LinkedIn search URLs to harvest related jobs from (auto-populated by `gmail_lj_jobs`).",
        "- `linkedin_needs_auth.md` — LinkedIn URLs that hit an auth wall (manual follow-up).",
        "",
        "**Cron** (`com.lj.lovework.plist`)",
        "- Mon / Wed / Fri 09:00 — runs the full pipeline.",
        "- Currently **not loaded** (the old work-like cron may still be active).",
        "- Install: `cp lovework-agent/com.lj.lovework.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.lj.lovework.plist`",
        "",
        "**Skills**",
        "- Claude: `lovework/.claude/skills/lovework/SKILL.md`",
        "- Hermes: `~/.hermes/skills/productivity/lovework/SKILL.md`",
        "",
        "**Docs**",
        "- `lovework/DECISIONS.md` — 13 decisions, with why, plus Phase 1/2 progress.",
        "- `lovework/README.LJ` — agent turn log (the conversation that built it).",
        "- `lovework-agent/ARCHITECTURE.md` — inherited design doc.",
        "- `lovework-agent/README.md` — quickstart + source list.",
        "",
    ])

    out_path = config.LOVEWORK_ROOT / "MANUAL.md"
    out_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(f"  registry: {stats}")
    print(f"  latest report: {latest.name if latest else '(none)'}")
    print(f"  top GOs rendered: {len(top_gos)}")
    print(f"  cross-checks today: {len(cross_check)}")


if __name__ == "__main__":
    main()
