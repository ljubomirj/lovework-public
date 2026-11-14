#!/usr/bin/env python3
"""Repair organisation pages whose filenames differ only by case.

A wiki page's filename is its identity, and org names arrive from sources that
disagree about capitalisation — in one VJ crawl, "OpenAI" and "Openai" both
reached the wiki for the same company. On a case-sensitive filesystem that is two
pages for one organisation; on macOS, which is case-insensitive, they cannot
coexist at all, so Linux and macOS grew different wikis and git could not carry
both (`state/vj/wiki/orgs/AON.md` beside `Aon.md`, 2026-09-20).

`WikiStore.org_page_path` now folds names case-insensitively so this cannot
recur. This tool repairs what the old behaviour already wrote:

* find every group of pages whose stems differ only by case;
* pick the spelling to keep — the one the principal's registry mostly uses
  (ties: the page holding more findings, then byte order, so the choice is
  deterministic and explainable);
* merge the group's findings into that page, deduplicated by finding header and
  in date order;
* move the other files aside, never delete them.

Sealed strata bundles are deliberately untouched: they are immutable point-in-
time evidence and rewriting one would break its manifest digests. Re-seal after a
repair if a bundle needs to be clean.

Usage::

    ../venv/bin/python3 wiki_case_repair.py --profile vj --dry-run
    ../venv/bin/python3 wiki_case_repair.py --profile vj
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PREAMBLE = "Findings from lovework-agent.\n\n---\n\n"
BACKUP_DIRNAME = "_case_dupes"
_FINDING_RE = re.compile(r"^### ", re.MULTILINE)


def case_collisions(orgs_dir: Path) -> Dict[str, List[Path]]:
    """Groups of pages whose stems differ only by case (folded stem → paths)."""
    groups: Dict[str, List[Path]] = defaultdict(list)
    if not orgs_dir.is_dir():
        return {}
    for path in sorted(orgs_dir.glob("*.md")):
        groups[path.stem.casefold()].append(path)
    return {stem: paths for stem, paths in groups.items() if len(paths) > 1}


def registry_org_counts(cache_dir: Path) -> Counter:
    """How many registry rows each org spelling has (the principal's own usage)."""
    counts: Counter = Counter()
    path = Path(cache_dir) / "jobs.csv"
    if not path.is_file():
        return counts
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("org") or "").strip()
                if name:
                    counts[name] += 1
    except OSError as e:
        logger.warning(f"could not read {path}: {e}")
    return counts


def findings(path: Path) -> List[str]:
    """The finding blocks of a page: each `### <date> — <title>` and its body."""
    text = path.read_text(encoding="utf-8", errors="replace")
    starts = [m.start() for m in _FINDING_RE.finditer(text)]
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        blocks.append(text[start:end].strip("\n"))
    return blocks


def choose_canonical(paths: List[Path], counts: Counter) -> Path:
    """The spelling to keep: registry usage, then findings, then byte order.

    Registry usage is the strongest signal — how often the principal's own
    crawls have spelled the name this way. Ties fall back to the fuller page and
    then to plain byte order, so the outcome never depends on directory order.
    """
    return sorted(
        paths,
        key=lambda p: (-counts.get(p.stem, 0), -len(findings(p)), p.stem),
    )[0]


def merge_pages(canonical: Path, group: List[Path]) -> str:
    """The canonical page plus the findings only the other spellings carry.

    The kept page's blocks are preserved verbatim — including any duplicate
    headers it already had, because those are its history and a collision repair
    is not a place to drop content. Only blocks the kept page lacks are added,
    deduplicated by their header line, and the result stays in date order like
    every other page.
    """
    blocks = findings(canonical)
    headers = {block.splitlines()[0].strip() for block in blocks}
    for path in group:
        if path == canonical:
            continue
        for block in findings(path):
            header = block.splitlines()[0].strip()
            if header in headers:
                continue
            headers.add(header)
            blocks.append(block)
    body = "\n\n".join(sorted(blocks, key=lambda b: b.splitlines()[0]))
    return f"# {canonical.stem}\n\n{PREAMBLE}{body}\n"


def repair_org_dir(orgs_dir: Path, counts: Counter, *, dry_run: bool) -> List[dict]:
    """Merge every case-colliding group in one `wiki/orgs` directory."""
    repaired: List[dict] = []
    for stem, paths in sorted(case_collisions(orgs_dir).items()):
        canonical = choose_canonical(paths, counts)
        merged = merge_pages(canonical, paths)
        losers = [p for p in paths if p != canonical]
        merged_findings = len(_FINDING_RE.findall(merged))
        repaired.append({
            "stem": stem,
            "kept": canonical.name,
            "dropped": [p.name for p in losers],
            "kept_findings": len(findings(canonical)),
            "merged_findings": merged_findings,
        })
        if dry_run:
            continue
        backup = orgs_dir.parent / BACKUP_DIRNAME
        backup.mkdir(parents=True, exist_ok=True)
        canonical.write_text(merged, encoding="utf-8")
        for path in losers:
            shutil.move(str(path), str(backup / path.name))
        logger.info(
            f"[case-repair] {[p.name for p in paths]} → {canonical.name} "
            f"({merged_findings} findings; originals kept in {backup})"
        )
    return repaired


def main(argv: Optional[List[str]] = None) -> int:
    from principal_runtime import resolve_principal_runtime

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", required=True,
                        help="principal whose wiki to repair (lj, vj, kj, …)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the collisions and the merged result, write nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    runtime = resolve_principal_runtime(args.profile)
    results = repair_org_dir(runtime.wiki_root / "orgs",
                             registry_org_counts(runtime.cache_dir),
                             dry_run=args.dry_run)
    verb = "would merge" if args.dry_run else "merged"
    if not results:
        print(f"[case-repair] {args.profile}: no case-colliding org pages")
        return 0
    for row in results:
        print(f"[case-repair] {args.profile}: {verb} {row['dropped']} into {row['kept']} "
              f"({row['kept_findings']} + {sum(1 for _ in row['dropped'])} file(s) "
              f"→ {row['merged_findings']} findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
