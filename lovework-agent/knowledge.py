"""knowledge.py — the outcome vocabulary and the per-principal split.

**One definition, shared by everything that talks about an outcome.** The
crawler's outcome scanner, the knowledge extractor and the checks in `docs/53`
all use these names; a stray spelling must not become a seventh category, and a
test directory must not become an application.

**One rule about where data lives** (LJ, 2026-11-10): *data derived from a
principal's record belongs to that principal's repo; the method, schemas, prompts
and checks are general and belong to the engine.* `split_by_principal()` is the
one function that applies it, so a merged corpus can be re-split at any time
without a second opinion about which row is whose.

The corpora themselves are `lovework-principal-<p>/knowledge/{cases,lessons,
guidance,outcomes}.jsonl`; this module writes them and reads them, and never
edits a source case.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

#: The only outcomes. `prepared` means material exists; `unknown` means nothing
#: was recorded. Silence is never a `rejected`.
OUTCOMES = ("applied", "rejected", "interview", "offer", "prepared", "unknown")

#: Spellings the extractors produce, folded onto the one vocabulary.
ALIASES = {
    "rejection": "rejected", "reject": "rejected", "no": "rejected",
    "declined": "rejected", "unsuccessful": "rejected",
    "interviewing": "interview", "screen": "interview",
    "hired": "offer", "accepted": "offer",
    "sent": "applied", "submitted": "applied", "app": "applied",
    "draft": "prepared", "scaffold": "prepared",
    "": "unknown", "none": "unknown", "?": "unknown",
}

#: Directory or company names that are fixtures, not applications.
FIXTURE_RE = re.compile(
    r"(^|[-_:])(testco|test-?case|fixture|dummy|example|sample|demo)([-_:]|$)", re.I)

#: Knowledge kinds that are general (the engine's, not a principal's): a pattern,
#: a check or a method holds for any principal, so it must not be filed under one.
GENERAL_KINDS = frozenset({"pattern", "anti_pattern", "self_healing_check", "meta_method"})


def normalise(value: str) -> str:
    """Fold a label onto the outcome vocabulary; unknown spellings pass through."""
    text = (value or "").strip().lower()
    return ALIASES.get(text, text)


def is_known(value: str) -> bool:
    return normalise(value) in OUTCOMES


def is_fixture(*parts: str) -> bool:
    """True when a case name or company is a test fixture, not an application."""
    return any(FIXTURE_RE.search(p or "") for p in parts)


def case_principal(case: dict, *, default: str = "lj") -> str:
    """Which principal a merged case row belongs to."""
    principal = str(case.get("principal") or "").strip().lower()
    if principal:
        return principal
    return default if case.get("kind") == "manual" else ""


def lessons_of(case: dict) -> list[dict]:
    """Every lesson a case carries, from either run, without duplicates.

    A case both runs read has `lessons_host-b` and `lessons_host-a`; a case only
    one run read has the merged `lessons`. Prefer the split form, so a lesson is
    never counted twice.
    """
    rows: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, text: str, confidence: str, quote: str, stream: str) -> None:
        key = (text or "").strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        rows.append({"case": case.get("case", ""), "principal": case_principal(case),
                     "stream": stream, "kind": kind, "text": text,
                     "confidence": confidence, "quote": quote,
                     "date": case.get("date", "")})

    if case.get("lessons_host-b") or case.get("lessons_host-a"):
        for lesson in case.get("lessons_host-b") or []:
            add(lesson.get("kind", ""), lesson.get("text", ""),
                lesson.get("confidence", ""), lesson.get("evidence", ""), "host-b")
        for lesson in case.get("lessons_host-a") or []:
            add("case_lesson", lesson.get("text", ""), "", lesson.get("quote", ""), "host-a")
    else:
        for lesson in case.get("lessons") or []:
            add(lesson.get("kind", ""), lesson.get("text", ""),
                lesson.get("confidence", ""), lesson.get("evidence", ""), "host-b")
    return rows


def _load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def split_by_principal(principal: str, merged_dir: Path, dest: Optional[Path] = None,
                       *, apply: bool = True) -> dict:
    """Write one principal's knowledge from a merged corpus.

    Reads `merged_dir/{cases-final,knowledge,outcomes}.jsonl` and writes
    `dest/{cases,lessons,guidance,outcomes}.jsonl` + `stats.json`. Fixtures are
    dropped from every output (they stay in the merged corpus, flagged). Guidance
    is kept only for the principal whose own words it is; general knowledge kinds
    never enter a principal's directory.
    """
    cases = [c for c in _load(merged_dir / "cases-final.jsonl")
             if not c.get("fixture") and case_principal(c) == principal]
    names = {c.get("case", "") for c in cases}
    knowledge = _load(merged_dir / "knowledge.jsonl")
    outcomes = [o for o in _load(merged_dir / "outcomes.jsonl") if o.get("case") in names]

    def link(k: dict) -> bool:
        short = str(k.get("case_id") or "").split(":", 1)[-1]
        return any(short == n.split(":", 1)[-1] for n in names)

    guidance = [k for k in knowledge
                if k.get("kind") not in GENERAL_KINDS
                and (link(k) or (principal == "lj" and not k.get("case_id")))]
    lessons = [l for c in cases for l in lessons_of(c)]

    stats = {
        "principal": principal,
        "cases": len(cases),
        "lessons": len(lessons),
        "guidance": len(guidance),
        "outcomes": len(outcomes),
        "outcome_counts": dict(collections.Counter(c.get("outcome") for c in cases)),
        "source": str(merged_dir),
    }
    if apply:
        dest = dest or Path.home() / "lovework" / f"lovework-principal-{principal}" / "knowledge"
        dest.mkdir(parents=True, exist_ok=True)
        for filename, rows in (("cases.jsonl", cases), ("lessons.jsonl", lessons),
                               ("guidance.jsonl", guidance), ("outcomes.jsonl", outcomes)):
            (dest / filename).write_text(
                "\n".join(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows)
                + ("\n" if rows else ""), encoding="utf-8")
        (dest / "stats.json").write_text(json.dumps(stats, indent=1, sort_keys=True), encoding="utf-8")
        stats["written"] = str(dest)
    return stats


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--principal", required=True)
    parser.add_argument("--merged", required=True, help="directory holding the merged corpus")
    parser.add_argument("--dest", default="", help="override the principal knowledge dir")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    stats = split_by_principal(args.principal, Path(args.merged),
                               Path(args.dest) if args.dest else None,
                               apply=not args.dry_run)
    print(json.dumps(stats, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
