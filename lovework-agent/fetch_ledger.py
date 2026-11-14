#!/usr/bin/env python3
"""fetch_ledger.py — what each fetch provider actually did.

LJ, switching the chain to Tavily while Firecrawl's credits are out (2026-11-04):
*"it's a good test, to see on our application — is some of those web search web
fetch providers better or worse? We have Firecrawl/Tavily/Exa and even DeepSeek.
Plus the personal Brave and or local Chrome as a last ditch fallbacks?"*

A provider choice made from memory is a guess. Tavily's quota ran out in October,
Exa's credits in October, Firecrawl's on 2026-11-03, and each switch was driven by
that month's failure rather than by a comparison of what each one returns on *our*
pages. This ledger makes the comparison a measurement.

One row per attempt (`state/<p>/dataset/fetch_attempts.jsonl`, append-only, the
same shape as every other ledger here):

    {"at": …, "run": "chain"|"bench", "provider": "tavily", "url": …,
     "ok": true, "chars": 6511, "seconds": 1.9, "error": "", "detail": …}

`ok` is not "the call returned" — it is "we got evidence": text that cleared the
block-page guard and the advert floor. A provider that returns 873 characters of a
Cloudflare wall has failed, and the ledger must say so rather than count a hit.

Every ratio printed shows the two numbers it divided (the house rule, §0 of the
skill): `Tavily 210 tok…`-style — here `61.8% ok (=611 of 988 attempts)`.
"""
from __future__ import annotations

import json
import logging
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

logger = logging.getLogger(__name__)

LEDGER_NAME = "fetch_attempts.jsonl"

#: Text under this many characters is not evidence, whatever the provider says.
#: The same floor the reviews use for an advert (`tick.MIN_ADVERT_CHARS`), stated
#: here so the ledger's verdict does not need to import the tick.
MIN_EVIDENCE_CHARS = 1000


def ledger_path() -> Path:
    """Where attempts land: the active principal's dataset, else the default one.

    `model_transcripts.resolve_dataset_dir()` is the one authority for "whose
    dataset is this process writing into" — the tick pins it per principal, so a
    fetch made while ticking vj is filed under vj rather than in lj's ledger.
    """
    import model_transcripts

    resolved = model_transcripts.resolve_dataset_dir()
    base = Path(resolved) if resolved else Path(config.DATASET_DIR)
    return base / LEDGER_NAME


def is_evidence(text: str) -> bool:
    """Is this text something we can judge a job advert by?"""
    body = (text or "").strip()
    if len(body) < MIN_EVIDENCE_CHARS:
        return False
    try:
        from enrichment import looks_blocked

        return not looks_blocked(body)
    except Exception:                      # never lose the measurement to the guard
        return True


def record(provider: str, url: str, text: str, *, seconds: float, run: str = "chain",
           error: str = "", detail: str = "", at: str | None = None) -> dict:
    """Append one attempt row. Never raises: a ledger must not break a fetch."""
    ok = bool(text) and is_evidence(text)
    if not ok and not error:
        # Say *why* a row is not evidence, or the report shows a bare zero.
        try:
            from enrichment import looks_blocked

            error = "block page" if looks_blocked(text) else (
                f"under {MIN_EVIDENCE_CHARS} chars" if text else "no text")
        except Exception:
            error = "no text" if not text else f"under {MIN_EVIDENCE_CHARS} chars"
    row = {
        "at": at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run": run,
        "provider": provider or "none",
        "url": url,
        "ok": ok,
        "chars": len(text or ""),
        "seconds": round(float(seconds or 0.0), 3),
        "error": error,
    }
    if detail:
        row["detail"] = detail[:300]
    try:
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:               # a failed write is logged, never raised
        logger.debug("fetch ledger write skipped: %s", exc)
    return row


def read(*, days: int | None = None, run: str | None = None) -> list[dict]:
    """Rows from the ledger, optionally only the last `days` days."""
    path = ledger_path()
    if not path.is_file():
        return []
    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if run and row.get("run") != run:
            continue
        if cutoff:
            try:
                when = datetime.fromisoformat(str(row.get("at")))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when < cutoff:
                continue
        rows.append(row)
    return rows


