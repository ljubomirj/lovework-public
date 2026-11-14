#!/usr/bin/env python3
"""Normalise the org-memory directory names (2026-10-05).

The historic curation wrote each org memory under the ledger's `org_name`
verbatim, and that field is machine-extracted: spaces, commas, parentheses,
accents, appended locations, and a few rows that are not organisations at all
("Machine Learning Engineer", "Near you", a test fixture). LJ: *"use names
user- and filesystem-friendly, don't use only machine friendly name… we just
need to put some extra care."*

Rules live in `mem.slug` (the one naming authority). This script applies them to
what already exists:

  * **rename** — `Dassault Systèmes` → `Dassault-Systemes`,
    `Advanced Machine Intelligence ` → `Advanced-Machine-Intelligence`;
  * **merge** — a name that folds onto an existing memory (`Le 001 Leerink
    Partners New York, NY` and `Leerink Partners New York, NY` →
    `Leerink-Partners`): logs are unioned in date order, ids renumbered, the
    derived TREE dropped (rebuildable), and the source directory moved aside —
    never deleted;
  * **attic** — ledger noise that is not an org (`mem.NOT_AN_ORG`): the memory
    moves out of `orgs/` into `_attic/`, so the tool no longer reads it, and the
    record survives.

Report-only by default; `--apply` performs it. Renames and merges leave a
`renamed:`/`merged:` note inside the memory, so the LOG itself carries the
origin of the new name.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import mem as memmod  # noqa: E402

RECORD_RE = re.compile(r"^#(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(.*)$")


def _read_records(log: Path) -> list[tuple[str, str]]:
    """[(date, text)] from one LOG, in file order."""
    out = []
    if not log.is_file():
        return out
    for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = RECORD_RE.match(line.strip())
        if m:
            out.append((m.group(2), m.group(3).strip()))
    return out


def _tool_module():
    """The vendored memo, loaded so this script uses its *own* record geometry
    (`LOG_REC`, `pad`) rather than a copy — a hand-rolled width silently
    misaligns the log (it did, 2026-10-05: 7 merged logs needed repair)."""
    import importlib.machinery
    import importlib.util
    memo = Path(__file__).resolve().parent / "tools" / "optmem" / "memo"
    loader = importlib.machinery.SourceFileLoader("optmem_memo_norm", str(memo))
    spec = importlib.util.spec_from_loader("optmem_memo_norm", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _write_records(log: Path, records: list[tuple[str, str]]) -> None:
    """Rewrite a LOG with ids renumbered from 0, using the tool's own padding."""
    mod = _tool_module()
    log.write_bytes(b"".join(mod.pad(f"#{i} {day} {text}", mod.LOG_REC)
                             for i, (day, text) in enumerate(records)))


def _plan(principal: str, orgs: Path) -> list[dict]:
    moves = []
    for d in sorted(p for p in orgs.iterdir() if p.is_dir()):
        if d.name.startswith("_attic"):
            continue
        target = memmod.slug(d.name)
        if not memmod.is_org_name(d.name):
            moves.append({"action": "attic", "from": d, "to": None,
                          "why": "not an organisation (ledger noise)"})
        elif not target:
            moves.append({"action": "keep", "from": d, "to": None,
                          "why": "slug folds to nothing — left alone"})
        elif target == d.name:
            continue
        else:
            dest = orgs / target
            moves.append({"action": "merge" if dest.exists() else "rename",
                          "from": d, "to": dest,
                          "why": "already a memory" if dest.exists() else ""})
    # nested residue: a directory *inside* a memory (other than the derived TREE)
    # is a fragment of that memory's own name — pre-slug era, when an org name
    # containing "/" was used as a path (e.g. the keel rows' combined names).
    # Appended last so renames above land first; the path is re-resolved at apply.
    for mem in sorted(p for p in orgs.iterdir() if p.is_dir() and not p.name.startswith("_")):
        for child in sorted(p for p in mem.iterdir() if p.is_dir()):
            if child.name == "TREE" or child.name.startswith("_"):
                continue
            moves.append({"action": "merge-nested", "from": mem / child.name,
                          "to": orgs / (memmod.slug(mem.name) or mem.name),
                          "child_name": child.name,
                          "why": f"nested residue inside {mem.name!r} (pre-slug name)"})
    return moves


