#!/usr/bin/env python3
"""The pack queue — which GO packs still need steps 1-3 (review, diligence, tailor).

Read-only and deterministic: it prints the pending packs, one per line, and *nothing at
all* when the queue is empty, so the cron job that consumes it can be monitor-driven and
stay silent on quiet days (an unchanged read never wakes the agent).

A pack is pending when any of the three signals is missing:

* no ``Advert review —`` section in its **dossier** (``<pack>.md`` since
  2026-10-15, the legacy ``<pack>.txt`` on older packs — asked for through
  ``application_packs.dossier_path``, never globbed),
* the checklist in that dossier does not tick steps 1-3, or
* the flat tailored material is absent (``cover-letter.md`` / ``cv-tailored.md``).

Only packs younger than the horizon are considered. GO packs are cut per crawl, and an
advert that has crossed the matcher's staleness floor (10-day cap / 25-day drop) is a
dead lead: reviewing it spends a worker on an application nobody can make. Older packs
stay on disk, untouched, and are simply not queued.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402  (path set above, matching the sibling modules)

HORIZON_DAYS = 10   # the matcher's staleness cap (10-day cap / 25-day drop): a pack whose
# advert is older than the cap is a lead nobody can act on, so it is not queued.
PACK_SUFFIX = "-LoveWork"
REQUIRED_MATERIAL = ("cover-letter.md", "cv-tailored.md")
REVIEW_MARKER = "Advert review —"


def _pack_date(name: str) -> date | None:
    """A pack's preparation date, from its ``YYYYMMDD-…`` name."""
    match = re.match(r"(\d{4})(\d{2})(\d{2})-", name)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _dossier_text(pack_dir: Path) -> str:
    """The pack's dossier text, or "" when it has no dossier at all.

    The dossier is asked for by *identity* (`application_packs.dossier_path`:
    ``<pack>.md`` first, then the legacy ``<pack>.txt``), not by globbing for a
    suffix. This module was the last reader still requiring a ``.txt`` file, so
    the 2026-10-15 rename to markdown (LJ: *"when LJ creates it, it will be .txt.
    But when lovework creates the yyyymmdd-Company_name-Opening_name — it should
    be .md not .txt"*) left it announcing ``no .txt stub`` for every pack in the
    tree — 253 of them on 2026-10-16, which is what woke the cron job whose
    monitor reads this queue.
    """
    from application_packs import dossier_path

    dossier = dossier_path(pack_dir)
    if dossier is None:
        return ""
    return dossier.read_text(encoding="utf-8", errors="ignore")


def _missing(pack_dir: Path) -> list[str]:
    """What this pack still lacks, in the order the workflow does the work."""
    text = _dossier_text(pack_dir)
    if not text:
        return ["no dossier"]
    # A superseded duplicate is history, not a pending pack: the same advert has
    # a newer directory, and listing both made the queue grow every day
    # (2026-10-13 — see application_packs.mark_superseded_duplicates).
    if "Superseded by " in text:
        return []
    problems = []
    if REVIEW_MARKER not in text:
        problems.append("no advert review")
    if len(re.findall(r"^- \[x\]", text, re.M)) < 3:
        problems.append("steps 1-3 unticked")
    absent = [name for name in REQUIRED_MATERIAL if not (pack_dir / name).is_file()]
    if absent:
        problems.append("missing " + ", ".join(absent))
    return problems


def _principals(state_dir: Path, profile: str) -> list[str]:
    if profile != "all":
        return [profile.lower()]
    # One authority (`config.principal_names`): a name shape is not a principal, so
    # `state/idx/` — the situation cache — is not queued as one (2026-10-15).
    return config.principal_names(state_dir)


def _role_key(pack_dir: Path) -> tuple[str, str]:
    """The (organisation, position) a pack is for, normalised.

    Two packs can describe the same job when a lead arrives through a different source
    URL — the advert-identity idempotency key differs, so both are cut (42 such pairs
    existed on 2026-10-04). The queue works one pack per role, newest first, so a role
    is never researched twice.
    """
    text = _dossier_text(pack_dir)
    def field(label: str) -> str:
        match = re.search(rf"^{label}: (.+)$", text, re.M)
        return re.sub(r"[^a-z0-9]+", "", (match.group(1) if match else "").lower())
    return field("Organisation"), field("Position")


def queue(*, state_dir: Path | None = None, profile: str = "all",
          today: date | None = None, horizon_days: int = HORIZON_DAYS) -> list[dict]:
    """The pending packs, newest first — the whole queue read, no side effects.

    One entry per role: if the same (principal, organisation, position) has several
    pending packs, only the newest is queued.
    """
    state_dir = state_dir or config.STATE_DIR
    today = today or date.today()
    pending: list[dict] = []
    for principal in _principals(state_dir, profile):
        root = state_dir / principal / "applications"
        if not root.is_dir():
            continue
        for pack in sorted(root.iterdir()):
            if not (pack.is_dir() and pack.name.endswith(PACK_SUFFIX)):
                continue
            prepared = _pack_date(pack.name)
            if prepared is None or (today - prepared).days > horizon_days:
                continue
            missing = _missing(pack)
            if missing:
                pending.append({
                    "principal": principal,
                    "pack": str(pack),
                    "prepared": prepared.isoformat(),
                    "role": _role_key(pack),
                    "missing": missing,
                })
    pending.sort(key=lambda row: row["prepared"], reverse=True)
    # One entry per role: a pack is skipped when a *newer* pack exists for the same
    # (principal, organisation, position) — pending or already worked — because the
    # newer sibling covers that role. Without the "already worked" half, the 42
    # duplicate twins of the 2026-10-04 backfill stayed queued for ever.
    newest: dict[tuple[str, tuple[str, str]], str] = {}
    complete: dict[tuple[str, tuple[str, str]], str] = {}
    for principal in _principals(state_dir, profile):
        root = state_dir / principal / "applications"
        if not root.is_dir():
            continue
        for pack in root.iterdir():
            if not (pack.is_dir() and pack.name.endswith(PACK_SUFFIX)):
                continue
            prepared = _pack_date(pack.name)
            key = (principal, _role_key(pack))
            if prepared is None or key[1] == ("", ""):
                continue
            stamp = prepared.isoformat()
            if stamp > newest.get(key, ""):
                newest[key] = stamp
            if not _missing(pack) and stamp > complete.get(key, ""):
                complete[key] = stamp
    unique: list[dict] = []
    seen: set[tuple[str, tuple[str, str]]] = set()
    for row in pending:
        key = (row["principal"], row["role"])
        if key[1] != ("", ""):
            # the role is covered by a same-or-newer pack that is already worked, or by
            # a strictly newer pack still queued; otherwise only the first of equal-date
            # pending twins is kept (so a role is never queued twice)
            if complete.get(key, "") >= row["prepared"] or newest.get(key, "") > row["prepared"]:
                continue
            if key in seen:
                continue
        seen.add(key)
        unique.append(row)
    return unique


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GO packs awaiting review/diligence/tailoring")
    parser.add_argument("--profile", default="all",
                        help="principal name or 'all' (default)")
    parser.add_argument("--horizon-days", type=int, default=HORIZON_DAYS,
                        help="ignore packs older than this many days (default 30)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    pending = queue(profile=args.profile, horizon_days=args.horizon_days)
    if args.json:
        print(json.dumps(pending, indent=2, sort_keys=True))
        return 0
    for row in pending:
        print(f"{row['principal']}\t{row['pack']}\t{'; '.join(row['missing'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
