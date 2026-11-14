#!/usr/bin/env python3
"""jsonl_io.py — the one correct way to read a line-delimited ledger.

**Never `path.read_text().splitlines()`.** `str.splitlines()` breaks on the
Unicode line separators a JSON string may legally contain — `\u2028`, `\u2029`,
`\x0b`, `\x0c`, `\x1c`, `\x1d`, `\x1e`, `\x85` — and these ledgers are written
with `json.dumps(..., ensure_ascii=False)`, so a model answer that contains one
puts it in the file *literally*. A reader that splits on it sees two invalid
fragments instead of one row, and a reader that skips unparseable lines loses
both halves silently.

Measured, 2026-10-13: `status.py --llm-usage` was dropping 37 rows of a ledger
that way; the same class corrupted a 126 MB ledger when a repair rewrote it with
`splitlines()` + `"\n".join(...)` (2026-10-11: 7,012 valid rows became 6,963 +
50 fragments). One rule, one helper, every reader.

    from jsonl_io import lines, rows, fragments

    for row in rows(path):          # parsed, physical line by physical line
        ...
    if fragments(path):             # the detector: lines that are not JSON
        ...
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


def lines(path: Path | str) -> list[str]:
    """The file's physical lines, split on newline and nothing else."""
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        return [line.rstrip("\n") for line in handle]


def rows(path: Path | str, *, skip_blank: bool = True) -> Iterator[dict]:
    """Yield each parsed row. Raises `ValueError` naming the line it cannot read.

    Loud on purpose: a ledger reader that drops what it cannot parse is how 37
    rows went missing without a word. A caller that wants to tolerate damage
    should ask `fragments()` first and say so.
    """
    for number, line in enumerate(lines(path), 1):
        if skip_blank and not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{Path(path).name} line {number}: {exc}") from exc


def fragments(path: Path | str) -> list[str]:
    """The physical lines that are not valid JSON — the class's own detector.

    A non-empty answer means either a torn write or a reader-visible separator
    got into the file; either way the row count the ledger claims is not the row
    count a reader sees.
    """
    out: list[str] = []
    for number, line in enumerate(lines(path), 1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            out.append(f"line {number}: {exc}")
    return out
