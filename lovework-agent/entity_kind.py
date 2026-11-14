#!/usr/bin/env python3
"""entity_kind.py — record **what kind of organisation** a wiki page is about.

LJ, 2026-10-15: *"the list of agencies should not be in the code … We should have
separate wiki pages for the agencies, separate from the companies? So we keep
history, but we can also distinguish company from agency."* and *"we should not
hard-code as there will be more. Best we treat it as a different type of entity,
separate from principal (candidate) and company (hiring)."*

So the kind is **data with provenance**, not a constant:

* the decision lives in a marker on the organisation's own page —
  `<!-- lovework:entity: agency | set 2026-10-15 by seed -->` — so it travels with
  the page through every seal and every host merge, and it can say who decided and
  when (`lead_identity.ENTITY_MARKER_RE`);
* the seed list lives in `profiles/<principal>/known_agencies.txt`, a file the
  principal edits;
* the rules (`lead_identity`) only ever **propose**, and this tool is what turns a
  proposal into a recorded fact.

Report-only by default. `--apply` writes markers for the proposals; `--set ORG=KIND`
records the principal's own decision (the escape hatch the automatic rules cannot
provide).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from lead_identity import (ENTITY_KINDS, entity_marker, org_kind,
                           recorded_entity_kind)
from wiki_store import org_page_paths

#: The seed list: one organisation name per line, `#` comments allowed. Principal
#: files, because "is this an agency?" is a judgement about the world that a
#: principal may want to correct without a code change.
SEED_NAME = "known_agencies.txt"

#: Where the marker goes: directly under the page's H1, above everything the page
#: says — a reader who opens the page learns what kind of organisation it is first.
def _insert_after_h1(text: str, marker: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# "):
            rest = lines[index + 1:]
            while rest and not rest[0].strip():
                rest.pop(0)
            return "\n".join(lines[:index + 1] + ["", marker] + rest) + (
                "\n" if text.endswith("\n") else "")
    return marker + "\n" + text


def seed_names(principal: str) -> set[str]:
    """The principal's own list of intermediaries, normalised for comparison."""
    path = config.PROFILES_DIR / principal / SEED_NAME
    names: set[str] = set()
    if not path.is_file():
        return names
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(re.sub(r"[^a-z0-9]", "", line.lower()))
    return names


def _careers_domains(principal: str) -> dict[str, str]:
    """org → the domain we have seen it publish on, from the registry."""
    registry = config.STATE_DIR / principal / "cache" / "jobs.csv"
    domains: dict[str, str] = {}
    if not registry.is_file():
        return domains
    try:
        with registry.open(encoding="utf-8", errors="replace", newline="") as handle:
            for row in csv.DictReader(handle):
                org = str(row.get("org") or "").strip()
                if not org or org in domains:
                    continue
                for field in ("careers_url", "url"):
                    found = re.match(r"https?://([^/]+)", str(row.get(field) or ""))
                    if found:
                        domains[org] = found.group(1).lower()
                        break
    except OSError:
        pass
    return domains


def proposals(principal: str) -> list[dict]:
    """What each organisation's page should record, and why."""
    from wiki_store import org_page_paths

    pages = org_page_paths(config.STATE_DIR / principal / "wiki")
    if not pages:
        return []
    seed = seed_names(principal)
    domains = _careers_domains(principal)
    out: list[dict] = []
    for page in pages:
        try:
            text = page.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        h1 = re.search(r"(?m)^# (.+)$", text)
        org = h1.group(1).strip() if h1 else page.stem.replace("_", " ")
        recorded = recorded_entity_kind(text)
        compact = re.sub(r"[^a-z0-9]", "", org.lower())
        by = ""
        kind = ""
        if compact in seed:
            kind, by = "agency", "seed"
        else:
            guess = org_kind(org, f"https://{domains.get(org, '')}" if domains.get(org) else "")
            if guess != "company":
                kind, by = guess, "rule"
        out.append({"page": page, "org": org, "recorded": recorded,
                    "proposed": kind, "by": by})
    return out


def apply_proposals(principal: str, *, today: date | None = None,
                    dry_run: bool = False) -> dict:
    today = today or date.today()
    result = {"principal": principal, "written": 0, "already": 0, "unchanged": 0,
              "rows": [], "dry_run": dry_run}
    for row in proposals(principal):
        if row["recorded"]:
            result["already"] += 1
            continue
        if not row["proposed"]:
            continue                       # nothing to say: a company by default
        result["rows"].append(row)
        if dry_run:
            continue
        marker = entity_marker(row["proposed"], set_on=today.isoformat(),
                               by=row["by"])
        try:
            text = row["page"].read_text(encoding="utf-8")
            row["page"].write_text(_insert_after_h1(text, marker), encoding="utf-8")
            result["written"] += 1
        except OSError as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"[:120]
    return result


