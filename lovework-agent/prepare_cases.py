#!/usr/bin/env python3
"""Prepare review dossiers for GO listings in an existing LoveWork report."""

from __future__ import annotations

import argparse
from pathlib import Path

from application_packs import go_entries_from_report, insert_pack_report_section, prepare_go_cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare LoveWork GO application packs")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true", help="List packs without creating directories")
    args = parser.parse_args()

    entries = go_entries_from_report(args.report)
    results = prepare_go_cases(entries, only_new=False, dry_run=args.dry_run)
    if not results:
        print("No GO listings found in report.")
        return 0
    for result in results:
        path = str(result.path) if result.path else "-"
        print(f"{result.status}\t{result.entry.org_name}\t{result.entry.title}\t{path}\t{result.reason}")
    if not args.dry_run:
        insert_pack_report_section(args.report, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
