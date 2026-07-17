#!/usr/bin/env python3
"""
Cross-check a wiki report (or the latest one) against applications/ and
Gmail, then append "prior contact found" blocks to the corresponding
wiki/orgs/ pages. Idempotent (same date = same block).

Usage:
    python crosscheck.py                       # cross-check the latest report
    python crosscheck.py --report path/to.md   # specific report
    python crosscheck.py --org Poolside        # specific org only
    python crosscheck.py --include-maybe       # also check MAYBE listings
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import config
from history import scan_history
from wiki_store import WikiEntry, WikiStore


# Match report sections like:
#   ### Poolside — Member of Engineering (Pre-training / Data Acquisition)
#   - **URL**: https://...
#   - **Score**: 9.0/10
#   - **Reasoning**: ...
SECTION_RE = re.compile(
    r"^###\s+(?P<org>.+?)\s+—\s+(?P<title>.+?)\s*$"
)
URL_RE = re.compile(r"^\s*-\s*\*\*URL\*\*:\s*(?P<url>\S+)")
SCORE_RE = re.compile(
    r"^\s*-\s*\*\*Score\*\*:\s*(?P<score>\d+(?:\.\d+)?)/10"
    r"(?:\s*\((?P<decision_inline>GO|MAYBE|FLAG|DROP)\))?"
)
DECISION_RE = re.compile(
    r"^\s*-\s*\*\*Decision\*\*:\s*\*?(?P<decision>GO|MAYBE|FLAG|DROP)\*?"
)
# Match index lines like "- **2026-06-20** | Poolside — Member of Engineering | 8.5/10 [url](url)"
INDEX_LISTING_RE = re.compile(
    r"^-\s*\*\*(?P<date>\d{4}-\d{2}-\d{2})\*\*\s*\|"
    r"\s*(?P<org>[^—|]+?)\s*—\s*"
    r"(?P<title>.+?)\s*\|\s*"
    r"(?P<score>\d+(?:\.\d+)?)/10"
    r"(?:\s*\[(?P<url>[^\]]+)\]\([^)]+\))?"
)


def parse_report(path: Path, *, include_maybe: bool = False) -> list[dict]:
    """Return [{date, org, title, score, url}] from a wiki markdown file.

    Handles two formats:
      1. **Report format** (wiki/reports/*.md): "### Org — Title" sections
         with bullet-point metadata (URL, Score, Reasoning). Section
         boundaries include "## GO", "## MAYBE", "## New Listings", etc.
      2. **Index format** (wiki/index.md): bullet lists with
         "- **DATE** | Org — Title | SCORE/10 [url](url)".

    The report's first line is the date (`# FULL SWEEP — 2026-06-29 14:28:13 BST`
    or `# INCREMENTAL — 2026-06-29 14:33:37 BST`).
    The index has the date inline on each line.
    """
    text = path.read_text(encoding="utf-8")
    out: list[dict] = []

    # Detect report date from the H1 if present.
    report_date = ""
    for line in text.splitlines()[:5]:
        m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", line)
        if m:
            report_date = m.group(1)
            break

    in_go_section = False
    in_maybe_section = False
    in_new_section = False  # "## New Listings (N)" — treat as GO by default
    section_org: str | None = None
    section_title: str | None = None
    section_url: str | None = None
    section_score: float | None = None
    section_decision: str | None = None

    def flush_section() -> None:
        nonlocal section_org, section_title, section_url, section_score, section_decision
        # A section's decision can be:
        #  - explicit via **Decision**: GO/MAYBE/...
        #  - implied by which ## H2 it's nested in (GO/MAYBE/NEW/DROP/...)
        #  - inline in **Score**: X/10 (GO)
        if section_org and section_score is not None:
            decision = section_decision
            if not decision:
                if in_go_section:
                    decision = "GO"
                elif in_maybe_section:
                    decision = "MAYBE"
                # in_new_section is treated as GO (the report's "New Listings"
                # section only renders GO + MAYBE entries by the wiki writer).
            if decision in ("GO", "MAYBE") and (decision == "GO" or include_maybe):
                out.append({
                    "date": report_date,
                    "org": section_org.strip(),
                    "title": (section_title or "").strip(),
                    "score": float(section_score),
                    "url": (section_url or "").strip(),
                })
        section_org = section_title = section_url = None
        section_score = None
        section_decision = None

    for line in text.splitlines():
        s = line.rstrip()
        # Section heading (### Org — Title).
        sec = SECTION_RE.match(s)
        if sec:
            flush_section()
            section_org = sec.group("org")
            section_title = sec.group("title")
            continue
        # H2 boundary: flush and update section flags.
        if s.startswith("## ") or s.startswith("# "):
            flush_section()
            low = s.lower()
            if "## go" in low or "## go listings" in low:
                in_go_section = True
                in_maybe_section = False
                in_new_section = False
            elif "## maybe" in low:
                in_go_section = False
                in_maybe_section = True
                in_new_section = False
            elif "## flag" in low or "## drop" in low or "## long-lasting" in low:
                in_go_section = False
                in_maybe_section = False
                in_new_section = False
            elif "## new listings" in low:
                # Reports put only GO/MAYBE entries inside "## New Listings",
                # so the default decision is GO unless overridden inline.
                in_go_section = True
                in_maybe_section = False
                in_new_section = True
            else:
                in_go_section = False
                in_maybe_section = False
                in_new_section = False
            continue
        # Index listing line.
        m = INDEX_LISTING_RE.match(s.strip())
        if m:
            flush_section()
            # Index listings don't have a section, so we treat them as
            # always-target (the caller can post-filter by decision).
            out.append({
                "date": m.group("date"),
                "org": m.group("org").strip(),
                "title": m.group("title").strip(),
                "score": float(m.group("score")),
                "url": (m.group("url") or "").strip(),
            })
            continue
        # Section metadata (URL / Score / Decision).
        um = URL_RE.match(s)
        if um and section_org:
            section_url = um.group("url")
        sm = SCORE_RE.match(s)
        if sm and section_org:
            section_score = float(sm.group("score"))
            if sm.group("decision_inline"):
                section_decision = sm.group("decision_inline")
        dm = DECISION_RE.match(s)
        if dm and section_org:
            section_decision = dm.group("decision")

    flush_section()
    return out


def latest_report() -> Path | None:
    """Return the most recent wiki report, or None."""
    reports = sorted(
        config.WIKI_REPORTS.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return reports[0] if reports else None


def cross_check(
    org: str,
    title: str,
    score: float,
    url: str,
    *,
    today: str,
    wiki: WikiStore | None = None,
) -> dict | None:
    """Look up prior contact and (optionally) append a wiki block. Returns
    the summary dict (or None if no prior contact).
    """
    try:
        prior = scan_history(org, use_gmail=True)
    except Exception as ex:
        print(f"  [warn] scan_history({org!r}) failed: {ex}", file=sys.stderr)
        return None
    if not prior.has_application and not prior.gmail_events:
        return None

    if wiki is not None:
        block = (
            f"### {today} — prior contact found\n\n"
            f"- **Source entry**: {title} ({score:.1f}/10) — {url or 'no url'}\n"
            f"- **Prior contact**: {prior.summary()}\n"
        )
        # Append via update_org_page (append-only by design).
        wiki.update_org_page(WikiEntry(
            org_name=org, title="(prior-contact cross-check)",
            url=None, location=None, score=0.0, decision="",
            reasoning=block, source="cross_check",
        ))
    return {
        "org": org,
        "title": title,
        "has_application": prior.has_application,
        "has_rejection": prior.has_rejection,
        "summary": prior.summary(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--report", type=Path, help="Specific report file")
    parser.add_argument("--org", help="Cross-check only this org (substring match)")
    parser.add_argument("--include-maybe", action="store_true", help="Also check MAYBE listings")
    args = parser.parse_args()

    report = args.report or latest_report()
    if report is None or not report.exists():
        print("No report found.", file=sys.stderr)
        sys.exit(1)
    print(f"Cross-checking report: {report}")

    listings = parse_report(report, include_maybe=args.include_maybe)
    if args.org:
        listings = [l for l in listings if args.org.lower() in l["org"].lower()]
    print(f"Listings to check: {len(listings)}")

    wiki = WikiStore()
    today = datetime.now().strftime("%Y-%m-%d")
    found = 0
    seen = set()
    for l in listings:
        key = l["org"].lower().strip()
        if key in seen:
            continue
        seen.add(key)
        result = cross_check(
            l["org"], l["title"], l["score"], l["url"],
            today=today, wiki=wiki,
        )
        if result:
            found += 1
            flag = " ✗ REJECTED" if result["has_rejection"] else ""
            print(f"  {l['org']}: {result['summary']}{flag}")

    print()
    print(f"Prior contact found for {found} of {len(seen)} unique org(s).")


if __name__ == "__main__":
    main()
