"""Union-resolve two diverged OptMem LOGs (PLAN.md T2.6).

The scenario: the watch cron on host-a and a session on host-b both
appended notes to the same memory before a git pull — LOG.txt conflicts.
Records are whole fixed-width lines appended at the same base, so the
resolution is a union, never a content merge:

  1. keep the identical common prefix (both sides' shared history);
  2. merge the two tails sorted by date (theirs first on equal dates);
  3. renumber the ids sequentially — position is identity, and a union
     re-assigns positions;
  4. wipe TREE/ — summaries are a cache keyed by record position, so a
     renumbered log invalidates them; the next nap ritual rebuilds (the
     tool's own `forget` does the same, just one node at a time).

Nothing from either side's LOG is ever dropped. Exit is loud: non-record
bytes at a file tail (a torn append) are truncated the way `memo repair`
would, and reported.

Usage:
    resolve_mem_conflict.py <memory_dir> <theirs_LOG> <ours_LOG> [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def load_memo_module():
    import importlib.machinery
    import importlib.util
    import config
    memo = config.AGENT_ROOT / "tools" / "optmem" / "memo"
    loader = importlib.machinery.SourceFileLoader("optmem_memo", str(memo))
    spec = importlib.util.spec_from_loader("optmem_memo", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def read_records(mod, path: Path) -> tuple[list[tuple[str, str]], int]:
    """(date, text) per whole record; torn trailing bytes are dropped and
    counted — the same repair rule memo applies on its own appends."""
    data = path.read_bytes() if path.exists() else b""
    rec = mod.LOG_REC
    whole, extra = divmod(len(data), rec)
    records = []
    for i in range(whole):
        line = data[i * rec:(i + 1) * rec].rstrip(b"\n").rstrip()
        text = line.decode("utf-8", "replace")
        body = text.split(" ", 2)  # "#<id> <date> <text…>"
        if len(body) == 3:
            records.append((body[1], body[2]))
    return records, extra


def union(mod, theirs: list[tuple[str, str]],
          ours: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], dict]:
    stats = {"prefix": 0, "theirs_only": 0, "ours_only": 0}
    i = 0
    while i < len(theirs) and i < len(ours) and theirs[i] == ours[i]:
        i += 1
    stats["prefix"] = i
    merged = list(theirs[:i])
    t, o = theirs[i:], ours[i:]
    stats["theirs_only"], stats["ours_only"] = len(t), len(o)
    # merge-join by date; each tail is date-ordered by construction, theirs
    # first on equal dates so the resolution is deterministic
    a = b = 0
    while a < len(t) and b < len(o):
        if t[a][0] <= o[b][0]:
            merged.append(t[a]); a += 1
        else:
            merged.append(o[b]); b += 1
    merged.extend(t[a:]); merged.extend(o[b:])
    return merged, stats


def write_log(mod, path: Path, records: list[tuple[str, str]]) -> None:
    with path.open("wb") as fh:
        for k, (d, text) in enumerate(records):
            fh.write(mod.pad("#%d %s %s" % (k, d, text), mod.LOG_REC))
        fh.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("memory_dir")
    parser.add_argument("theirs")
    parser.add_argument("ours")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    mod = load_memo_module()
    memdir = Path(args.memory_dir)
    theirs, torn_t = read_records(mod, Path(args.theirs))
    ours, torn_o = read_records(mod, Path(args.ours))
    merged, stats = union(mod, theirs, ours)
    print("common prefix %(prefix)s records, +%(theirs_only)s theirs, "
          "+%(ours_only)s ours → %(total)s total" % {**stats,
                                                      "total": len(merged)})
    if torn_t or torn_o:
        print("torn trailing bytes dropped: theirs %d, ours %d "
              "(memo repair applies the same rule)"
              % (torn_t, torn_o))
    if args.dry_run:
        for k, (d, text) in enumerate(merged):
            print("#%d %s %s" % (k, d, text))
        return 0

    write_log(mod, memdir / "LOG.txt", merged)
    wiped = 0
    tree = memdir / "TREE"
    if tree.is_dir():
        for f in tree.iterdir():
            if f.is_file():
                f.unlink()
                wiped += 1
    print("LOG rewritten (ids renumbered, nothing dropped); "
          "%d TREE cache files wiped — naps will rebuild them" % wiped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
