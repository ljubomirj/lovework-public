#!/usr/bin/env python3
"""Prepare review dossiers for GO listings in an existing LoveWork report."""

from __future__ import annotations

import argparse
import sys

import config
from writer_boundary import WriterBusy, standalone_writer_guard
from pathlib import Path

from application_packs import (go_entries_from_report, insert_pack_report_section,
                               prepare_go_cases, relink_packs)


def main():
    """Direct invocation: hold the writer lease, then prepare the GO review packs."""
    args = _parse_args()
    principal = args.profile
    state_root = config.STATE_DIR / principal
    try:
        with standalone_writer_guard(principal, mode="direct", state_root=state_root,
                                     shared=()):
            return _prepare_cases_body(args)
    except WriterBusy as busy:
        print(f"[LOCK] writer busy: {busy.resource.key} — another operation holds "
              f"this principal's lease (attempt recorded at {busy.attempt_path})")
        raise SystemExit(75)


def _parse_args():
    parser = argparse.ArgumentParser(description="Prepare LoveWork GO application packs")
    parser.add_argument("--profile", default="lj", choices=["lj", "vj", "kj", "pk"],
                        help="Principal whose application packs to prepare")
    parser.add_argument("--report", type=Path,
                        help="Report whose GO listings to pack (required unless --relink)")
    parser.add_argument("--dry-run", action="store_true", help="List packs without creating directories")
    parser.add_argument("--relink", action="store_true",
                        help="Only ensure the unified applications/ view mirrors every "
                             "state-owned pack (idempotent; run after merging a crawl)")
    args = parser.parse_args()
    if not args.relink and args.report is None:
        parser.error("--report is required unless --relink is given")
    return args


def _prepare_cases_body(args=None):
    args = args or _parse_args()

    if args.relink:
        counts = relink_packs(args.profile)
        print(f"packs: {counts['packs']}  mirrored now: {counts['created']}  "
              f"already linked: {counts['present']}  other entries: {counts['other_entries']}")
        return 0

    entries = go_entries_from_report(args.report)
    results = prepare_go_cases(entries, principal=args.profile, only_new=False, dry_run=args.dry_run)
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
