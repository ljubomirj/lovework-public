#!/usr/bin/env python3
"""Keep the project's learning logs readable: one order, no manual wrapping.

LJ, 2026-10-08: "Looks like we have plethora of conventions when writing into
JOURNAL and LEARNINGS .md files. Sometimes append at top, other times at bottom.
Sometimes no 72 chars linebreaks, only paragraph breaks, other times 72 line
breaks."

The convention this tool enforces, and the one the lovework skill teaches:

  1. **Append new entries at the TAIL.** Newest last, oldest first. Appending at
     the top reads well for the first week and then descends into chaos: two
     writers adding "at the top" fight over the same lines, and every merge
     resolves into an interleaving nobody can read.
  2. **No manual line breaks inside a paragraph.** A paragraph is one line; the
     reader's view component wraps it. Hard 72-column wrapping was a terminal
     convention from when the screen was 80 columns — it survives in these files
     as noise that makes every diff touch the whole paragraph.
  3. **A blank line separates paragraphs.** One newline ends nothing; two do.

`--check` reports; `--fix` rewrites. Both are idempotent: running the fix twice
changes nothing, which is what makes it safe to wire into a daily pass.

Usage:
    logs_lint.py [--fix] [--json] [JOURNAL.md LEARNINGS.md ...]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

#: A line that starts a structural element and must never be joined.
MARKER_RE = re.compile(r"^(#|\s|[-*+>|]\s|\d+[.)]\s|_|```|~~~)")
DATE_HEAD_RE = re.compile(r"^(?:- \*\*|## )?(?:.*?)(\d{4}-\d{2}-\d{2})")
TRAILING_DATE_RE = re.compile(r"\((\d{4}-\d{2}-\d{2})\)\s*$")


#: What starts an entry, per file. JOURNAL entries are `- **<date> …**` bullets;
#: LEARNINGS entries are `## <title> (<date>)` sections — and LEARNINGS bodies
#: are full of `- **Friction:**` bullets, so a shared pattern would split one
#: entry into a dozen and a "sort" would scramble the file.
ENTRY_RE = {
    "JOURNAL.md": r"(?m)^- \*\*",
    "LEARNINGS.md": r"(?m)^## ",
}


def _entry_pattern(path: Path, text: str) -> str:
    if path.name in ENTRY_RE:
        return ENTRY_RE[path.name]
    return r"(?m)^## " if len(re.findall(r"(?m)^## ", text)) >= 5 else r"(?m)^- \*\*"


def _split(text: str, pattern: str) -> tuple[str, list[str]]:
    starts = [m.start() for m in re.finditer(pattern, text)]
    if not starts:
        return text, []
    preamble = text[:starts[0]]
    entries = [text[s:(starts[i + 1] if i + 1 < len(starts) else len(text))]
               for i, s in enumerate(starts)]
    return preamble, entries


def _entry_date(entry: str) -> date | None:
    """The entry's own date: its heading's leading date, else a trailing one."""
    head = entry.split("\n", 1)[0]
    m = DATE_HEAD_RE.match(head) or TRAILING_DATE_RE.search(head)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


TOP_MARKER_RE = re.compile(r"^(#|[-*+>|]\s|\d+[.)]\s|_|```|~~~)")
NESTED_ITEM_RE = re.compile(r"^\s+(?:[-*+]\s|\d+[.)]\s)")


def reflow(entry: str) -> str:
    """Join hard-wrapped prose lines; leave structure alone.

    Three shapes have to survive untouched: a fenced code block, a table, and a
    nested list item. Everything else is prose, and a run of prose lines is one
    paragraph — that is the convention (a blank line is the paragraph break).
    A *bullet* is prose too: its wrapped continuation lines are indented, and
    they are what the 72-column era left behind all over LEARNINGS.md.
    """
    out: list[str] = []
    buffer: list[str] = []
    in_fence = False

    def flush() -> None:
        if buffer:
            out.append(" ".join(part.strip() for part in buffer))
            buffer.clear()

    for line in entry.split("\n"):
        if line.startswith(("```", "~~~")):
            flush()
            in_fence = not in_fence
            out.append(line.rstrip())
            continue
        if in_fence:
            out.append(line.rstrip())
            continue
        if not line.strip():
            flush()
            out.append("")
            continue
        if NESTED_ITEM_RE.match(line):
            flush()
            out.append(line.rstrip())
            continue
        if TOP_MARKER_RE.match(line):
            flush()
            buffer.append(line)
            continue
        if line[:1].isspace():
            if buffer:
                buffer.append(line)
            else:
                out.append(line.rstrip())
            continue
        # prose at column 0: joins prose, never a bullet's tail
        if buffer and not TOP_MARKER_RE.match(buffer[0]):
            buffer.append(line)
        else:
            flush()
            buffer.append(line)
    flush()
    return "\n".join(out)


def normalise(path: Path, *, fix: bool = False) -> dict:
    text = path.read_text(encoding="utf-8")
    preamble, entries = _split(text, _entry_pattern(path, text))
    wrapped = 0
    for entry in entries:
        if reflow(entry) != entry:
            wrapped += 1
    dated = [(i, _entry_date(e)) for i, e in enumerate(entries)]
    ordered = [d for _, d in dated if d]
    out_of_order = sum(1 for i in range(len(ordered) - 1)
                       if ordered[i] > ordered[i + 1])
    stats = {"file": str(path), "entries": len(entries),
             "entries_needing_reflow": wrapped,
             "out_of_order_pairs": out_of_order}
    if not fix:
        return stats
    # Sort by date ascending, undated first (they are the oldest). **Stable, and
    # only by date** — the file's own order inside one day is the intended order,
    # because the convention is *append at the tail*.
    #
    # This reverses a within-day `-index` that used to run here, and the reversal was
    # wrong in a way only a second run showed: it was a **one-time migration** (the
    # files had been written newest-first for most of their life, so the first pass
    # flipped each day's group) written as if it were a standing rule. Applied twice
    # it flipped the same group back — a 2-cycle that never converged. A subagent
    # found it on 2026-10-18 by running `--fix` on `HEAD:LEARNINGS.md` twice: pass 1
    # differed by 2,070 lines, pass 2 was byte-identical to HEAD. The migration is
    # done, so the key is now `(date,)` — idempotent by construction.
    keyed = [(d or date(1970, 1, 1), i, e) for i, (e, (_, d)) in
             enumerate(zip(entries, dated))]
    keyed.sort(key=lambda row: row[0])
    rebuilt = preamble + "".join(reflow(e) for _, _, e in keyed)
    if rebuilt != text:
        path.write_text(rebuilt, encoding="utf-8")
        stats["rewritten"] = True
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", help="default: JOURNAL.md LEARNINGS.md")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", default=None)
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent
    files = [Path(f) for f in args.files] or [root / "JOURNAL.md", root / "LEARNINGS.md"]
    results = [normalise(f, fix=args.fix) for f in files if f.is_file()]
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        for r in results:
            print(f"{r['file']}: {r['entries']} entries, "
                  f"{r['entries_needing_reflow']} needing reflow, "
                  f"{r['out_of_order_pairs']} out-of-order pair(s)"
                  + ("  [rewritten]" if r.get("rewritten") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
