"""
The company-event cube — raw {date, principal, company, event} records.

Why this exists
---------------
A company shows up in our crawls, we score it, and then it is forgotten: the wiki
keeps a dossier, but nothing keeps a *count*. That count is what a promotion
decision needs — should this company join the list we crawl on every run? — so we
collect the raw events first and decide from tallies later, rather than guessing.

What it is
----------
An append-only JSONL log, one row per decision:

    state/<principal>/dataset/company_events.jsonl

with the four cube dimensions plus the evidence that produced the row:

    {"date": "2026-09-26", "principal": "lj", "company": "Lendable",
     "event": "go", "decision": "GO", "score": 9.1, "advert_hash": "…",
     "title": "AI Engineer", "source": "digitaljobs", "lifecycle": "new",
     "url": "…", "run_id": "…", "observed_at": "2026-09-26T17:30:48+00:00",
     "event_id": "…", "recorded_at": "2026-09-30T…"}

Rows are keyed by `event_id = sha1(principal|date|advert_hash|event)`, so running
the backfill twice — or a crawl that re-scores the same advert the same day —
cannot double-count.

Two jobs, one code path
-----------------------
* **history** — `backfill()` streams the dated assessment ledgers
  (`state/<p>/dataset/assessments.jsonl`, 25k+ rows from 2026-07-09 onward) and
  writes the events they imply.
* **going forward** — `append_events()` is called by `snapshot.append_assessments()`
  for every row a run writes, so every crawl feeds the cube as it runs.

Both funnel through `row_to_event` / `append_events`, so history and the present
cannot drift apart.

The date axis
-------------
`date` is when **we** decided (`observed_at`), not when the advert was posted and
not `first_seen`. A stale advert re-seen today is a decision taken today, and the
regime that made it is today's. `posted_at`/`first_seen` are kept alongside for
anyone who wants the advertised timeline (see docs/26 for the full temporal
contract); they are not the cube's date.

Tallies, not verdicts
---------------------
`tally()` aggregates the cube (per company × principal: counts by event, first and
last date, distinct dates, sources) and `candidates()` turns that into a shortlist
under explicit thresholds. What qualifies a company for the always-crawl list is a
policy decision (docs/37 §4) — this module only produces the evidence.
"""

import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("lovework-agent")

EVENTS_FILENAME = "company_events.jsonl"
ASSESSMENTS_FILENAME = "assessments.jsonl"
CUBE_DIMENSIONS = ("date", "principal", "company", "event")

#: Decision → cube event name. Anything that produced a scored finding is an event;
#: unscored rows are skipped (no decision was taken).
DECISION_EVENTS = {"GO": "go", "MAYBE": "maybe", "FLAG": "flag", "DROP": "drop"}

#: Strings that are not companies: placeholders and aggregator artifacts that leak
#: into the ledger's `org_name`. Measured on the 2026-10-01 cube (21,500 rows, 2,692
#: distinct strings): "Confidential company" x99, "CV-Library listing" x13,
#: "Jobs via eFinancialCareers" x6, "Reed" x3, and the job boards themselves when a
#: parser falls back to the site it scraped. Compared casefolded.
DROPPED_COMPANIES = frozenset({
    "confidential", "confidential company", "undisclosed", "undisclosed company",
    "unknown", "n/a", "na", "none", "not specified", "various", "private",
    "cv-library listing", "cv-library", "jobs via efinancialcareers",
    "jobs for planet", "reed", "linkedin", "indeed", "totaljobs", "glassdoor",
    "talent.com", "ziprecruiter", "jobgether", "data science jobs uk",
})

#: A scraped page string used as a name is prose or a token blob, never a company.
MAX_COMPANY_CHARS = 80
#: Real employers in this corpus run to five words outside any qualifier ("St. Jude
#: Children's Research Hospital", "Advanced Machine Intelligence / AMI Labs (Yann
#: LeCun)"), while a scraped sentence starts around eleven. Seven leaves room on both
#: sides. Words inside a parenthetical or bracket are not counted: those are
#: qualifiers ("(NPL)", "(EMBL-EBI)", "(Yann LeCun)"), and dropping a real employer is
#: the one error this filter must not make.
MAX_COMPANY_WORDS = 7
_PARENTHETICAL = re.compile(r"\([^)]*\)|\[[^\]]*\]")