def set_kind(principal: str, org: str, kind: str, *, today: date | None = None,
             by: str = "principal") -> dict:
    """Record the principal's own decision about one organisation."""
    if kind not in ENTITY_KINDS:
        return {"error": f"kind must be one of {', '.join(ENTITY_KINDS)}"}
    today = today or date.today()
    wanted = re.sub(r"[^a-z0-9]", "", org.lower())
    for page in org_page_paths(config.STATE_DIR / principal / "wiki"):
        text = page.read_text(encoding="utf-8", errors="ignore")
        h1 = re.search(r"(?m)^# (.+)$", text)
        name = h1.group(1).strip() if h1 else page.stem.replace("_", " ")
        if re.sub(r"[^a-z0-9]", "", name.lower()) != wanted:
            continue
        marker = entity_marker(kind, set_on=today.isoformat(), by=by)
        if recorded_entity_kind(text):
            text = re.sub(r"<!--\s*lovework:entity:.*?-->", marker, text, count=1)
        else:
            text = _insert_after_h1(text, marker)
        page.write_text(text, encoding="utf-8")
        return {"principal": principal, "org": name, "kind": kind, "page": str(page)}
    return {"error": f"no org page for {org!r}"}


def move_agencies(principal: str, *, dry_run: bool = False) -> dict:
    """Move every page whose **recorded kind** is an intermediary into `agencies/`.

    LJ, 2026-10-15: *"Let us do the separation next - separate them move them into
    wiki/agencies."*

    The move is safe by construction, and that is the point of doing it after the
    marker rather than before:

    * the **kind** stays on the page, so nothing depends on the directory to know
      what an organisation is;
    * every reader asks `wiki_store.org_page_paths` / `org_page_for`, which look in
      **both** directories — a reader that globbed only `orgs/` would have reported a
      cleaner tree the moment the agencies left, which is the "the audit got
      quieter, not cleaner" failure this project already paid for;
    * a link written before the move (`orgs/X.md`) still resolves, because the
      serving resolver falls back to the sibling directory.

    Idempotent: a page already in `agencies/` is not a candidate.
    """
    from wiki_store import AGENCIES_DIRNAME, ORGS_DIRNAME, org_page_dirs

    wiki_root = config.STATE_DIR / principal / "wiki"
    orgs_dir, agencies_dir = org_page_dirs(wiki_root)
    out = {"principal": principal, "moved": [], "already": 0, "dry_run": dry_run}
    if not orgs_dir.is_dir():
        return out
    if not dry_run:
        agencies_dir.mkdir(parents=True, exist_ok=True)
    for page in sorted(orgs_dir.glob("*.md")):
        try:
            kind = recorded_entity_kind(page.read_text(encoding="utf-8",
                                                       errors="ignore"))
        except OSError:
            continue
        if kind not in ("agency", "board"):
            continue
        destination = agencies_dir / page.name
        if destination.exists():
            out["already"] += 1
            continue
        out["moved"].append(page.name)
        if dry_run:
            continue
        page.rename(destination)
    _ = ORGS_DIRNAME
    return out


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--set", default="", help="ORG=KIND, the principal's own call")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--move-agencies", action="store_true",
                        help="move every agency/board page into wiki/agencies/")
    args = parser.parse_args()
    if args.set:
        org, _, kind = args.set.partition("=")
        outcome = set_kind(args.profile, org.strip(), kind.strip().lower())
        print(outcome)
        return 0 if "error" not in outcome else 1
    if args.move_agencies:
        moved = move_agencies(args.profile, dry_run=args.dry_run)
        print(f"{args.profile}: {len(moved['moved'])} page(s) to wiki/agencies/"
              + (" (dry run)" if args.dry_run else "")
              + f" · {moved['already']} already there")
        for name in moved["moved"][:8]:
            print(f"   {name}")
        return 0
    result = apply_proposals(args.profile, dry_run=args.dry_run or not args.apply)
    if args.json:
        import json

        print(json.dumps({**result, "rows": [r["org"] for r in result["rows"]]},
                         indent=2, sort_keys=True))
        return 0
    print(f"{args.profile}: {result['written']} written"
          + (" (dry run)" if result["dry_run"] else "")
          + f" · {result['already']} already recorded · {len(result['rows'])} proposed")
    for row in result["rows"][:10]:
        print(f"   {row['org'][:40]:42s} -> {row['proposed']:7s} by {row['by']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