def _apply_one(principal: str, move: dict, attic: Path, *, dry: bool) -> None:
    src, dest = move["from"], move["to"]
    if move["action"] == "attic":
        target = attic / src.name
        if not dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target = attic / f"{src.name}.{date.today():%Y%m%d}"
            shutil.move(str(src), str(target))
        return
    if move["action"] == "merge-nested":
        mem = move["to"]
        src = mem / move["child_name"]        # re-resolved: the parent may have been renamed
        if not mem.is_dir() or not src.is_dir():
            return
        if dry:
            return                            # a dry run reports, never moves
        if (src / "LOG.txt").is_file():
            ours = _read_records(src / "LOG.txt")
            theirs = _read_records(mem / "LOG.txt")
            merged = sorted(theirs + ours, key=lambda r: r[0])   # date order, stable
            _write_records(mem / "LOG.txt", merged)
            _note(mem, f"merged: nested {move['child_name']!r} folded into this "
                       f"memory (one entity, pre-slug name, 2026-10-05)")
            shutil.rmtree(mem / "TREE", ignore_errors=True)
            (mem / "TREE").mkdir(exist_ok=True)
        keep = attic / f"merged-nested-{move['child_name'].strip()}"
        keep.parent.mkdir(parents=True, exist_ok=True)
        if keep.exists():
            keep = keep.with_name(f"{keep.name}.{date.today():%Y%m%d}")
        shutil.move(str(src), str(keep))
        return
    # decide at apply time, not plan time: a sibling earlier in this same run may have
    # just created this target, which turns a rename into a merge
    action = move["action"]
    if action == "rename" and dest.exists():
        action = "merge"
    if action == "rename":
        if not dry:
            src.rename(dest)
            _note(dest, f"renamed: {src.name!r} → {dest.name!r} (filesystem-friendly slug policy, 2026-10-05)")
        return
    if action == "merge":
        ours = _read_records(src / "LOG.txt")
        theirs = _read_records(dest / "LOG.txt")
        if dry:
            return
        merged = sorted(theirs + ours, key=lambda r: r[0])       # date order, stable
        _write_records(dest / "LOG.txt", merged)
        _note(dest, f"merged: {src.name!r} folded into this memory (same organisation, 2026-10-05)")
        # the derived tree no longer matches the log: drop it (rebuildable)
        shutil.rmtree(dest / "TREE", ignore_errors=True)
        (dest / "TREE").mkdir(exist_ok=True)
        keep = attic / f"merged-{src.name}"
        keep.parent.mkdir(parents=True, exist_ok=True)
        if keep.exists():
            keep = keep.with_name(f"{keep.name}.{date.today():%Y%m%d}")
        shutil.move(str(src), str(keep))


def _note(memdir: Path, line: str) -> None:
    """Append one note through the tool (never by editing the LOG)."""
    memmod.run_memo(memdir, ["note", line], quiet=True)


def apply_for_profile(principal: str, *, apply: bool) -> int:
    """The programmatic entry selfheal rides (2026-10-22): plan + apply for one
    principal, return the move count. Same code path as the CLI, one door."""
    orgs = config.STATE_DIR / principal / "dataset" / "memory" / "orgs"
    if not orgs.is_dir():
        return 0
    moves = _plan(principal, orgs)
    done = 0
    for move in moves:
        try:
            _apply_one(principal, move, orgs / "_attic", dry=not apply)
            done += 1
        except OSError:
            continue               # one bad move must not stop the rest
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Normalise org-memory directory names")
    ap.add_argument("--profile", default="all")
    ap.add_argument("--apply", action="store_true", help="perform (default: report only)")
    args = ap.parse_args(argv)

    profiles = ([args.profile] if args.profile != "all"
                else sorted(p.name for p in config.STATE_DIR.iterdir()
                            if (p / "dataset" / "memory" / "orgs").is_dir()))
    total = 0
    for principal in profiles:
        orgs = config.STATE_DIR / principal / "dataset" / "memory" / "orgs"
        if not orgs.is_dir():
            continue
        moves = _plan(principal, orgs)
        print(f"== {principal}: {len(moves)} of {len([d for d in orgs.iterdir() if d.is_dir()])} memories need work")
        for move in moves:
            arrow = f"→ {move['to'].name}" if move["to"] else f"→ _attic/{move['from'].name}"
            print(f"   {move['action']:<7} {move['from'].name!r} {arrow}"
                  + (f"  ({move['why']})" if move["why"] else ""))
            try:
                _apply_one(principal, move, orgs / "_attic", dry=not args.apply)
                total += 1
            except OSError as exc:                    # one bad move must not stop the rest
                print(f"   ! failed: {exc}", file=sys.stderr)
    print(f"{'applied' if args.apply else 'would apply'}: {total} move(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