def summarise(rows: list[dict]) -> dict[str, dict]:
    """Per-provider totals. Counts are integers; every rate is reported with them."""
    out: dict[str, dict] = {}
    for row in rows:
        name = str(row.get("provider") or "none")
        acc = out.setdefault(name, {"attempts": 0, "ok": 0, "chars": 0,
                                    "ok_chars": 0, "seconds": [], "errors": {}})
        acc["attempts"] += 1
        acc["chars"] += int(row.get("chars") or 0)
        try:
            acc["seconds"].append(float(row.get("seconds") or 0.0))
        except (TypeError, ValueError):
            pass
        if row.get("ok"):
            acc["ok"] += 1
            acc["ok_chars"] += int(row.get("chars") or 0)
        error = str(row.get("error") or "").strip()
        if error:
            key = error.split(":")[0][:60]
            acc["errors"][key] = acc["errors"].get(key, 0) + 1
    for acc in out.values():
        times = [t for t in acc.pop("seconds") if t > 0]
        acc["median_seconds"] = round(statistics.median(times), 2) if times else 0.0
        acc["total_seconds"] = round(sum(times), 1)
        acc["ok_rate"] = (acc["ok"] / acc["attempts"]) if acc["attempts"] else 0.0
        acc["chars_per_ok"] = (round(acc["ok_chars"] / acc["ok"]) if acc["ok"] else 0)
    return out


HEADER = ("provider", "attempts", "ok", "ok% (ok/attempts)", "median s",
          "chars/ok", "chars/s", "top error")


def render(summary: dict[str, dict]) -> str:
    """A table where every ratio carries its numerator and denominator."""
    lines = ["  ".join(f"{h:>10}" if i else f"{h:<10}"
                       for i, h in enumerate(HEADER))]
    for name in sorted(summary, key=lambda n: (-summary[n]["ok"], -summary[n]["attempts"])):
        acc = summary[name]
        top_error = ""
        if acc["errors"]:
            top_error = max(acc["errors"].items(), key=lambda kv: kv[1])[0]
            top_error = f"{top_error[:28]}×{max(acc['errors'].values())}"
        rate = (f"{100 * acc['ok_rate']:.0f}% "
                f"(={acc['ok']}/{acc['attempts']})") if acc["attempts"] else "—"
        per_second = (round(acc["ok_chars"] / acc["total_seconds"])
                      if acc["total_seconds"] else 0)
        cells = [name, str(acc["attempts"]), str(acc["ok"]), rate,
                 f"{acc['median_seconds']:.1f}", str(acc["chars_per_ok"]),
                 str(per_second), top_error]
        lines.append("  ".join(f"{c:>10}" if i else f"{c:<10}"
                               for i, c in enumerate(cells)))
    return "\n".join(lines)


def report(*, days: int | None = 30, run: str | None = "chain") -> str:
    rows = read(days=days, run=run)
    if not rows:
        return (f"No fetch attempts in the ledger yet "
                f"({ledger_path()}) — runs record themselves as they fetch.")
    summary = summarise(rows)
    head = (f"Fetch providers — {len(rows)} attempts "
            f"({'all time' if not days else f'last {days} days'}"
            f"{', ' + run + ' runs' if run else ''})")
    return "\n".join([head, "", render(summary), "",
                      "ok = text that cleared the block-page guard and the "
                      f"{MIN_EVIDENCE_CHARS}-char evidence floor."])


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="What each fetch provider did.")
    parser.add_argument("--days", type=int, default=30,
                        help="window; 0 for everything")
    parser.add_argument("--run", default="chain",
                        help="'chain' (real fetches), 'bench' (the comparison), or 'all'")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    run = None if args.run == "all" else args.run
    if args.json:
        rows = read(days=args.days or None, run=run)
        print(json.dumps(summarise(rows), indent=2))
        return 0
    print(report(days=args.days or None, run=run))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(_main())
