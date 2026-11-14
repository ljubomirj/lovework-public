"""Backfill missing org pages from the principal's own sealed reports
(2026-10-07 link-rot class fix).

The class: reports have always linked an org page per listing, but the page
writer and the link writer could disagree (folded/kebab slugs, junk-gated
names, and — on vj/kj — hundreds of listings whose pages were simply never
written). Serving now resolves folds and renames; what remains genuinely
missing is rebuilt here FROM the sealed reports, which carry the full
listing block — decision, scores, URL, reasoning — as evidence.

The tool never invents a block (a page is only created for stems a report
actually links) and marks every backfilled page as such. Blocks are rendered
by `wiki_store.render_org_block` — the SAME writer the crawl uses — because
the first version of this tool copied the report's block text verbatim and
gave the wiki a second block schema (`### Org — Title` with `**Score**` and
no date): the index parser needs `### DATE — Title` plus `**Decision**`, so
1,778 backfilled blocks in three wikis were invisible to the index and to
every audit rule anchored on that heading (2026-10-07 audit).

Usage:
    wiki_backfill.py --profile lj [--dry-run] [--rewrite-backfilled]

`--rewrite-backfilled` regenerates pages that hold ONLY backfilled blocks
(a page that has since received real crawl blocks is left alone, so the
repair cannot destroy live findings).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import config

ORG_LINK_RE = re.compile(r"\]\(<?/?principals/[\w-]+/wiki/orgs/([^/>\s)]+)\.md>?\)")
BLOCK_SPLIT_RE = re.compile(r"(?=^### )", re.M)
REPORT_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
SECTION_RE = re.compile(r"^##\s+(GO|MAYBE|FLAG|DROP)\b")
FIELD_RE = re.compile(r"^- \*\*(?P<name>[^*]+)\*\*: (?P<value>.*)$")
ITEM_RE = re.compile(r"^\s+- (?P<item>.*)$")
HEADING_RE = re.compile(r"^###\s+(?:\d+\.\s+)?(?P<org>.*?) — (?P<title>.*?)\s*$")
BACKFILL_MARK = "Backfilled 2026-10-07 from sealed reports"
SCORE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)/10")


def _fold(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _float(value: str):
    m = SCORE_RE.search(value or "")
    return float(m.group(1)) if m else None


def _entry_from_report_block(block: str, decision: str, report_date: str):
    """One report block → a WikiEntry the org-page writer can render.

    The report block *is* the evidence: decision (from its section), scores,
    URL, location, source and reasoning. Fields the report does not carry
    stay empty rather than being guessed.
    """
    from wiki_store import WikiEntry

    head = block.split("\n", 1)[0]
    m = HEADING_RE.match(head)
    if not m:
        return None
    org = m.group("org").strip()
    title = m.group("title").strip().removesuffix("🆕").strip()
    fields: dict[str, str] = {}
    items: dict[str, list[str]] = {}
    current = None
    for line in block.split("\n")[1:]:
        fm = FIELD_RE.match(line)
        if fm:
            current = fm.group("name").strip().lower()
            fields[current] = fm.group("value").strip()
            continue
        im = ITEM_RE.match(line)
        if im and current in ("evidence alignment", "gaps"):
            items.setdefault(current, []).append(im.group("item").strip())
    from wiki_store import is_no_url_placeholder

    url = fields.get("url", "").strip()
    if is_no_url_placeholder(url):
        url = ""
    else:
        url = url.split(" ")[0]
    discovery_url = ""
    discovery_date = ""
    found = fields.get("found via", "")
    fv = re.search(r"\]\((?P<url>[^)]+)\)(?:\s*\((?P<date>\d{4}-\d{2}-\d{2})\))?", found)
    if fv:
        discovery_url = fv.group("url")
        discovery_date = fv.group("date") or ""
    content_hash = ""
    cached = fields.get("cached advert", "")
    ch = re.search(r"page_([0-9a-f]{32,64})\.md", cached)
    if ch:
        content_hash = ch.group(1)
    score = _float(fields.get("score", ""))
    unscored = "UNSCORED" in fields.get("score", "").upper() or score is None
    lifecycle = fields.get("lifecycle", "").split(" ")[0].strip() or None
    return WikiEntry(
        org_name=org,
        title=title,
        url=url or None,
        location=fields.get("location", "") or None,
        score=0.0 if unscored else score,
        decision=decision,
        reasoning=fields.get("reasoning", ""),
        source=fields.get("source", ""),
        date=discovery_date or report_date,
        lifecycle_status=lifecycle,
        fit_score=_float(fields.get("fit", "")),
        reach_score=_float(fields.get("reach", "")),
        flourish_score=_float(fields.get("flourish", "")),
        combined_score=_float(fields.get("combined", "")),
        recommended_action=fields.get("action", "") or None,
        discovery_url=discovery_url,
        discovery_date=discovery_date,
        primary_content_hash=content_hash or None,
        alignment_matrix=items.get("evidence alignment", []),
        gaps=items.get("gaps", []),
        application_angle=fields.get("application angle", ""),
        screening_story=fields.get("screening story", ""),
        likely_day_to_day=fields.get("likely day-to-day", ""),
        prestige_trap_risk=fields.get("prestige-trap risk", ""),
        salary=fields.get("salary", ""),
        seniority=fields.get("seniority", ""),
        assessment_status="UNSCORED" if unscored else "SCORED",
    )


def _report_entries(report_path: Path):
    """(stem, WikiEntry) for every listing block of one report.

    Scanned line by line, because the section heading that carries the
    decision ("## GO (9) — …") sits *above* the blocks it decides, and a
    block-split by heading puts it at the tail of the previous chunk.
    """
    from job_registry import _job_hash

    text = report_path.read_text(encoding="utf-8", errors="ignore")
    dm = REPORT_DATE_RE.match(report_path.name)
    report_date = dm.group(1) if dm else ""
    out = []
    decision = ""
    current: list[str] = []

    def flush() -> None:
        if not current or not decision:
            return
        body = "\n".join(current)
        lm = ORG_LINK_RE.search(body)
        if not lm:
            return
        entry = _entry_from_report_block(body, decision, report_date)
        if entry is None:
            return
        out.append((lm.group(1), entry,
                    _job_hash(entry.org_name, entry.title, entry.url or "")))

    for line in text.split("\n"):
        sm = SECTION_RE.match(line)
        if sm:
            flush()
            current = []
            decision = sm.group(1)
            continue
        if line.startswith("### "):
            flush()
            current = [line]
            continue
        if line.startswith("## "):
            flush()
            current = []
            decision = ""
            continue
        if current:
            current.append(line)
    flush()
    return out


def _all_blocks(text: str) -> list[str]:
    parts = re.split(r"(?m)^### ", text)
    return ["### " + p for p in parts[1:]]


def is_backfill_only(text: str) -> bool:
    """True when a page holds ONLY backfilled blocks (safe to regenerate)."""
    if BACKFILL_MARK not in text:
        return False
    for block in _all_blocks(text):
        if not re.match(r"^### \d{4}-\d{2}-\d{2} — ", block):
            return True          # a foreign-schema (report-shaped) block
    return False


def backfill(wiki_root: Path, *, dry_run: bool = False,
             rewrite_backfilled: bool = False) -> dict:
    from wiki_store import WikiStore, org_page_dirs, org_page_paths, render_org_block

    reports_dir = wiki_root / "reports"

    orgs_dir = wiki_root / "orgs"
    if not reports_dir.is_dir() or not any(d.is_dir() for d in org_page_dirs(wiki_root)):
        return {"error": "no reports/orgs dirs"}

    renames: dict[str, str] = {}
    rmap = wiki_root / "org-renames.json"
    if rmap.is_file():
        import json
        try:
            renames = {k.lower(): v for k, v in
                       json.loads(rmap.read_text(encoding="utf-8")).items()}
        except (ValueError, OSError):
            renames = {}

    existing = {f.stem for f in org_page_paths(wiki_root)}
    # A page that was MOVED to `_attic/` still exists: the migration put it
    # there because its name was junk, and its blocks were copied into a target
    # page. Counting only `orgs/` would call 323 of those "missing" and re-mint
    # every one of them, undoing the migration (2026-10-08 forensics).
    attic = wiki_root / "_attic"
    if attic.is_dir():
        existing |= {f.stem for f in attic.glob("*.md")}
    existing_fold: dict[str, str] = {}
    for stem in existing:
        path = orgs_dir / f"{stem}.md"
        if rewrite_backfilled and path.is_file() and is_backfill_only(
                path.read_text(encoding="utf-8", errors="ignore")):
            continue               # regenerated below
        existing_fold.setdefault(_fold(stem), stem)

    # stem -> list of entries, reports oldest first
    wanted: dict[str, list] = {}
    seen_identity: dict[str, set] = {}
    for rep in sorted(reports_dir.glob("*.md")):
        text = rep.read_text(encoding="utf-8", errors="ignore")
        if not ORG_LINK_RE.search(text):
            continue
        for stem, entry, identity in _report_entries(rep):
            if stem in existing and not (rewrite_backfilled and
                                         is_backfill_only((orgs_dir / f"{stem}.md").read_text(
                                             encoding="utf-8", errors="ignore"))):
                continue                     # page exists — nothing to do
            if _fold(stem) in existing_fold:
                continue                     # serving resolves the fold
            if stem.lower() in renames:
                continue                     # serving resolves the rename
            bucket = wanted.setdefault(stem, [])
            keys = seen_identity.setdefault(stem, set())
            key = (identity, entry.date)
            if key in keys:
                continue
            keys.add(key)
            bucket.append(entry)

    stats = {"missing_linked": len(wanted),
             "pages_created": 0, "blocks_written": 0}
    for stem, entries in sorted(wanted.items()):
        # A sealed report from before the 120-byte cap can link a stem
        # longer than eCryptfs accepts (143 bytes/component); the page we
        # create now is capped by the writer's own rule, not by the old
        # link text.
        path = orgs_dir / f"{WikiStore._safe_filename(stem)}.md"
        if path.exists() and not is_backfill_only(
                path.read_text(encoding="utf-8", errors="ignore")):
            stats.setdefault("skipped_live_pages", 0)
            stats["skipped_live_pages"] += 1
            continue
        pretty = path.read_text(encoding="utf-8").splitlines()[0][2:].strip() \
            if path.exists() and path.read_text(encoding="utf-8").startswith("# ") \
            else stem.replace("_", " ").strip() or stem
        body = [f"# {pretty}", "",
                "Backfilled from sealed reports: the page writer never created "
                "this page, but the reports linked it and carry the evidence "
                "blocks below. Blocks are rendered by the org-page writer "
                "(2026-10-07).", "", "---", ""]
        for entry in sorted(entries, key=lambda e: (e.date, e.title)):
            body.extend(render_org_block(entry))
            stats["blocks_written"] += 1
        if dry_run:
            continue
        path.write_text("\n".join(body), encoding="utf-8")
        stats["pages_created"] += 1
    return stats


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rewrite-backfilled", action="store_true",
                        help="regenerate pages that hold only backfilled blocks")
    args = parser.parse_args(argv)
    root = config.STATE_DIR / args.profile / "wiki"
    if not root.is_dir():
        print(f"no wiki at {root}", file=sys.stderr)
        return 1
    stats = backfill(root, dry_run=args.dry_run,
                     rewrite_backfilled=args.rewrite_backfilled)
    print(args.profile, stats)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