#: Real names sometimes carry the company's URL — "Smarkets (https://www.smarkets.com)"
#: x20, "Snout https://snout.com/" x28, "Distru (https://www.distru.com)" x22 — so a
#: URL suffix is *stripped*, not rejected: dropping a real company would corrupt the
#: very tally the promotion policy reads.
_URL_IN_NAME = re.compile(
    r"\s*[\(\[]\s*(?:https?://|www\.)[^)\]]*[\)\]]"      # " (https://…)" / "[www.…]"
    r"|\s+https?://\S+"                                   # " https://…"
    r"|\s*[\(\[]\s*[a-z0-9.-]+\.(?:com|ai|io|co|org|net|dev)\s*[\)\]]",  # " (acme.com)"
    re.IGNORECASE,
)


def canonical_company(name: str) -> Optional[str]:
    """The company a cube row names, or None when the string is not a company.

    High precision on purpose: a false drop removes a real employer from the tallies
    that decide what we crawl, which is worse than keeping a stray row. So this only
    rejects what it can name — a placeholder, an aggregator artifact, or a string too
    long to be a company name — and otherwise cleans the string.
    """
    text = (name or "").strip()
    if not text:
        return None
    cleaned = _URL_IN_NAME.sub("", text).strip(" -–—,;")
    text = cleaned or text
    if len(text) > MAX_COMPANY_CHARS:
        return None
    if len(_PARENTHETICAL.sub(" ", text).split()) > MAX_COMPANY_WORDS:
        return None
    if text.casefold() in DROPPED_COMPANIES:
        return None
    if not any(ch.isalpha() for ch in text):
        return None
    return text


def norm_key(name: str) -> str:
    """Variant key: case, punctuation and spacing folded, so spellings merge.

    "Isomorphic Labs" and "Isomorphiclabs", "OpenAI" and "Openai", "*Fastly" and
    "Fastly" are one company each; without this the tallies under-count a company
    exactly where the promotion decision looks (17 such groups in the first cube).
    """
    return re.sub(r"[^a-z0-9]+", "", (name or "").casefold())


def _dataset_dir(principal: str, dataset_dir: Optional[Path] = None) -> Path:
    """Resolve a principal's dataset directory without importing config cycles."""
    if dataset_dir is not None:
        return Path(dataset_dir)
    import config  # local import: config reads env at import time

    return Path(config.STATE_DIR) / principal.lower() / "dataset"


def event_id(principal: str, date: str, advert_hash: str, event: str) -> str:
    """Stable key for one cube cell — makes backfill and crawls idempotent."""
    raw = "|".join((principal.lower(), date, advert_hash or "", event))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def event_date(observed_at: str, first_seen: str = "") -> str:
    """The cube's date: when we assessed, falling back to the advert's first sighting.

    Both ledgers use this order so history and future rows agree. Returns "" when
    neither is usable, and callers drop such rows rather than dating them wrongly.
    """
    for value in (observed_at, first_seen):
        if not value:
            continue
        text = str(value).strip()
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            candidate = text[:10]
            try:
                datetime.fromisoformat(candidate)
            except ValueError:
                continue
            return candidate
    return ""


