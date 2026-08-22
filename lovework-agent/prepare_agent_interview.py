#!/usr/bin/env python3
"""Prepare a non-live agent-to-agent interview case."""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

from agent_interviews import prepare_superme_interview_case


def _date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch public SuperMe protocol material and prepare a LoveWork ATA "
            "case. This command never starts or submits an interview."
        )
    )
    parser.add_argument("--principal", default="lj")
    parser.add_argument("--company", required=True)
    parser.add_argument("--position", required=True)
    parser.add_argument("--role-url", required=True)
    parser.add_argument("--date", type=_date, default=date.today())
    parser.add_argument("--applications-dir", type=Path)
    parser.add_argument("--refresh-references", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = prepare_superme_interview_case(
        principal=args.principal,
        company=args.company,
        position=args.position,
        role_url=args.role_url,
        when=args.date,
        applications_dir=args.applications_dir,
        refresh_references=args.refresh_references,
        dry_run=args.dry_run,
    )
    print(f"ATA preparation: {result.status}")
    print(f"Case: {result.path}")
    print(f"Manifest: {result.manifest_path}")
    print("External interview actions: none")


if __name__ == "__main__":
    main()
