"""
LoveWork Dashboard Server.

A small HTTP server that renders a single-page dashboard with live state
from the LoveWork wiki, logs, job registry, profile config, and Hermes
cron jobs. Reads from existing data sources at request time -- does NOT
maintain parallel state. Any source file change is reflected on the
next page load.

Sections:
- Runs: recent log files + last entry from each
- Jobs: top GO listings from latest report + registry counts
- Profiles: configured profiles (LJ, VJ, example) + role files
- Entities: orgs that originated job listings
- Sources: from wiki/sources.md
- Reports: list of past reports
- Config: LLMs in use + cron schedule
- System: paths, state file location, gateway health

Run with:
    python3 dashboard_server.py [--port 8765]
    python3 dashboard_server.py --host 0.0.0.0 --token "$LOVEWORK_DASHBOARD_TOKEN"
    python3 dashboard_server.py --host 0.0.0.0 --no-auth   # internal trial, home LAN
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import mimetypes
import os
from hermes_context import hermes_home_or_none, profile_name
import ipaddress
import re
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import doc_serve

import re as _re


# ── Dashboard CSS (module-level constant to avoid f-string parsing issues with --) ──

_DASHBOARD_CSS = """<style>
  :root { color-scheme: light dark; }
  @media (prefers-color-scheme: light) {
    :root { --bg: #FAFAF7; --fg: #1A1A1A; --accent: #1D4ED8; --line: #D4D4D0; --code-bg: #EEEEEA; --dim: #888; --h1: #1A1A1A; --h2: #1A1A1A; --navbar-bg: #fff; --live-bg: #fff; --live-border: #1D4ED8; --live-running: #2EA043; --go-bg: rgba(29, 78, 216, 0.06); --maybe-bg: rgba(187, 128, 9, 0.06); --flag-bg: rgba(248, 81, 73, 0.05); --hover-bg: rgba(29, 78, 216, 0.1); }
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0d1117; --fg: #c9d1d9; --accent: #58a6ff; --line: #21262d; --code-bg: #161b22; --dim: #6e7681; --h1: #f0883e; --h2: #58a6ff; --navbar-bg: #161b22; --live-bg: #161b22; --live-border: #f0883e; --live-running: #3fb950; --go-bg: rgba(46, 160, 67, 0.08); --maybe-bg: rgba(187, 128, 9, 0.06); --flag-bg: rgba(248, 81, 73, 0.05); --hover-bg: rgba(56, 139, 253, 0.1); }
  }
  body { font-family: -apple-system, system-ui, "Segoe UI", Roboto, monospace; margin: 0; padding: 1.5em; background: var(--bg); color: var(--fg); font-size: 14px; line-height: 1.5; }
  h1 { color: var(--h1); margin: 0 0 0.5em 0; font-size: 1.6em; }
  h2 { color: var(--h2); border-bottom: 1px solid var(--line); padding-bottom: 0.3em; margin: 1.5em 0 0.5em; font-size: 1.2em; }
  h3 { color: var(--h2); font-size: 1.05em; margin: 1em 0 0.4em; }
  code { background: var(--code-bg); padding: 1px 4px; border-radius: 3px; color: var(--fg); font-size: 0.92em; }
  pre { background: var(--code-bg); padding: 0.8em; border-radius: 6px; overflow-x: auto; color: var(--dim); font-size: 12px; line-height: 1.45; white-space: pre-wrap; }
  table { border-collapse: collapse; width: 100%; margin: 0.5em 0; font-size: 13px; }
  th, td { padding: 5px 10px; text-align: left; border-bottom: 1px solid var(--line); }
  th { background: var(--code-bg); color: var(--dim); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
  tr.go td { background: var(--go-bg); }
  tr.maybe td { background: var(--maybe-bg); }
  tr.flag td { background: var(--flag-bg); }
  tr:hover td { background: var(--hover-bg); }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .header { display: flex; justify-content: space-between; align-items: baseline; border-bottom: 2px solid var(--line); padding-bottom: 0.6em; margin-bottom: 1em; }
  .timestamp { color: var(--dim); font-size: 12px; }
  .navbar { background: var(--navbar-bg); padding: 10px 24px; border-bottom: 1px solid var(--line); display: flex; gap: 20px; font-size: 13px; align-items: center; }
  .navbar a { color: var(--dim); }
  .navbar a:hover { color: var(--accent); text-decoration: none; }
  .navbar .brand { color: var(--h1); font-weight: 600; margin-right: 8px; }
  .page { padding: 1.5em; }
  .live-run { background: var(--live-bg); border-left: 4px solid var(--live-border); padding: 0.8em 1em; border-radius: 6px; margin: 0.5em 0; }
  .live-run.running { border-left-color: var(--live-running); background: rgba(99, 185, 80, 0.05); }
  .live-run.idle { border-left-color: var(--dim); }
  .live-run-header { color: var(--live-border); font-weight: 600; margin-bottom: 0.4em; font-size: 13px; }
  .live-run.running .live-run-header { color: var(--live-running); }
  .live-run-tail { font-family: ui-monospace, "JetBrains Mono", monospace; font-size: 11px; line-height: 1.5; color: var(--dim); max-height: 220px; overflow-y: auto; }
  .logline { padding: 1px 0; white-space: pre-wrap; word-break: break-all; }
  .lastline { font-size: 11px; color: var(--dim); max-width: 500px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta-grid { display: grid; grid-template-columns: 200px 1fr; gap: 4px 12px; }
  .meta-grid dt { color: var(--dim); }
  .meta-grid dd { margin: 0; }
</style>"""


def _safe_org_name(name: str) -> str:
    """The wiki's own filename rule — one authority, not a copy of it.

    This used to carry a duplicate of `wiki_store._safe_filename`, which then
    drifted: when the wiki learned to transliterate, the copy kept folding
    non-ASCII to `_`, so the dashboard linked `_m_Labs.md` and `B_rse.md` where
    the pages are `Zum_Labs.md` and `Borse.md` — every accented org page link
    broken, silently.

    "Jack & Jill" → "Jack_Jill"
    "Isomorphic Labs" → "Isomorphic_Labs"
    "Zūm Labs" → "Zum_Labs"
    """
    from wiki_store import WikiStore
    return WikiStore._safe_filename(name or "x")


# ── Configuration ────────────────────────────────────────────────────────
def _resolve_lovework_root() -> Path:
    """Resolve the lovework repo root.

    Honor an explicit LOVEWORK_ROOT env var first; otherwise probe the
    engine checkout inside the workspace (host-b, host-a dev and host-a
    production).
    """
    env = os.environ.get("LOVEWORK_ROOT")
    if env:
        return Path(env)
    for cand in (
        # This module lives at <engine>/lovework-agent/, so the root is its
        # own parent — the candidate that survives the tree being moved.
        Path(__file__).resolve().parent.parent,
        Path.home() / "lovework" / "lovework-engine",
        Path("/srv/lovework/lovework-engine"),
        Path("/srv/lovework-dev/lovework-engine"),
    ):
        if (cand / "lovework-agent").is_dir():
            return cand
    # Last-resort fallback so the import never hard-fails.
    return Path(__file__).resolve().parent.parent


LOVEWORK_ROOT = _resolve_lovework_root()
LOVEWORK_DOCS_ROOT = LOVEWORK_ROOT  # docs/, profiles/, MANUAL.md etc.

HERMES_HOME = hermes_home_or_none()      # None post-severance (A5)

STATE_DIR = Path(os.getenv("LOVEWORK_STATE_DIR") or (LOVEWORK_ROOT / "state"))
WIKI = LOVEWORK_ROOT / "lovework-agent" / "wiki"
REPORTS = WIKI / "reports"
ORGS = WIKI / "orgs"
# Legacy dashboard helpers remain LJ-specific; all new and migrated logs live
# under principal state.
LOGS = STATE_DIR / "lj" / "logs"
PROFILES = LOVEWORK_ROOT / "profiles"
CACHE_DIR = LOVEWORK_ROOT / "lovework-agent" / "cache"

# Browser publication is deliberately narrower than the principal state tree.
# Caches, downloaded source pages, Gmail-derived data, profiles, and datasets
# remain local-only.  The three subtrees below are operator-facing artefacts:
# generated findings, wrapper logs, and compact run/incident records.
# Deliberately narrow: the operator-facing artefacts only. `cache` itself is
# NOT here — publishing it served the whole cache tree (the registry, downloaded
# source pages, enrichment, assessment caches), which contradicts this boundary
# and the note above. The two subtrees under it that *are* operator-facing are
# listed explicitly instead. `runs` and `incidents` are the only cache children
# the dashboard links to.
PUBLISHED_PRINCIPAL_AREAS = {
    "wiki": "wiki",
    "logs": "logs",
    "runs": "cache/runs",
    "incidents": "cache/incidents",
    # The fetch cache, so a report's "Cached advert" line can be opened. It is
    # NOT a directory publication: `cache/` also holds jobs.csv, the ledgers,
    # enrichment and downloaded source pages, so the area is narrowed by name
    # below (`AREA_FILE_ALLOW`) to the advert pages alone.
    "cache": "cache",
    # The case directories, so a GO's application pack is openable from the wiki
    # (LJ, 2026-10-09: "when applying, the principal should not start from Zero").
    # A pack is `application_pack.md` plus its dossier and tailored material, and
    # the markdown is rendered to HTML like the wiki. Note the boundary this does
    # NOT cross: LJ's *manual* cases are symlinks out of the state tree, and the
    # serving layer refuses a path that resolves outside its root — so they stay
    # 404 while the LoveWork packs are served.
    "applications": "applications",
    # …and the lead pages: every judgement (MAYBE, FLAG, DROP) keeps its evidence
    # in `state/<p>/leads/`, so the wiki's MAYBE listings can lead with it (LJ,
    # 2026-10-09: "all this should be in the Wiki, should not be forgotten").
    "leads": "leads",
    # The two closing piles (LJ's design, docs/45, built 2026-10-14): `gone/`
    # holds packs that were a GO and are now closed (the material is the value),
    # `expired/` holds judged adverts that closed. Both stay browsable at their
    # new path, and the old path carries a relative tombstone link.
    "gone": "gone",
    "expired": "expired",
    # The loop's own residue: `state/<p>/loop/drafts/` is what the tick wrote
    # and `receipts.jsonl` is what it claims it did. Without this area the
    # loop's product was invisible — a draft with no reader is work nobody can
    # use, and the receipt is the only record of what ran (docs/43 §7).
    "loop": "loop",
}

#: For an area that is a whole directory, the filenames that may be served.
#: Anything not matched is 404 — including a directory request, so no listing
#: of the cache is ever rendered. `page_<sha256(url)[:16]>.md` is the fetch
#: cache's own naming (`crawler._cache_path`), never a guess about content.
AREA_FILE_ALLOW: dict[str, re.Pattern] = {
    "cache": re.compile(r"^page_[0-9a-f]{16}\.md$"),
}


def principal_names() -> list[str]:
    """The principals with state on this host — `config.principal_names`, one authority.

    This used to accept any directory whose *name* looked like a principal's, so
    `state/idx/` (the AS1 situation cache) was listed as a fifth principal, **IDX**,
    with four links that 404 (LJ, 2026-10-15: *"Remove the IDX"*). A name shape is
    not a principal; a principal's directory holds a principal's material.
    """
    import config  # local: the dashboard sets its own STATE_DIR and must not
                   # depend on config's import order

    return config.principal_names(STATE_DIR)


def principal_area_root(principal: str, area: str) -> Path | None:
    """Map a public principal URL component to its intentionally public root."""
    if principal not in principal_names():
        return None
    relative = PUBLISHED_PRINCIPAL_AREAS.get(area)
    if relative is None:
        return None
    return STATE_DIR / principal / relative


def _find_jobs_db() -> Path:
    """Locate the active SQLite registry -- prefers jobs.db, falls back to jobs.db.N (rotation)."""
    primary = CACHE_DIR / "jobs.db"
    if primary.exists():
        return primary
    rotated = sorted(CACHE_DIR.glob("jobs.db.*"), key=lambda p: p.name, reverse=True)
    return rotated[0] if rotated else primary


JOBS_DB = _find_jobs_db()
JOBS_CSV = CACHE_DIR / "jobs.csv"
CONFIG = (HERMES_HOME / "config.yaml") if HERMES_HOME is not None else None

# ── Data fetchers ────────────────────────────────────────────────────────
def fetch_registry_stats() -> dict:
    """Read live job counts. Prefers jobs.csv (current source of truth); falls
    back to SQLite jobs.db (legacy) for installations that haven't migrated yet.
    """
    if JOBS_CSV.exists():
        try:
            import csv
            counts: dict[str, int] = {}
            with open(JOBS_CSV, encoding="utf-8", errors="ignore") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    status = row.get("status", "unknown")
                    counts[status] = counts.get(status, 0) + 1
            total = sum(counts.values())
            return {
                "source": "csv", "path": str(JOBS_CSV),
                "size_mb": round(JOBS_CSV.stat().st_size / 1024 / 1024, 1),
                "total": total, "stats": counts, "available": True,
            }
        except Exception as e:
            return {"available": False, "source": "csv", "error": str(e), "path": str(JOBS_CSV)}
    # Legacy SQLite fallback
    if not JOBS_DB.exists():
        return {"available": False, "path": str(JOBS_DB)}
    try:
        import sqlite3
        con = sqlite3.connect(str(JOBS_DB))
        cur = con.cursor()
        cur.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")
        rows = cur.fetchall()
        con.close()
        stats = {status: count for status, count in rows}
        return {
            "source": "sqlite", "available": True, "stats": stats,
            "total": sum(stats.values()), "path": str(JOBS_DB),
            "size_mb": round(JOBS_DB.stat().st_size / 1024 / 1024, 1),
        }
    except Exception as e:
        return {"available": False, "source": "sqlite", "error": str(e), "path": str(JOBS_DB)}


def fetch_top_jobs(limit: int = 20) -> list[dict]:
    """Read top GO/MAYBE listings from the most recent report file."""
    if not REPORTS.exists():
        return []
    reports = sorted(REPORTS.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not reports:
        return []
    latest = reports[0]
    text = latest.read_text(encoding="utf-8", errors="ignore")
    return _parse_report_top_listings(text, latest.name, limit)


def _parse_report_top_listings(text: str, source_file: str, limit: int) -> list[dict]:
    """Extract ### Org — Title entries + score, URL, location from a report."""
    out = []
    # Sections like "## neolabs", "## gmail_lj_jobs" then "### Org — Title"
    current_source = "(unknown)"
    for line in text.split("\n"):
        if line.startswith("## ") and not line.startswith("###"):
            current_source = line[3:].strip()
        elif line.startswith("### "):
            m = re.match(r"(.+?)\s*\u2014\s*(.+)", line[4:])
            if m:
                out.append({"org": m.group(1).strip(), "title": m.group(2).strip(),
                            "source": current_source, "source_file": source_file})
                if len(out) >= limit:
                    break
    # Now enrich with score/URL/location by re-scanning
    return _enrich_jobs(out, text)


def _extract_field(block: str, label: str):
    """Extract a markdown field value, e.g. '**Score**: 8.5/10' → '8.5/10'."""
    m = re.search(rf"\*\*{re.escape(label)}\*\*:\s*(.+)", block)
    return m.group(1).strip() if m else None


def _enrich_jobs(jobs: list[dict], text: str) -> list[dict]:
    """For each job header, look up the score/url/location below it."""
    enriched = []
    blocks = re.split(r"### ", text)
    for job in jobs:
        for block in blocks:
            if block.startswith(job["org"] + " \u2014 " + job["title"]):
                entry = dict(job)
                entry["score"] = _extract_field(block, "Score") or "—"
                entry["url"] = _extract_field(block, "URL") or ""
                entry["location"] = _extract_field(block, "Location") or ""
                entry["fit_score"] = _extract_field(block, "Fit")
                entry["reach_score"] = _extract_field(block, "Reach")
                entry["flourish_score"] = _extract_field(block, "Flourish")
                entry["combined_score"] = _extract_field(block, "Combined")
                entry["recommended_action"] = _extract_field(block, "Action")
                # Legacy decision from inline parens (old report format)
                dec_m = re.search(r"\(([A-Z]+)\)", block)
                entry["decision"] = dec_m.group(1) if dec_m else entry.get("recommended_action", "")
                enriched.append(entry)
                break
    return enriched


def fetch_runs() -> list[dict]:
    """List recent principal-owned crawl logs with metadata + last activity."""
    runs = []
    for principal in principal_names():
        log_dir = principal_area_root(principal, "logs")
        if log_dir is None or not log_dir.is_dir():
            continue
        for p in log_dir.glob("*.log"):
            st = p.stat()
            last_lines = []
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
                    last_lines = [ln.rstrip() for ln in lines[-3:] if ln.strip()]
            except OSError:
                pass
            kind = "incremental" if "incremental" in p.name else ("full" if "full" in p.name else "other")
            runs.append({
                "principal": principal,
                "name": p.name,
                "path": str(p),
                "rel_path": f"principals/{principal}/logs/{p.name}",
                "size_kb": round(st.st_size / 1024, 1),
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "kind": kind,
                "last_lines": last_lines,
                "line_count": sum(1 for _ in open(p, errors="ignore")) if p.exists() else 0,
            })
    return sorted(runs, key=lambda run: run["mtime"], reverse=True)[:20]


def fetch_live_run_progress() -> dict:
    """Check lock file and live log for a running crawl.

    Returns dict with running, started_at, age_seconds, crawl_type,
    log (filename), recent_lines (last 8 lines of activity).
    """
    lock = CACHE_DIR / "crawl.lock"
    lock_info = {}
    if lock.exists():
        try:
            for line in lock.read_text(encoding="utf-8", errors="ignore").strip().split("\n"):
                if "=" in line:
                    k, v = line.split("=", 1)
                    lock_info[k.strip()] = v.strip()
        except Exception:
            pass

    now = time.time()
    running = False
    started_at = ""
    age_seconds = 0
    crawl_type = ""
    recent_lines = []
    latest_log = ""

    # Check lock file first (immediate detection)
    if lock_info.get("status") == "running":
        try:
            start_epoch = int(lock_info.get("start_epoch", "0"))
            age_seconds = int(now - start_epoch)
            started_at = lock_info.get("start", "")
            crawl_type = lock_info.get("type", "")
            running = True
        except (ValueError, TypeError):
            pass

    # Read live log for activity stream
    if LOGS.exists():
        logs = sorted(LOGS.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
        if logs:
            latest = logs[0]
            latest_log = latest.name
            log_age = now - latest.stat().st_mtime
            try:
                text = latest.read_text(encoding="utf-8", errors="ignore")
                finished = any(marker in text for marker in [
                    "Report written to", "LoveWork Results", "Wiki:",
                    "===== LoveWork", "[CRAWL COMPLETE]",
                ])
                lines = [ln for ln in text.split("\n") if ln.strip()][-8:]
                recent_lines = lines
                # If new enough and not finished, it's active
                if log_age < 300 and not finished:
                    running = True
                # If lock says running but log says finished, use log
                if finished:
                    running = False
            except Exception:
                pass

    return {
        "running": running,
        "started_at": started_at,
        "age_seconds": age_seconds,
        "crawl_type": crawl_type,
        "log": latest_log,
        "recent_lines": recent_lines,
    }


def fetch_profiles() -> dict:
    """Profile metadata: name, soul.md exists, role files."""
    out = {}
    if not PROFILES.exists():
        return out
    for profile_dir in sorted(PROFILES.iterdir()):
        if not profile_dir.is_dir():
            continue
        soul = profile_dir / "soul.md"
        roles = sorted((profile_dir / "roles").glob("*.md")) if (profile_dir / "roles").is_dir() else []
        out[profile_dir.name] = {
            "soul_exists": soul.exists(),
            "soul_lines": sum(1 for _ in open(soul)) if soul.exists() else 0,
            "roles": [r.stem for r in roles],
        }
    return out


def fetch_entities() -> dict:
    """Org-page count + sample of most-recently-touched orgs."""
    if not ORGS.exists():
        return {"count": 0}
    pages = list(ORGS.glob("*.md"))
    by_mtime = sorted(pages, key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "count": len(pages),
        "top_recent": [
            {"name": p.stem.replace("_", " "), "path": p.name,
             "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
            for p in by_mtime[:15]
        ],
    }


def _app_dir_sort_key(p) -> tuple:
    """Sort by YYYYMMDD embedded in directory name, falling back to mtime.

    Conventions:
      "20260419-TestCo-Some_Role"  → (20260419, ...)  descending by date
      "random-dir-without-date"    → (0, mtime)        descending by mtime
    Dirs with dates sort newest-first; dirs without dates sort at the end.
    """
    name = p.name
    if len(name) >= 8 and name[:8].isdigit():
        return (int(name[:8]), name)
    return (0, p.stat().st_mtime, name)


def fetch_applications() -> dict:
    """Count + recent entries of the configured application corpus.

    Uses `config.APPLICATIONS_DIR` — the one authority every other reader
    (`history.scan_applications`, `cli_views`, the matchers) already uses. It
    used to read `LOVEWORK_ROOT / "applications"`, a path that only existed
    while a `<runtime-root>/applications` symlink pointed at LJ's manual corpus;
    when that symlink was dropped (2026-10-27) the dashboard silently showed
    zero cases while the rest of the engine still saw them.
    """
    import config  # local, same reason as principal_names(): one path authority

    apps_dir = config.APPLICATIONS_DIR
    if not apps_dir.is_dir():
        return {"count": 0, "available": False, "path": str(apps_dir)}
    entries = sorted([p for p in apps_dir.iterdir() if p.is_dir()],
                     key=_app_dir_sort_key, reverse=True)
    return {
        "count": len(entries),
        "available": True,
        "path": str(apps_dir),
        "recent": [p.name for p in entries[:10]],
    }


def fetch_sources() -> dict:
    """Read the canonical sources reference (wiki/sources.md)."""
    sources_md = WIKI / "sources.md"
    if not sources_md.exists():
        return {"raw": ""}
    return {"raw": sources_md.read_text(encoding="utf-8", errors="ignore"),
            "path": str(sources_md)}


def fetch_reports() -> list[dict]:
    """List past reports with size and a snippet of the headline (top 5 lines)."""
    out = []
    for principal in principal_names():
        reports_dir = principal_area_root(principal, "wiki")
        reports_dir = reports_dir / "reports" if reports_dir is not None else None
        if reports_dir is None or not reports_dir.is_dir():
            continue
        for p in reports_dir.glob("*.md"):
            text = p.read_text(encoding="utf-8", errors="ignore")
            first_line = text.split("\n", 1)[0] if text else ""
            # Pull decision counts line if present
            decisions = re.search(r"GO: \d+ \u00b7 MAYBE: \d+ \u00b7 FLAG: \d+ \u00b7 DROP: \d+", text)
            summary = re.search(r"Total entries\*: (\d+)", text)
            out.append({
                "principal": principal,
                "name": p.name,
                "path": str(p),
                "rel_path": f"principals/{principal}/wiki/reports/{p.name}",
                "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
                "title": first_line,
                "decisions": decisions.group(0) if decisions else None,
                "summary_count": summary.group(1) if summary else None,
            })
    return sorted(out, key=lambda report: report["mtime"], reverse=True)


def fetch_principal_workspaces() -> list[dict]:
    """Links to the narrowly published state for each principal."""
    workspaces = []
    for principal in principal_names():
        wiki = principal_area_root(principal, "wiki")
        logs = principal_area_root(principal, "logs")
        reports_dir = wiki / "reports" if wiki is not None else None
        latest = None
        if reports_dir is not None and reports_dir.is_dir():
            reports = sorted(reports_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            if reports:
                latest = reports[0]
        workspaces.append({
            "principal": principal,
            "wiki_url": f"/principals/{principal}/wiki/",
            "reports_url": f"/principals/{principal}/wiki/reports/",
            "logs_url": f"/principals/{principal}/logs/",
            "latest_report_name": latest.name if latest else None,
            "latest_report_url": f"/principals/{principal}/wiki/reports/{latest.name}" if latest else None,
            "has_logs": bool(logs and logs.is_dir()),
        })
    return workspaces


def _crontab_jobs() -> list[dict]:
    """Job rows from scripts/cron/crontab.in (the post-severance schedule)."""
    path = LOVEWORK_ROOT / "scripts" / "cron" / "crontab.in"
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" in line.split()[0]:
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        schedule = " ".join(parts[:5])
        command = " ".join(parts[5:]).split(">>")[0].strip()
        name = Path(command.split()[0]).name.removesuffix(".sh")
        args = command.split()[1:]
        # The row's identity is the launcher + its principal. Other arguments
        # (the tick's `--run-minutes` budget) are schedule detail, not a second
        # principal: `lovework-tick.sh lj 8` is the lj tick, not "lj-8".
        principal = args[0] if args and args[0] in ("lj", "vj", "kj", "pk", "all") else None
        rows.append({"name": name + (f"-{principal}" if principal else ""),
                     "schedule": schedule, "enabled": True,
                     "type": "script", "command": command})
    return rows


def fetch_cron_jobs() -> list[dict]:
    """The schedule: Hermes cron while it exists, else the checked-in crontab.

    Post-severance (A2/A3) the store of truth is `scripts/cron/crontab.in`;
    each lovework line is shown as a job row with the same keys the Hermes
    rows carried, so the dashboard card needs no second renderer.
    """
    if HERMES_HOME is None:
        return _crontab_jobs()
    jobs_path = HERMES_HOME / "cron" / "jobs.json"
    if not jobs_path.exists():
        # Try parent
        if HERMES_HOME.parent.exists():
            jobs_path = HERMES_HOME.parent / "cron" / "jobs.json"
        if not jobs_path.exists():
            return []
    try:
        data = json.loads(jobs_path.read_text())
    except Exception:
        return []
    return [{
        "name": j.get("name", "?"),
        "schedule": j.get("schedule", "?"),
        "next_run_at": j.get("next_run_at", ""),
        "state": j.get("state", "?"),
        "last_status": j.get("last_status"),
        "last_run_at": j.get("last_run_at"),
    } for j in data.get("jobs", [])]


def fetch_config_summary() -> dict:
    """Pull model/LLM config from the active profile's config.yaml."""
    if not CONFIG.exists():
        return {"available": False, "path": str(CONFIG)}
    try:
        import yaml
        data = yaml.safe_load(CONFIG.read_text())
    except Exception as e:
        return {"available": False, "error": str(e)}
    out = {"available": True, "path": str(CONFIG)}
    if isinstance(data, dict):
        m = data.get("model", {})
        if m:
            out["default_model"] = {
                "provider": m.get("provider"),
                "default": m.get("default"),
                "base_url": m.get("base_url"),
            }
        deleg = data.get("delegation", {})
        if deleg:
            out["delegation"] = {
                "model": deleg.get("model"),
                "provider": deleg.get("provider"),
                "base_url": deleg.get("base_url"),
            }
        fb = data.get("fallback_models", [])
        if fb:
            out["fallbacks"] = fb
        cp = data.get("custom_providers", [])
        if cp:
            out["custom_providers"] = [{"name": p.get("name"), "model": p.get("model"),
                                        "base_url": p.get("base_url")} for p in cp]
        brand = data.get("branding", {})
        if brand:
            out["branding"] = {
                "agent_name": brand.get("agent_name"),
                "welcome": brand.get("welcome"),
            }
        # Top-level tags useful at a glance
        for key in ("agent", "terminal", "memory", "approvals"):
            if key in data:
                v = data[key]
                if isinstance(v, dict):
                    out[key] = {k: v for k, v in v.items() if k in
                                  ("max_turns", "gateway_timeout", "backend", "cwd", "mode",
                                   "memory_enabled", "user_profile_enabled", "memory_char_limit")}
    return out


def fetch_system() -> dict:
    """System-level health: gateway state, processes, paths."""
    state_file = HERMES_HOME / "gateway_state.json"
    out = {
        "lovework_root": str(LOVEWORK_ROOT),
        "hermes_home": str(HERMES_HOME),
        "hermes_profile": profile_name(HERMES_HOME),
        "wiki_path": str(WIKI),
        "wiki_rel": "lovework-agent/wiki/",
        "jobs_csv": str(JOBS_CSV),
        "jobs_csv_rel": "lovework-agent/cache/jobs.csv",
        "config_path": str(CONFIG),
    }
    if state_file.exists():
        try:
            gs = json.loads(state_file.read_text())
            out["gateway_state"] = gs.get("gateway_state", "?")
            out["gateway_pid"] = gs.get("pid")
            out["platforms"] = {k: v.get("state") for k, v in gs.get("platforms", {}).items()}
        except Exception as e:
            out["gateway_error"] = str(e)
    # PID check. A process listing is a convenience, not the page: under a
    # sandbox (or a minimal container) `ps` answers EPERM, and an unguarded call
    # took the whole dashboard down with "Error rendering dashboard" (2026-11-07).
    # No answer is "unknown", never a crash and never a confident False.
    import subprocess
    out["hermes_gateway_running"] = None
    try:
        r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=10)
        out["hermes_gateway_running"] = "hermes_cli.main gateway" in r.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        # Not a silent swallow: the reason travels with the data, and the page
        # says "unknown" rather than guessing.
        out["ps_error"] = str(exc)[:200]
    return out


# ── HTML renderer ────────────────────────────────────────────────────────
def render_html() -> str:
    reg = fetch_registry_stats()
    jobs = fetch_top_jobs(limit=20)
    runs = fetch_runs()
    progress = fetch_live_run_progress()
    profiles = fetch_profiles()
    entities = fetch_entities()
    apps = fetch_applications()
    sources = fetch_sources()
    reports = fetch_reports()
    workspaces = fetch_principal_workspaces()
    cron = fetch_cron_jobs()
    cfg = fetch_config_summary()
    sysinfo = fetch_system()

    return HTML_TEMPLATE.format(
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        reg=reg,
        jobs=jobs,
        runs=runs,
        progress=progress,
        profiles=profiles,
        entities=entities,
        apps=apps,
        sources=sources,
        reports=reports,
        cron=cron,
        cfg=cfg,
        sysinfo=sysinfo,
    )


def _fmt_section_profiles(profiles: dict) -> str:
    rows = []
    for name, p in profiles.items():
        soul = "✓" if p["soul_exists"] else "—"
        roles = ", ".join(p["roles"]) if p["roles"] else "(none)"
        rows.append(f"<tr><td><b>{name}</b></td><td>{soul}</td><td>{p['soul_lines']}</td><td>{roles}</td></tr>")
    return "\n".join(rows) or "<tr><td colspan=4>No profiles</td></tr>"


def _fmt_section_entities(entities: dict) -> str:
    if not entities.get("count"):
        return "<p>No org pages yet.</p>"
    rows = []
    for o in entities.get("top_recent", []):
        rows.append(f"<tr><td><a href='/lovework-agent/wiki/orgs/{o['path']}'>{o['name']}</a></td><td>{o['mtime']}</td></tr>")
    table = "\n".join(rows)
    return f"<p><b>{entities['count']}</b> org pages total. Most recently updated:</p><table>{table}</table>"


def _fmt_jobs_tooltip(entry: dict) -> str:
    """Build a hover tooltip showing multi-axis scores when available."""
    parts = []
    fs = entry.get("fit_score")
    rs = entry.get("reach_score")
    ls = entry.get("flourish_score")
    cs = entry.get("combined_score")
    ra = entry.get("recommended_action")
    if fs or rs or ls:
        parts.append(f"Fit={fs} Reach={rs} Flourish={ls}")
    if cs:
        parts.append(f"Combined={cs}")
    if ra:
        parts.append(f"Action={ra}")
    return " | ".join(parts) if parts else ""


def _fmt_section_jobs(jobs: list[dict]) -> str:
    if not jobs:
        return "<p>No jobs in the latest report.</p>"
    rows = []
    for j in jobs[:20]:
        org = j.get("org", "")
        title = j.get("title", "")
        url = j.get("url", "")
        loc = j.get("location", "")
        safe_org = _safe_org_name(org)
        title_link = f"<a href='{url}'>{title}</a>" if url else f"<a href='/lovework-agent/wiki/orgs/{safe_org}.md'>{title}</a>"
        org_link = f"<a href='/lovework-agent/wiki/orgs/{safe_org}.md'>{org}</a>"
        decision = j.get("recommended_action", j.get("decision", ""))
        score = j.get("combined_score", j.get("score", "—"))
        decision_class = decision.lower() if decision else "maybe"
        tooltip = _fmt_jobs_tooltip(j)
        rows.append(f"<tr class='{decision_class}' title='{tooltip}'>"
                    f"<td>{score}</td><td><b>{decision}</b></td>"
                    f"<td>{org_link}</td><td>{title_link}</td><td>{loc}</td><td>{j.get('source','')}</td></tr>")
    return f"<table class='jobs'><thead><tr><th>Score</th><th>Decision</th><th>Org</th><th>Title</th><th>Location</th><th>Source</th></tr></thead><tbody>{chr(10).join(rows)}</tbody></table>"


def _fmt_section_runs(runs: list[dict], progress: dict) -> str:
    if not runs:
        return "<p>No log files.</p>"
    # Live run progress card
    progress_html = ""
    if progress.get("running") is not False:
        live = progress.get("log", "")
        age = progress.get("age_seconds", 0)
        started = progress.get("started_at", "")
        ctype = progress.get("crawl_type", "")
        if progress.get("running"):
            if age < 120:
                dur = f"{age}s"
            else:
                dur = f"{age // 60}m {age % 60}s"
            status = f"RUNNING ({dur})"
            color = "running"
        else:
            status = f"IDLE ({age}s since last)"
            color = "idle"
        recent = "\n".join(f"<div class='logline'>{ln}</div>" for ln in progress.get("recent_lines", []))
        info = f"<code>{ctype}</code> started {started}" if started else f"<code>{live or 'no log'}</code>"
        progress_html = f"""<div class='live-run {color}'>
<div class='live-run-header'>● {status} · {info}</div>
<div class='live-run-tail'>{recent}</div>
</div>"""
    rows = []
    for r in runs:
        size = r.get("size_kb", 0)
        kind = r.get("kind", "other")
        last = " | ".join(r.get("last_lines", []))[:120]
        rows.append(f"<tr><td><code>{r['name']}</code></td><td>{kind}</td>"
                    f"<td>{size}KB</td><td>{r['line_count']}</td><td>{r['mtime']}</td>"
                    f"<td class='lastline'>{last}</td></tr>")
    table = f"<table><thead><tr><th>Log file</th><th>Kind</th><th>Size</th><th>Lines</th><th>Last modified</th><th>Last lines</th></tr></thead><tbody>{chr(10).join(rows)}</tbody></table>"
    return progress_html + table


def _fmt_section_reports(reports: list[dict]) -> str:
    if not reports:
        return "<p>No reports yet.</p>"
    rows = []
    for r in reports:
        decision = r.get("decisions") or "—"
        count = r.get("summary_count") or "—"
        rows.append(f"<tr><td>{r['principal'].upper()}</td><td><a href='/{r['rel_path']}'>{r['name']}</a></td>"
                    f"<td>{r['mtime']}</td><td>{decision}</td><td>{count}</td></tr>")
    return f"<table><thead><tr><th>Principal</th><th>Report</th><th>Modified</th><th>Decisions</th><th>Total</th></tr></thead><tbody>{chr(10).join(rows)}</tbody></table>"


def _fmt_section_principal_workspaces(workspaces: list[dict]) -> str:
    if not workspaces:
        return "<p>No principal state has been created yet.</p>"
    rows = []
    for workspace in workspaces:
        principal = workspace["principal"].upper()
        latest = workspace.get("latest_report_name")
        latest_html = (
            f"<a href='{workspace['latest_report_url']}'>{latest}</a>"
            if latest else "—"
        )
        rows.append(
            f"<tr><td><b>{principal}</b></td>"
            f"<td><a href='{workspace['wiki_url']}'>wiki</a> · "
            f"<a href='{workspace['reports_url']}'>reports</a> · "
            f"<a href='{workspace['logs_url']}'>logs</a></td>"
            f"<td>{latest_html}</td></tr>"
        )
    return (
        "<p>Published principal artefacts only: generated wiki/results, crawl logs, "
        "and run records. Source caches, profiles, Gmail data, and datasets stay local.</p>"
        "<table><thead><tr><th>Principal</th><th>Browse</th><th>Latest report</th></tr></thead>"
        f"<tbody>{chr(10).join(rows)}</tbody></table>"
    )


def _fmt_section_cron(cron: list[dict]) -> str:
    if not cron:
        return "<p>No scheduled jobs found.</p>"
    rows = []
    for j in cron:
        last = j.get("last_run_at") or "—"
        status = j.get("last_status") or j.get("state", "—")
        rows.append(f"<tr><td><code>{j['name']}</code></td><td><code>{j['schedule']}</code></td>"
                    f"<td>{j.get('next_run_at','')}</td><td>{last}</td><td>{status}</td></tr>")
    return f"<table><thead><tr><th>Job</th><th>Schedule</th><th>Next run</th><th>Last run</th><th>Status</th></tr></thead><tbody>{chr(10).join(rows)}</tbody></table>"


def _fmt_section_sources(sources: dict) -> str:
    return f"<pre>{sources.get('raw','')}</pre>"


def _fmt_section_config(cfg: dict) -> str:
    if not cfg.get("available"):
        return f"<p>Config not available: {cfg.get('error','')}</p>"
    parts = []
    dm = cfg.get("default_model")
    if dm:
        parts.append(f"<p><b>Default model:</b> {dm.get('default')} via {dm.get('provider')} ({dm.get('base_url')})</p>")
    d = cfg.get("delegation")
    if d:
        parts.append(f"<p><b>Delegation (subagents):</b> {d.get('model')} via {d.get('provider')}</p>")
    fb = cfg.get("fallbacks")
    if fb:
        items = ", ".join(f"{f['model']} via {f['provider']}" for f in fb)
        parts.append(f"<p><b>Fallbacks:</b> {items}</p>")
    cp = cfg.get("custom_providers")
    if cp:
        rows = "".join(f"<tr><td><code>{p['name']}</code></td><td><code>{p['model']}</code></td><td>{p.get('base_url','')}</td></tr>" for p in cp)
        parts.append(f"<p><b>Custom providers:</b></p><table><thead><tr><th>Name</th><th>Default model</th><th>Base URL</th></tr></thead><tbody>{rows}</tbody></table>")
    br = cfg.get("branding")
    if br:
        parts.append(f"<p><b>Agent:</b> {br.get('agent_name')} — <i>{br.get('welcome')}</i></p>")
    return "\n".join(parts) or "<p>No model config found.</p>"


def _fmt_section_applications(apps: dict) -> str:
    if not apps.get("count"):
        return "<p>No applications yet.</p>"
    lines = [f"<p><b>{apps['count']}</b> applications on file at <code>{apps['path']}</code></p>",
             "<table><thead><tr><th>Most recent</th></tr></thead><tbody>"]
    for name in apps.get("recent", []):
        lines.append(f"<tr><td>{name}</td></tr>")
    lines.append("</tbody></table>")
    lines.append("<p>Application files are intentionally not published on the LAN dashboard.</p>")
    return "\n".join(lines)


def _fmt_section_system(s: dict) -> str:
    gw_state = s.get("gateway_state", "?")
    gw_running = s.get("hermes_gateway_running")
    gw_running = "unknown" if gw_running is None else gw_running
    platforms = s.get("platforms", {})
    plat_str = ", ".join(f"{k}={v}" for k, v in platforms.items()) or "—"
    return f"""<p><b>LoveWork root:</b> <code><a href='/README.md'>{s.get('lovework_root')}</a></code></p>
<p><b>Hermes home:</b> <code><a href='/{s.get('hermes_rel','')}'>{s.get('hermes_home')}</a></code></p>
<p><b>Wiki:</b> <code><a href='/{s.get('wiki_rel','')}'>{s.get('wiki_path')}</a></code></p>
<p><b>Jobs CSV:</b> <code>{s.get('jobs_csv')}</code> (not LAN-published)</p>
<p><b>Config:</b> <code>{s.get('config_path')}</code></p>
<p><b>Gateway state:</b> {gw_state} (running={gw_running})</p>
<p><b>Platforms:</b> {plat_str}</p>"""


#: The two directories an organisation page may live in — the kind a page records
#: decides which (LJ asked for the separation, 2026-10-15).
ORG_DIRS = ("orgs", "agencies")


def _sibling_org_path(root: Path, relative: str) -> str | None:
    """The same page in the other directory, when it moved between them.

    `orgs/X.md` → `agencies/X.md` and back. The area, the principal and the
    publication boundary are unchanged — only the directory differs — so this cannot
    widen what is served.
    """
    head, _, tail = relative.partition("/")
    if head not in ORG_DIRS or not tail:
        return None
    other = ORG_DIRS[0] if head == ORG_DIRS[1] else ORG_DIRS[1]
    if (root / other / tail).is_file():
        return f"{other}/{tail}"
    return None


def _serve_principal_public_path(path: str, query: str) -> dict | None:
    """Serve only an allowlisted principal-result subtree.

    Public URLs deliberately do not mirror ``state/``: it also holds private
    profile-derived caches and downloaded source material.
    """
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 3 or parts[0] != "principals":
        return None
    principal, area = parts[1], parts[2]
    root = principal_area_root(principal, area)
    if root is None:
        return None
    relative = "/".join(parts[3:]) or "/"
    allow = AREA_FILE_ALLOW.get(area)
    if allow is not None:
        name = relative.rstrip("/")
        if "/" in name or not allow.match(name):
            return None          # not an advert page: 404, and no listing

    # An **old path** may now live in another area of the *same* principal: the
    # closing piles move a directory into `gone/` or `expired/` and record the move
    # (docs/45), so a wiki GO row or a lead page written before the retirement must
    # still answer. The boundary is unchanged — only a published area of this same
    # principal is reachable, and the map is the only way out of this area.
    resolved_root = root
    if not (root / relative).exists():
        try:
            import retire

            moves = retire.move_map(STATE_DIR / principal)
        except Exception:
            moves = {}
        # The map records *directories*, and a request names a file inside one:
        # walk up the path until a move matches, then keep the tail
        # (`<pack>/application_pack.md` → the pack moved → the file moved with it).
        moved, remainder = None, ""
        pieces = [p for p in relative.split("/") if p]
        for cut in range(len(pieces), 0, -1):
            found = moves.get(f"{area}/" + "/".join(pieces[:cut]))
            if found:
                moved, remainder = found, "/".join(pieces[cut:])
                break
        if not moved:
            sibling = _sibling_org_path(root, relative)
            if sibling is not None:
                # The page changed directory, not identity: a link written before
                # the separation still answers (2026-10-15).
                return doc_serve.try_serve_path(sibling, query, root,
                                                request_path=path)
            return doc_serve.try_serve_path(relative, query, root, request_path=path)
        for other_area, other_rel in PUBLISHED_PRINCIPAL_AREAS.items():
            if moved == other_rel or moved.startswith(other_rel + "/"):
                tail = moved[len(other_rel):].lstrip("/")
                relative = "/".join(p for p in (tail, remainder) if p)
                resolved_root = STATE_DIR / principal / other_rel
                break
        else:
            return None          # moved outside every published area: 404 by design
    return doc_serve.try_serve_path(relative, query, resolved_root, request_path=path)


# ── The documentation site (docs/html) ────────────────────────────────────
#
# docs/html is the docsify site root: one index.html plus a farm of *relative*
# symlinks — chapter files as `../X.md`, repo-root files as `../../X`. The farm is
# what keeps the site location-independent, and it is also why this subtree is
# served as plain static files with a *resolved-path* allow-list, not through the
# server-rendered markdown used for /docs/*.md:
#   * docsify fetches `<route>.md` and needs the raw markdown, not rendered HTML;
#   * two of the farm's links reach material that must never be LAN-published —
#     README.LJ (LJ's turn log) and profiles/<principal>/.
DOCS_SITE_REL = "docs/html"
DOCS_DENY_TOP = {"README.LJ", "state", "applications", "credentials", "logBook"}
DOCS_DENY_PREFIXES = (".", "~")


def _docs_path_allowed(fs_path: Path) -> bool:
    """Publish a docs-site file only when its *resolved* location is publishable.

    Resolution is the point: the farm's symlinks and any `..` segments are
    collapsed first, so `/docs/html/README.LJ` and
    `/docs/html/profiles/example/soul.md` cannot smuggle private material out.
    """
    try:
        rel = fs_path.resolve().relative_to(LOVEWORK_DOCS_ROOT.resolve())
    except (OSError, ValueError):
        return False
    parts = rel.parts
    if not parts or parts[0] in DOCS_DENY_TOP or parts[0].startswith(DOCS_DENY_PREFIXES):
        return False
    if parts[0] == "profiles" and not (len(parts) > 1 and parts[1] == "example"):
        return False
    return True


def _serve_docs_site(rel: str) -> dict | None:
    """Serve the docsify site root as raw static files (mime by extension)."""
    fs_path = doc_serve._resolve_scoped_path(LOVEWORK_DOCS_ROOT, rel)
    if fs_path is None:
        return None
    if fs_path.is_dir():
        fs_path = fs_path / "index.html"
    if not fs_path.is_file() or not _docs_path_allowed(fs_path):
        return None
    try:
        data = fs_path.read_bytes()
    except OSError:
        return None
    ctype, _ = mimetypes.guess_type(str(fs_path))
    if fs_path.suffix == ".md":
        ctype = "text/plain; charset=utf-8"          # docsify parses it as text
    elif ctype is None:
        ctype = "application/octet-stream"
    return {"data": data, "mime": ctype}


def _serve_general_public_path(path: str, query: str) -> dict | None:
    """Serve the small, intentionally public documentation surface."""
    rel = path.strip("/")
    if rel == DOCS_SITE_REL or rel.startswith(DOCS_SITE_REL + "/"):
        return _serve_docs_site(rel)
    # MANUAL.md is written into the state dir (2026-11-03) — serve it from there so
    # there is exactly one write target and the code checkout stays clean.
    if rel == "MANUAL.md":
        return doc_serve.try_serve_path(path, query, STATE_DIR)
    allowed_top_level = {"README.md", "MANUAL.md", "LICENSE"}
    allowed_prefixes = ("docs", "lovework-agent/wiki")
    if rel in allowed_top_level or any(rel == prefix or rel.startswith(prefix + "/") for prefix in allowed_prefixes):
        return doc_serve.try_serve_path(path, query, LOVEWORK_DOCS_ROOT)
    return None


def _fmt_section_registry(reg: dict) -> str:
    if not reg.get("available"):
        return f"<p>Registry DB not available: {reg.get('error','not found')}</p>"
    stats = reg.get("stats", {})
    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in sorted(stats.items()))
    return f"<p><b>Total jobs:</b> {reg.get('total',0)} (<b>{reg.get('size_mb',0)}MB</b> on disk)</p><table><thead><tr><th>Lifecycle</th><th>Count</th></tr></thead><tbody>{rows}</tbody></table>"


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>LoveWork Dashboard</title>
<meta http-equiv="refresh" content="30">
<style>
  body {{ font-family: -apple-system, system-ui, "Segoe UI", Roboto, monospace; margin: 0; padding: 1.5em; background: #0d1117; color: #c9d1d9; font-size: 14px; line-height: 1.5; }}
  h1 {{ color: #f0883e; margin: 0 0 0.5em 0; font-size: 1.6em; }}
  h2 {{ color: #58a6ff; border-bottom: 1px solid #21262d; padding-bottom: 0.3em; margin: 1.5em 0 0.5em; font-size: 1.2em; }}
  h3 {{ color: #d2a8ff; font-size: 1.05em; margin: 1em 0 0.4em; }}
  code {{ background: #161b22; padding: 1px 4px; border-radius: 3px; color: #e6edf3; font-size: 0.92em; }}
  pre {{ background: #161b22; padding: 0.8em; border-radius: 6px; overflow-x: auto; color: #8b949e; font-size: 12px; line-height: 1.45; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0.5em 0; font-size: 13px; }}
  th, td {{ padding: 5px 10px; text-align: left; border-bottom: 1px solid #21262d; }}
  th {{ background: #161b22; color: #8b949e; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }}
  tr.go td {{ background: rgba(46, 160, 67, 0.08); }}
  tr.maybe td {{ background: rgba(187, 128, 9, 0.06); }}
  tr.flag td {{ background: rgba(248, 81, 73, 0.05); }}
  tr:hover td {{ background: rgba(56, 139, 253, 0.1); }}
  a {{ color: #58a6ff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .header {{ display: flex; justify-content: space-between; align-items: baseline; border-bottom: 2px solid #21262d; padding-bottom: 0.6em; margin-bottom: 1em; }}
  .timestamp {{ color: #6e7681; font-size: 12px; }}
  .live-run {{ background: #161b22; border-left: 4px solid #f0883e; padding: 0.8em 1em; border-radius: 6px; margin: 0.5em 0; }}
  .live-run.running {{ border-left-color: #3fb950; background: rgba(63, 185, 80, 0.05); }}
  .live-run.idle {{ border-left-color: #6e7681; }}
  .live-run-header {{ color: #f0883e; font-weight: 600; margin-bottom: 0.4em; font-size: 13px; }}
  .live-run.running .live-run-header {{ color: #3fb950; }}
  .live-run-tail {{ font-family: ui-monospace, "JetBrains Mono", "Cascadia Code", monospace; font-size: 11px; line-height: 1.5; color: #8b949e; max-height: 200px; overflow-y: auto; }}
  .logline {{ padding: 1px 0; white-space: pre-wrap; word-break: break-all; }}
  .lastline {{ font-size: 11px; color: #6e7681; max-width: 500px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .status-ok {{ color: #3fb950; }}
  .status-warn {{ color: #d29922; }}
  .status-err {{ color: #f85149; }}
  .meta-grid {{ display: grid; grid-template-columns: 200px 1fr; gap: 4px 12px; }}
  .meta-grid dt {{ color: #6e7681; }}
  .meta-grid dd {{ margin: 0; }}
</style>
</head>
<body>
<div class="header">
  <h1>◆ LoveWork Dashboard</h1>
  <div class="timestamp">refreshed {now} · auto-refresh 30s</div>
</div>

<h2>▣ System</h2>
{_section_system}

<h2>▣ Registry (live jobs.db)</h2>
{_section_registry}

<h2>◆ Live Run Progress</h2>
{_section_runs}

<h2>◆ Recent Runs (logs)</h2>
{_section_runs_table}

<h2>◆ Cron Schedule</h2>
{_section_cron}

<h2>◆ Config (active profile)</h2>
{_section_config}

<h2>◆ Profiles</h2>
<table><thead><tr><th>Profile</th><th>soul.md</th><th>Lines</th><th>Roles</th></tr></thead>
<tbody>{_section_profiles}</tbody></table>

<h2>◆ Applications (LJ submissions)</h2>
{_section_applications}

<h2>◆ Entities (orgs originating jobs)</h2>
{_section_entities}

<h2>◆ Sources (where leads come from)</h2>
{_section_sources}

<h2>◆ Reports (past runs)</h2>
{_section_reports}

<h2>◆ Top Jobs (latest report)</h2>
{_section_jobs}

<p style="color:#6e7681; margin-top: 3em; font-size: 12px; text-align: center;">
  LoveWork Dashboard · renders live from wiki/, logs/, jobs.db, profiles/ at request time
</p>
</body>
</html>
"""


# Re-format the template to use a function-based approach so we can compute
# each section. The format string above is just the layout.
def render_html() -> str:
    reg = fetch_registry_stats()
    jobs = fetch_top_jobs(limit=20)
    runs = fetch_runs()
    progress = fetch_live_run_progress()
    profiles = fetch_profiles()
    entities = fetch_entities()
    apps = fetch_applications()
    sources = fetch_sources()
    reports = fetch_reports()
    workspaces = fetch_principal_workspaces()
    cron = fetch_cron_jobs()
    cfg = fetch_config_summary()
    sysinfo = fetch_system()

    # For sections that have both a card and a table (runs), split
    runs_table = []
    progress_html = ""
    if progress:
        live = progress.get("log", "")
        age = progress.get("age_seconds", 0)
        started = progress.get("started_at", "")
        ctype = progress.get("crawl_type", "")
        if progress.get("running"):
            if age < 120:
                dur = f"{age}s"
            else:
                dur = f"{age // 60}m {age % 60}s"
            status = f"RUNNING ({dur})"
            color = "running"
        elif progress.get("log"):
            status = f"IDLE ({age}s since last log update)"
            color = "idle"
        else:
            color = "idle"
            status = "—"
        recent_lines = progress.get("recent_lines", [])
        if recent_lines:
            recent = "\n".join(f"<div class='logline'>{ln}</div>" for ln in recent_lines)
            info = f"<code>{ctype}</code> started {started}" if started else f"<code>{live or 'no log'}</code>"
            progress_html = f"""<div class='live-run {color}'>
<div class='live-run-header'>● {status} · {info}</div>
<div class='live-run-tail'>{recent}</div>
</div>"""
        else:
            progress_html = f"<div class='live-run idle'><div class='live-run-header'>● {status} · <code>{live or 'no log'}</code></div></div>"

    runs_rows = []
    for r in runs:
        last = " | ".join(r.get("last_lines", []))[:120]
        runs_rows.append(
            f"<tr><td>{r.get('principal', '').upper()}</td><td><code><a href='/{r['rel_path']}'>{r['name']}</a></code></td><td>{r['kind']}</td>"
            f"<td>{r['size_kb']}KB</td><td>{r['line_count']}</td><td>{r['mtime']}</td>"
            f"<td class='lastline'>{last}</td></tr>"
        )
    runs_table_html = f"<table><thead><tr><th>Principal</th><th>Log file</th><th>Kind</th><th>Size</th><th>Lines</th><th>Modified</th><th>Last lines</th></tr></thead><tbody>{chr(10).join(runs_rows)}</tbody></table>" if runs_rows else "<p>No log files.</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>LoveWork Dashboard</title>
<meta http-equiv="refresh" content="30">
{_DASHBOARD_CSS}
</head>
<body>
<div class="navbar">
  <span class="brand">LoveWork</span>
  <a href="/">Home</a>
  <a href="/dashboard/">Dashboard</a>
  <a href="/principals/lj/wiki/reports/">LJ results</a>
  <a href="/principals/vj/wiki/reports/">VJ results</a>
  <a href="/docs/">📖 Docs</a>
  <a href="/MANUAL.md">📋 Manual</a>
  <a href="/README.md">ℹ️ About</a>
  <a href="/docs/10-ecosystem-survey.md">🔍 Survey</a>
</div>
<div class="page">
<div class="header">
  <h1>◆ LoveWork Dashboard</h1>
  <div class="timestamp">refreshed {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} · auto-refresh 30s</div>
</div>

<h2>⚙ System</h2>
{_fmt_section_system(sysinfo)}

<h2>◎ Registry (live jobs.csv)</h2>
{_fmt_section_registry(reg)}

<h2>◫ Principal Results</h2>
{_fmt_section_principal_workspaces(workspaces)}

<h2>● Live Run Progress</h2>
{progress_html}

<h2>◆ Recent Runs (logs)</h2>
{runs_table_html}

<h2>▣ Cron Schedule</h2>
{_fmt_section_cron(cron)}

<h2>⚙ Config (active profile)</h2>
{_fmt_section_config(cfg)}

<h2>◉ Profiles</h2>
<table><thead><tr><th>Profile</th><th>soul.md</th><th>Lines</th><th>Roles</th></tr></thead>
<tbody>{_fmt_section_profiles(profiles)}</tbody></table>

<h2>◆ Applications (LJ submissions)</h2>
{_fmt_section_applications(apps)}

<h2><a href="/lovework-agent/wiki/orgs/" style="color: inherit; text-decoration: none;">◉ Entities (orgs originating jobs)</a></h2>
{_fmt_section_entities(entities)}
<p><a href="/lovework-agent/wiki/orgs/">📂 Browse all entities →</a></p>

<h2><a href="/lovework-agent/wiki/sources.md" style="color: inherit; text-decoration: none;">◎ Sources (where leads come from)</a></h2>
<details><summary>Click to expand</summary>
{_fmt_section_sources(sources)}
</details>

<h2>◆ Reports (past runs)</h2>
{_fmt_section_reports(reports)}

<h2>★ Top Jobs (latest report)</h2>
{_fmt_section_jobs(jobs)}

<p style="color:var(--dim); margin-top: 3em; font-size: 12px; text-align: center;">
  LoveWork Dashboard · renders live from wiki/, logs/, jobs.db, profiles/ at request time
</p>
</div>
</div>
</body>
</html>
"""


def render_lan_index() -> str:
    """Render the stable, human-facing front door for LoveWork on the LAN."""
    cards = []
    for workspace in fetch_principal_workspaces():
        principal = workspace["principal"].upper()
        latest = workspace.get("latest_report_name")
        latest_link = (
            f"<p>Latest: <a href='{workspace['latest_report_url']}'>{latest}</a></p>"
            if latest else "<p>No report yet.</p>"
        )
        cards.append(
            f"<section class='principal-card'><h3>{principal}</h3>"
            f"<p><a href='{workspace['wiki_url']}'>Wiki</a> · "
            f"<a href='{workspace['reports_url']}'>Reports</a> · "
            f"<a href='{workspace['logs_url']}'>Logs</a> · "
            f"<a href='/principals/{workspace['principal']}/runs/'>Run records</a> · "
            f"<a href='/principals/{workspace['principal']}/incidents/'>Incidents</a></p>"
            f"{latest_link}</section>"
        )
    principal_html = "\n".join(cards) or "<p>No principal state has been created yet.</p>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LoveWork — Local LAN</title>
{_DASHBOARD_CSS}
<style>
  .index {{ max-width: 960px; margin: 0 auto; }}
  .intro {{ color: var(--dim); max-width: 760px; }}
  .principal-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }}
  .principal-card {{ background: var(--live-bg); border: 1px solid var(--line); border-radius: 7px; padding: 14px; }}
  .principal-card h3 {{ margin-top: 0; }}
  .links {{ display: flex; flex-wrap: wrap; gap: 8px 18px; }}
</style>
</head>
<body>
<main class="page index">
  <div class="header">
    <h1>◆ LoveWork</h1>
    <div class="timestamp">Local LAN index · {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
  </div>
  <p class="intro">One starting point for LoveWork’s published local results, operations, and documentation.</p>

  <h2>Principal results</h2>
  <div class="principal-grid">
    {principal_html}
  </div>

  <h2>Operations</h2>
  <p class="links">
    <a href="/dashboard/">Live dashboard</a>
    <a href="/usage/">Model usage — tokens per provider and model</a>
    <a href="/health">Health</a>
    <a href="/api/registry">Registry API</a>
    <a href="/api/progress">Live-progress API</a>
  </p>

  <h2>Project information</h2>
  <p class="links">
    <a href="/README.md">About LoveWork</a>
    <a href="/MANUAL.md">LJ operator manual</a>
    <a href="/docs/">Documentation — all chapters</a>
    <a href="/docs/00-index.md">Documentation index page</a>
    <a href="/docs/02-architecture.md">Architecture</a>
    <a href="/docs/09-intelligence-layer.md">Intelligence layer</a>
  </p>

  <h2>Publication boundary</h2>
  <p class="intro">Only generated principal wiki/results, logs, run records, and incident records are published here. Profiles, applications, source caches, Gmail-derived data, and datasets remain local-only.</p>
</main>
</body>
</html>"""


# ── Authentication ───────────────────────────────────────────────────────
#
# The dashboard (and its MCP face on the same process) reads every
# principal's wiki, reports and logs, and MCP exposes run_python, so the
# server is loopback-only by default and requires a shared token whenever it
# is bound wider. Token sources, in order:
#   1. Authorization: Bearer <token>   — API / MCP clients
#   2. ?token=<token>                  — curl convenience and the first
#                                        browser load; also sets the cookie
#   3. Cookie lovework_token=<token>   — all later browser requests
# The token is never logged and never echoed back in any response body.
#
# --no-auth (or LOVEWORK_DASHBOARD_NO_AUTH=1) disables authentication for the
# process. It is the explicit operator consent that bypasses the non-loopback
# refusal below, for the internal R&D trial on LJ's trusted home LAN
# (lj, pk, vj, kj). It is logged as a warning at startup and must not be used
# for any host reachable beyond that LAN.

AUTH_COOKIE_NAME = "lovework_token"
AUTH_TOKEN: str | None = None

# Redacts ?token=... from the request line before it reaches the log.
_TOKEN_IN_QUERY = re.compile(r"(?i)([?&]token=)[^&\s\"']+")


def configure_auth(token: str | None) -> None:
    """Set the process-wide dashboard token. Empty/None disables auth."""
    global AUTH_TOKEN
    AUTH_TOKEN = token or None


def resolve_token(cli_value: str | None = None) -> str | None:
    """Resolve the token: --token first, then LOVEWORK_DASHBOARD_TOKEN."""
    return cli_value or os.environ.get("LOVEWORK_DASHBOARD_TOKEN") or None


NO_AUTH_ENV = "LOVEWORK_DASHBOARD_NO_AUTH"
_NO_AUTH_TRUE = {"1", "true", "yes", "on"}


def resolve_no_auth(cli_value: bool | None = None) -> bool:
    """True when authentication is deliberately disabled for this process.

    Sources: ``--no-auth``, or ``LOVEWORK_DASHBOARD_NO_AUTH=1``. This flag is
    the explicit operator consent that lets a non-loopback bind run without a
    token (see ``bind_policy_error``); it is not a default and not a fallback.
    """
    if cli_value:
        return True
    return (os.environ.get(NO_AUTH_ENV) or "").strip().lower() in _NO_AUTH_TRUE


def is_loopback_host(host: str) -> bool:
    """True only for bind hosts that stay on this machine.

    Anything else -- 0.0.0.0, ::, a LAN address, a DNS name -- is treated as
    network-reachable, including the empty string (which would bind wide).
    """
    h = (host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    if not h:
        return False
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def bind_policy_error(host: str, token: str | None, no_auth: bool = False) -> str | None:
    """Return a refusal message when the bind config is unsafe, else None.

    A non-loopback bind without a token would expose every principal's wiki,
    reports, logs and the MCP server (including its run_python tool) to the
    whole network, unauthenticated. Refuse to start; never warn and continue.

    ``no_auth=True`` (the explicit ``--no-auth`` / LOVEWORK_DASHBOARD_NO_AUTH
    opt-out) is the one deliberate exception: the operator has consented to an
    unauthenticated process, so the refusal is bypassed -- loudly, at startup.
    """
    if no_auth:
        return None
    if token or is_loopback_host(host):
        return None
    return (
        f"refusing to start: --host {host!r} is not loopback and no dashboard\n"
        "token is configured. Every principal's wiki, reports, logs and the MCP\n"
        "server (including its run_python tool) would be exposed to the network\n"
        "without authentication. Fix with one of:\n"
        "  --host 127.0.0.1                 # loopback only (local access)\n"
        "  --token <secret>                 # or LOVEWORK_DASHBOARD_TOKEN in the environment\n"
        "LAN form: --host 0.0.0.0 --token <secret>\n"
        "Internal-trial LAN form (no auth, trusted home LAN only):\n"
        "  --host 0.0.0.0 --no-auth         # or LOVEWORK_DASHBOARD_NO_AUTH=1\n"
    )


# ── HTTP handler ──────────────────────────────────────────────────────────
class DashboardHandler(BaseHTTPRequestHandler):
    # Headers queued for the next end_headers() call (e.g. Set-Cookie).
    _extra_headers: list[tuple[str, str]] | None = None

    def _queue_header(self, key: str, value: str) -> None:
        if getattr(self, "_extra_headers", None) is None:
            self._extra_headers = []
        self._extra_headers.append((key, value))

    def end_headers(self) -> None:
        for key, value in getattr(self, "_extra_headers", None) or []:
            self.send_header(key, value)
        self._extra_headers = None
        super().end_headers()

    def _supplied_token(self, parsed) -> tuple[str | None, bool]:
        """Return (token, came_from_query) for this request, or (None, False)."""
        header = self.headers.get("Authorization", "")
        if header[:7].lower() == "bearer ":
            return header[7:].strip() or None, False
        query = parse_qs(parsed.query)
        if query.get("token"):
            return query["token"][0], True
        cookie = self.headers.get("Cookie", "")
        for chunk in cookie.split(";"):
            name, _, value = chunk.strip().partition("=")
            if name == AUTH_COOKIE_NAME and value:
                return unquote(value), False
        return None, False

    def _require_auth(self, parsed) -> bool:
        """True when the request may proceed; otherwise send 401 and return False."""
        expected = AUTH_TOKEN
        if not expected:
            return True
        supplied, from_query = self._supplied_token(parsed)
        if supplied is not None and hmac.compare_digest(supplied, expected):
            if from_query:
                # First browser load: remember the token so it can leave the URL.
                self._queue_header(
                    "Set-Cookie",
                    f"{AUTH_COOKIE_NAME}={quote(expected)}; Path=/; HttpOnly; SameSite=Lax",
                )
            return True
        body = (
            "401 Unauthorized — LoveWork dashboard requires a token.\n\n"
            "Browser: open http://<host>:<port>/?token=<token> once; the cookie\n"
            "         then carries the session.\n"
            "API/MCP: send 'Authorization: Bearer <token>' (or ?token=<token>).\n"
        ).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("WWW-Authenticate", 'Bearer realm="lovework"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        if not self._require_auth(parsed):
            return
        path = parsed.path
        query = parsed.query
        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                html = render_lan_index()
                self.wfile.write(html.encode("utf-8"))
            except Exception as e:
                self.wfile.write(f"<h1>Error rendering LoveWork index</h1><pre>{e}</pre>".encode("utf-8"))
        elif path == "/dashboard" or path == "/dashboard/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                html = render_html()
                self.wfile.write(html.encode("utf-8"))
            except Exception as e:
                self.wfile.write(f"<h1>Error rendering dashboard</h1><pre>{e}</pre>".encode("utf-8"))
        elif path in ("/usage", "/usage/"):
            # LJ, 2026-10-13: "We started collecting tracking the tokens stats
            # per provider/model (including local). Can we see some stats about
            # that on the C&C web page?" — a read over the ledgers that already
            # exist (`usage_stats.py`), never a second ledger.
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                import usage_stats

                params = parse_qs(query)
                days = int((params.get("days") or ["7"])[0])
                by_day = (params.get("by_day") or ["0"])[0] not in ("0", "", "no")
                html = usage_stats.render_html(
                    usage_stats.aggregate(days=days), by_day=by_day)
                self.wfile.write(html.encode("utf-8"))
            except Exception as e:
                self.wfile.write(f"<h1>Error rendering usage</h1><pre>{e}</pre>".encode("utf-8"))
        elif path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "ts": datetime.now().isoformat()}).encode())
        elif path == "/api/registry":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(fetch_registry_stats(), default=str).encode())
        elif path == "/api/progress":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(fetch_live_run_progress(), default=str).encode())
        else:
            # /principals/ is canonical. Keep old local bookmarks alive while
            # making the principal→agent vocabulary visible in every new URL.
            if path in ("/docs", "/docs/", "/" + DOCS_SITE_REL):
                # trailing slash matters: docsify fetches `<page>.md` relative to the
                # page URL, so the SPA must be served at /docs/html/
                self.send_response(302)
                self.send_header("Location", f"/{DOCS_SITE_REL}/")
                self.end_headers()
                return
            if path.startswith("/candidates/"):
                canonical = "/principals/" + path.removeprefix("/candidates/")
                if query:
                    canonical = f"{canonical}?{query}"
                self.send_response(302)
                self.send_header("Location", canonical)
                self.end_headers()
                return
            # Publish only documentation plus the narrow principal-result
            # surfaces. Never expose the state tree as a filesystem mirror.
            result = _serve_principal_public_path(path, query)
            if result is None:
                result = _serve_general_public_path(path, query)
            if result:
                self.send_response(200)
                self.send_header("Content-Type", result["mime"])
                self.send_header("Content-Length", str(len(result["data"])))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(result["data"])
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(
                    f"404 — Not found: {path}\n\n"
                    "Try /principals/vj/wiki/reports/ for VJ results or /docs/00-index.md for documentation.\n"
                    .encode()
                )

    def do_POST(self):
        """MCP endpoint: POST /mcp with a JSON-RPC 2.0 body.

        Routed to mcp_server.handle_request(). Returns the JSON-RPC response
        (or 202 with no body for notifications).
        """
        parsed = urlparse(self.path)
        if not self._require_auth(parsed):
            return
        path = parsed.path
        if path != "/mcp":
            self.send_response(404)
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b""
        except Exception as e:
            self._send_json(400, {"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": f"Read error: {e}"}})
            return

        # Lazy import avoids a hard dependency at module load (and avoids a
        # circular import: mcp_server imports dashboard_server for fetch_*).
        from mcp_server import handle_request

        try:
            resp = handle_request(body)
        except Exception as e:
            logging.exception("MCP handle_request crashed")
            self._send_json(500, {"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32603, "message": f"Internal error: {e}"}})
            return

        if resp is None:
            # JSON-RPC notification (no id) — acknowledge without a body.
            self.send_response(202)
            self.end_headers()
            return
        self._send_json(200, json.loads(resp.decode("utf-8")))

    def _send_json(self, status: int, obj: dict) -> None:
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        # Never log the token: the request line may carry ?token=...
        message = _TOKEN_IN_QUERY.sub(r"\1REDACTED", fmt % args)
        logging.info("%s - - %s", self.address_string(), message)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LoveWork Dashboard Server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address. Loopback by default; pass 0.0.0.0 for LAN access, "
             "which requires a token.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="shared access token (falls back to $LOVEWORK_DASHBOARD_TOKEN). "
             "Required for any non-loopback bind unless --no-auth is given.",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="disable authentication for this process (also $LOVEWORK_DASHBOARD_NO_AUTH=1). "
             "This is the explicit consent that allows a non-loopback bind without a "
             "token: internal R&D trial on the trusted home LAN only. Any exposure "
             "beyond that LAN must use --token instead.",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    # Refuse the dangerous configuration before opening a socket.
    token = resolve_token(args.token)
    no_auth = resolve_no_auth(args.no_auth)
    if no_auth and token:
        logging.warning(
            "auth is disabled (--no-auth / $%s): the supplied token is IGNORED "
            "and every route is served without credentials",
            NO_AUTH_ENV,
        )
        token = None
    refusal = bind_policy_error(args.host, token, no_auth=no_auth)
    if refusal:
        sys.stderr.write(refusal)
        sys.stderr.flush()
        logging.error("refusing to start: non-loopback bind %s without a token", args.host)
        return 2
    configure_auth(token)

    lan_exposed = not is_loopback_host(args.host)
    if no_auth:
        logging.warning(
            "AUTH DISABLED (--no-auth / $%s): every route on this process, including "
            "POST /mcp and its run_python tool, is served without credentials",
            NO_AUTH_ENV,
        )
    server = HTTPServer((args.host, args.port), DashboardHandler)
    # Effective URL a human can open: the wildcard addresses are not browsable.
    url_host = "127.0.0.1" if args.host in {"0.0.0.0", "::", ""} else args.host
    bind_desc = f"LAN-exposed ({args.host})" if lan_exposed else "loopback-only"
    if no_auth and lan_exposed:
        bind_desc += (" (internal trial) — any exposure beyond the trusted home LAN "
                      "must use --token")
    logging.info(
        "LoveWork dashboard: http://%s:%d  auth=%s  bind=%s",
        url_host, args.port,
        "on" if token else "off",
        bind_desc,
    )
    logging.info("  wiki:    %s", WIKI)
    logging.info("  logs:    %s", LOGS)
    logging.info("  jobs.db: %s", JOBS_DB)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("shutting down")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
