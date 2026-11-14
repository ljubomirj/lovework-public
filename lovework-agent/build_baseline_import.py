"""Baseline import for org memories (PLAN.md T2.1, D22).

One `baseline:` note per tracked org, generated from the 4-cube
(company_events.tally) and the outcomes ledger, so each org memory opens
with its compressed past instead of a blank page. The narrative tier only:
the cube and outcomes remain the fact authority.

Tracked = company_pages.yaml entries ∪ market_tracker.txt names (the same
populations promote_demote reads). Idempotent per org: an org whose memory
LOG already holds records is skipped, so re-runs are no-ops.

Usage:
    build_baseline_import.py --profile lj [--dry-run]
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

import jsonl_io
import config
import mem as mem_mod
from company_events import canonical_company, norm_key, tally
from promote_demote import _load_yaml_list, _tracker_names

TIER_RE = re.compile(r"cube-promotion tier ([AB])")


def _applied_counts(state_root: Path) -> dict[str, int]:
    """org norm-key → number of application-ish outcome rows."""
    path = state_root / "dataset" / "outcomes.jsonl"
    counts: dict[str, int] = {}
    if not path.is_file():
        return counts
    for line in jsonl_io.lines(path):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("kind") not in ("applied", "application_sent"):
            continue
        org = row.get("org_name") or row.get("org") or ""
        key = norm_key(canonical_company(str(org)) or str(org)) or str(org)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def baseline_lines(state_root: Path, repo_root: Path, principal: str,
                   today: date | None = None) -> list[tuple[str, str]]:
    """[(org display name, baseline note)] for every tracked org, sorted."""
    today = today or date.today()
    isodate = today.isoformat()

    yaml_list = _load_yaml_list(repo_root / "profiles" / principal
                                / "company_pages.yaml")
    tracker_names = _tracker_names(repo_root / "profiles" / principal
                                   / "market_tracker.txt")

    cube = tally(principal=principal)
    cube_by_key: dict[str, dict] = {}
    display_by_key: dict[str, str] = {}
    for (company, name), cell in cube.items():
        if name != principal:
            continue
        cube_by_key[norm_key(company)] = cell
        display_by_key[norm_key(company)] = company

    applied = _applied_counts(state_root)

    # tracked population: display name + tier hint, keyed by norm-key
    tracked: dict[str, tuple[str, str]] = {}
    for entry in yaml_list:
        name = entry.get("name") or ""
        key = norm_key(canonical_company(name) or name)
        tier = ""
        m = TIER_RE.search(str(entry.get("reason") or ""))
        if m:
            tier = "tier %s, " % m.group(1)
        tracked[key] = (display_by_key.get(key) or name, tier)
    for name in tracker_names:
        key = norm_key(canonical_company(name) or name)
        tracked.setdefault(key, (display_by_key.get(key) or name, ""))

    out: list[tuple[str, str]] = []
    for key, (name, tier) in sorted(tracked.items(),
                                    key=lambda kv: kv[1][0].casefold()):
        cell = cube_by_key.get(key)
        k = applied.get(key, 0)
        if cell:
            out.append((name,
                        "baseline: %d events since %s, applied %dx, "
                        "last event %s, %sbest %.1f" % (
                            cell["events"], cell["first"] or "?", k,
                            cell["last"] or "?", tier,
                            cell["best_score"]
                            if isinstance(cell["best_score"], (int, float))
                            else 0.0)))
        else:
            out.append((name,
                        "baseline: tracked since %s, no crawl events yet"
                        % isodate))
    return out


def run(state_root: Path, repo_root: Path, principal: str, *,
        dry_run: bool = False, today: date | None = None) -> dict:
    today = today or date.today()
    lines = baseline_lines(state_root, repo_root, principal, today)
    stats = {"orgs": len(lines), "noted": 0, "skipped_existing": 0,
             "no_memory": 0}
    for name, note in lines:
        if dry_run:
            print("%s\t%s" % (name, note))
            continue
        memdir = mem_mod.memory_dir(principal, org=name)
        log = memdir / "LOG.txt"
        if log.exists() and log.stat().st_size > 0:
            stats["skipped_existing"] += 1
            continue  # idempotent: a memory that exists stays as authored
        rc = mem_mod.run_memo(memdir, ["init"], quiet=True)
        if rc != 0:
            stats["no_memory"] += 1
            continue
        rc = mem_mod.run_memo(memdir, ["note", note], quiet=True)
        if rc == 0:
            stats["noted"] += 1
        else:
            stats["no_memory"] += 1
    return stats


def _main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    state_root = config.STATE_DIR / args.profile
    if not state_root.is_dir():
        print("no state dir: %s" % state_root, file=sys.stderr)
        return 1
    repo_root = config.LOVEWORK_ROOT
    stats = run(state_root, repo_root, args.profile, dry_run=args.dry_run)
    if not args.dry_run:
        print("%(orgs)s tracked orgs: %(noted)s baselined, "
              "%(skipped_existing)s already had memory, "
              "%(no_memory)s failed" % stats)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