def row_to_event(row: dict, principal: str) -> Optional[dict]:
    """Turn one assessment-ledger row into a cube row, or None if undecidable.

    A row with no recognised decision carries no event: absence of a decision is
    not a data point about the company (the same principle as `ABSENCE_IS_NOT_EVIDENCE`
    in the registry — an unscored advert says nothing).
    """
    decision = str(row.get("decision") or "").strip().upper()
    event = DECISION_EVENTS.get(decision)
    if not event:
        return None

    # Not every `org_name` is a company: placeholders, aggregator artifacts and
    # scraped page text leak into the ledger (docs/37 §4b). Such a string says
    # nothing about an employer, so it never becomes a cube row.
    company = canonical_company(str(row.get("org_name") or ""))
    if not company:
        return None

    date = event_date(str(row.get("observed_at") or ""), str(row.get("first_seen") or ""))
    if not date:
        return None

    advert_hash = str(row.get("advert_hash") or "")
    principal = principal.lower()
    return {
        "date": date,
        "principal": principal,
        "company": company,
        "event": event,
        "event_id": event_id(principal, date, advert_hash, event),
        "decision": decision,
        "score": row.get("combined_score", row.get("score")),
        "advert_hash": advert_hash,
        "title": str(row.get("title") or ""),
        "source": str(row.get("source") or ""),
        "lifecycle": str(row.get("lifecycle_status") or ""),
        "url": str(row.get("url") or ""),
        "run_id": str(row.get("run_id") or ""),
        "observed_at": str(row.get("observed_at") or ""),
        "posted_at": str(row.get("posted_at") or ""),
        "first_seen": str(row.get("first_seen") or ""),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _existing_ids(path: Path) -> set:
    seen = set()
    if not path.is_file():
        return seen
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if '"event_id"' not in line:
                continue
            try:
                seen.add(json.loads(line)["event_id"])
            except Exception:
                continue
    return seen


def note_memories(events: list[dict], principal: str) -> int:
    """Mirror cube listing deltas into org memories (D22, PLAN.md T2.2).

    Note-if-exists: only orgs that already have a memory (the baseline
    import creates one per tracked org) get a note, so a crawl never mints
    memories for the 1,500-company long tail. Bookkeeping contract: never
    raises, never breaks the caller; nap prompts are ignored (pipelines
    don't compress).
    """
    noted = 0
    try:
        import mem as _mem

        dirs: dict[str, Path] = {}
        for event in events:
            company = canonical_company(str(event.get("company") or "")) or ""
            if not company:
                continue
            d = dirs.get(company)
            if d is None:
                d = _mem.memory_dir(principal, org=company)
                dirs[company] = d
            if not (d / "LOG.txt").exists():
                continue  # no memory yet → the org is untracked; only the
                # baseline import or a deliberate `mem init` creates one
            line = "watch: %s (%s)" % (
                event.get("event", "?"), event.get("date", "?"))
            if _mem.run_memo(d, ["note", line], quiet=True) == 0:
                noted += 1
    except Exception as exc:  # bookkeeping is best-effort by design
        logger.info("Memory notes skipped: %s", exc)
    return noted


def append_events(
    rows: Iterable[dict],
    principal: str,
    *,
    dataset_dir: Optional[Path] = None,
) -> int:
    """Append new cube rows; returns how many were added. Never raises.

    Bookkeeping must not break a crawl (same contract as the model transcript),
    so every failure is logged and swallowed. New rows are mirrored into org
    memories (note-if-exists) — see ``note_memories``.
    """
    try:
        directory = _dataset_dir(principal, dataset_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / EVENTS_FILENAME
        seen = _existing_ids(path)
        new = []
        for row in rows:
            event = row_to_event(row, principal)
            if event is None or event["event_id"] in seen:
                continue
            seen.add(event["event_id"])
            new.append(event)
        if not new:
            return 0
        with path.open("a", encoding="utf-8") as handle:
            for event in new:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        logger.info("Company events: appended %d new rows (%s)", len(new), path)
        note_memories(new, principal)
        return len(new)
    except Exception as exc:  # bookkeeping is best-effort by design
        logger.warning("Company events not recorded: %s", exc)
        return 0


def backfill(principal: str, *, dataset_dir: Optional[Path] = None, dry_run: bool = False) -> dict:
    """Write cube rows for every decision already recorded in this principal's ledger.

    Idempotent: rows already in the cube are skipped, so this is safe to re-run as
    the ledger grows.
    """
    directory = _dataset_dir(principal, dataset_dir)
    ledger = directory / ASSESSMENTS_FILENAME
    if not ledger.is_file():
        return {"principal": principal, "ledger_rows": 0, "events": 0, "added": 0, "dry_run": dry_run}

    ledger_rows = 0
    raw_rows = []
    decidable = 0
    with ledger.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ledger_rows += 1
            raw_rows.append(row)
            if row_to_event(row, principal) is not None:
                decidable += 1

    # Pass the *ledger* rows: append_events does the conversion itself (one code
    # path for backfill and crawls, so the two can never drift apart).
    added = 0 if dry_run else append_events(raw_rows, principal, dataset_dir=directory)
    return {
        "principal": principal,
        "ledger_rows": ledger_rows,
        "events": decidable,
        "added": added,
        "dry_run": dry_run,
        "events_file": str(directory / EVENTS_FILENAME),
    }


def load_events(principal: Optional[str] = None, *, dataset_dir: Optional[Path] = None) -> list:
    """Read cube rows. With no principal, scan every principal's cube."""
    principals = [principal.lower()] if principal else _all_principals(dataset_dir)
    rows = []
    for name in principals:
        path = _dataset_dir(name, dataset_dir) / EVENTS_FILENAME
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    return rows


def _all_principals(dataset_dir: Optional[Path]) -> list:
    """Principals worth looking at: those with a dataset directory (any ledger).

    Deliberately *not* "those with a cube file" — that is chicken-and-egg for the
    first backfill, when nobody has one yet.
    """
    def _names(root: Path) -> list:
        if not root.is_dir():
            return []
        return sorted(
            p.name for p in root.iterdir()
            if (p / "dataset").is_dir() or (p / ASSESSMENTS_FILENAME).is_file()
        )

    if dataset_dir is not None:
        base = Path(dataset_dir)
        # allow either "<state>/<p>/dataset" or "<state>"
        if (base / ASSESSMENTS_FILENAME).is_file() or (base / EVENTS_FILENAME).is_file():
            return [base.parent.name]
        return _names(base)

    import config

    return _names(Path(config.STATE_DIR))


def tally(*, principal: Optional[str] = None, dataset_dir: Optional[Path] = None) -> dict:
    """Aggregate the cube: per (company, principal) → counts, span, sources, titles.

    Spelling variants are merged (`norm_key`) and the most common spelling is used as
    the display name, so "Isomorphic Labs"/"Isomorphiclabs" — or "OpenAI"/"Openai" —
    cannot split a company's evidence in half. Non-company strings are excluded here
    too, so re-reading an older cube gives the same clean answer as a fresh one.
    """
    cube = defaultdict(lambda: {
        "events": 0, "by_event": defaultdict(int), "dates": set(), "sources": set(),
        "titles": set(), "best_score": None, "first": None, "last": None,
        "names": defaultdict(int),
    })
    for row in load_events(principal, dataset_dir=dataset_dir):
        company = canonical_company(str(row.get("company") or ""))
        if company is None:
            continue  # not an employer; see canonical_company
        key = (norm_key(company), row.get("principal", ""))
        cell = cube[key]
        cell["names"][company] += 1
        cell["events"] += 1
        cell["by_event"][row.get("event", "?")] += 1
        if row.get("date"):
            cell["dates"].add(row["date"])
            cell["first"] = row["date"] if cell["first"] is None or row["date"] < cell["first"] else cell["first"]
            cell["last"] = row["date"] if cell["last"] is None or row["date"] > cell["last"] else cell["last"]
        if row.get("source"):
            cell["sources"].add(row["source"])
        if row.get("title"):
            cell["titles"].add(row["title"])
        score = row.get("score")
        if isinstance(score, (int, float)):
            cell["best_score"] = score if cell["best_score"] is None else max(cell["best_score"], score)

    out = {}
    for (key, name), cell in cube.items():
        # Most common spelling wins; ties fall back to alphabetical order, so the
        # display name is stable across runs.
        display = max(sorted(cell["names"]), key=lambda n: cell["names"][n])
        out[(display, name)] = {
            "events": cell["events"],
            "by_event": dict(cell["by_event"]),
            "distinct_dates": len(cell["dates"]),
            "first": cell["first"],
            "last": cell["last"],
            "sources": sorted(cell["sources"]),
            "titles": len(cell["titles"]),
            "best_score": cell["best_score"],
        }
    return out


def tally_exclusions(*, principal: Optional[str] = None, dataset_dir: Optional[Path] = None) -> dict:
    """What the tally refuses to count, so the rules stay arguable.

    Nothing is deleted — the cube is append-only history and the filter applies on
    read — but a filter nobody can inspect is indistinguishable from a bug, so the
    tally prints this alongside its numbers.
    """
    rows = 0
    names = defaultdict(int)
    for row in load_events(principal, dataset_dir=dataset_dir):
        raw = str(row.get("company") or "").strip()
        if canonical_company(raw) is None:
            rows += 1
            names[raw[:60]] += 1
    return {"rows": rows, "names": dict(sorted(names.items(), key=lambda kv: -kv[1]))}


def candidates(
    cube: Optional[dict] = None,
    *,
    principal: Optional[str] = None,
    dataset_dir: Optional[Path] = None,
    min_strong: int = 2,
    min_dates: int = 2,
) -> list:
    """Companies whose events argue for a re-crawl cadence, strongest first.

    The thresholds are the *policy* half of docs/37 §4 and are deliberately bare
    numbers: a company qualifies when it has at least `min_strong` GO/MAYBE events
    across at least `min_dates` distinct dates, so one lucky sweep cannot promote it.
    """
    cube = cube if cube is not None else tally(principal=principal, dataset_dir=dataset_dir)
    shortlist = []
    for (company, name), cell in cube.items():
        strong = cell["by_event"].get("go", 0) + cell["by_event"].get("maybe", 0)
        if strong < min_strong or cell["distinct_dates"] < min_dates:
            continue
        shortlist.append({
            "company": company,
            "principal": name,
            "strong": strong,
            "events": cell["events"],
            "distinct_dates": cell["distinct_dates"],
            "first": cell["first"],
            "last": cell["last"],
            "best_score": cell["best_score"],
            "by_event": cell["by_event"],
        })
    shortlist.sort(key=lambda r: (-r["strong"], -(r["best_score"] or 0), r["company"]))
    return shortlist


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Company-event cube (raw {date,principal,company,event})")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backfill", action="store_true",
                       help="write cube rows for every decision already in the assessment ledger")
    group.add_argument("--tally", action="store_true", help="aggregate the cube and print the top companies")
    group.add_argument("--candidates", action="store_true",
                       help="print the promotion shortlist (see docs/37 §4)")
    parser.add_argument("--principal", default=None, help="restrict to one principal (default: all)")
    parser.add_argument("--dataset-dir", default=None, help="override the dataset directory (tests/backfill)")
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-strong", type=int, default=2)
    parser.add_argument("--min-dates", type=int, default=2)
    args = parser.parse_args(argv)

    dataset_dir = Path(args.dataset_dir) if args.dataset_dir else None
    principals = [args.principal] if args.principal else _all_principals(dataset_dir)

    if args.backfill:
        for name in principals:
            result = backfill(name, dataset_dir=dataset_dir, dry_run=args.dry_run)
            print(f"  {name}: ledger_rows={result['ledger_rows']} events={result['events']} "
                  f"added={result['added']}{' (dry-run)' if args.dry_run else ''}")
        return 0

    if args.tally:
        cube = tally(principal=args.principal, dataset_dir=dataset_dir)
        ranked = sorted(cube.items(), key=lambda kv: (-kv[1]["events"], kv[0][0]))
        print(f"  companies in the cube: {len(cube)}")
        excl = tally_exclusions(principal=args.principal, dataset_dir=dataset_dir)
        if excl["rows"]:
            top = ", ".join(list(excl["names"])[:3])
            print(f"  excluded as non-company: {excl['rows']} rows across {len(excl['names'])} strings "
                  f"(e.g. {top})")
        print(f"  {'company':32} {'principal':10} {'events':>6} {'dates':>6} {'go':>4} {'maybe':>5} {'best':>5}")
        for (company, name), cell in ranked[:args.top]:
            print(f"  {company[:32]:32} {name:10} {cell['events']:>6} {cell['distinct_dates']:>6} "
                  f"{cell['by_event'].get('go', 0):>4} {cell['by_event'].get('maybe', 0):>5} "
                  f"{cell['best_score'] if cell['best_score'] is not None else '-':>5}")
        return 0

    for row in candidates(principal=args.principal, dataset_dir=dataset_dir,
                          min_strong=args.min_strong, min_dates=args.min_dates)[:args.top]:
        print(f"  {row['company'][:32]:32} {row['principal']:10} strong={row['strong']:>3} "
              f"events={row['events']:>3} dates={row['distinct_dates']:>3} "
              f"{row['first']}..{row['last']} best={row['best_score']}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
