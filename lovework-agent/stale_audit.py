#!/usr/bin/env python3
"""How stale are the adverts in a principal's latest report? (read-only)

PK's 2026-09-20 feedback on the VJ sweep: the first GO links were weeks-to-months
old adverts. A stale advert is bad for the principal three ways, and all three
are firm-side failures, not our-side noise:

  1. the firm cannot fill the role — something is wrong with the firm;
  2. the firm filled it and nobody took the advert down — the listing is noise;
  3. the firm is not hiring but farms CVs routinely — a pool to dip into when
     ~10% of staff leave each year.

**What this tool can and cannot measure.** Two signals exist, in this order:

1. **A real posting date, when a source provides one — the true signal.** Nothing
   captures one today; the viable capture point is HN's Algolia `created_at` on
   the job item (a WP3a acquisition slice: registry column + migration +
   plumbing + tests).
2. **The observed-open floor** — `age = report_date - first_seen`, where
   `first_seen` is when *we* first observed the advert. It is a **lower bound**:
   an org we started crawling last month hides its year-old posting. A large
   floor justifies a penalty; a small floor is **no evidence of freshness**.

No source carries a posted date today — digitaljobs documents its absence, alert
mailboxes carry the alert's date rather than the advert's, and cached advert
pages rarely carry one (the "posted" hits in the VJ cache are prose like "posted
on LinkedIn", not dates). The audit therefore reports the floor and labels it as
such; unknown age is reported as unknown, never as fresh. That asymmetry is
deliberate: a penalty built on the floor can under-punish, never wrongly kill a
fresh advert.

Usage (from lovework-agent/):

    ../venv/bin/python3 stale_audit.py --profile vj
    ../venv/bin/python3 stale_audit.py --profile vj --section GO --oldest 10
    ../venv/bin/python3 stale_audit.py --profile vj --json
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from datetime import date, datetime
from pathlib import Path

from report_header import section_heading_label

# Report sections in reading order: the ones a principal acts on first, first.
SECTION_ORDER = ("GO", "MAYBE", "New Listings", "Long-Lasting", "FLAG", "DROP")

# The ordinal is presentation, not identity: crawl reports number their
# entries ("### 1. Org — Title") and some writers do not.
_LISTING_RE = re.compile(r"^###\s+(?:\d+\.\s+)?(?P<org>.+?)\s+—\s+(?P<title>.+?)\s*$")
# Section headings are read through the report format's own reader, so an
# annotation after the count ("## DROP (692) — 53 ineligible hidden (…)") does
# not close a section early. The local regex used to require the line to end at
# the count, which silently attributed every DROP listing to FLAG.
_URL_RE = re.compile(r"^\s*-\s*\*\*URL\*\*:\s*(?P<url>\S+)")
_LIFECYCLE_RE = re.compile(r"^\s*-\s*\*\*Lifecycle\*\*:\s*(?P<life>\S+)")
_SCORE_RE = re.compile(r"^\s*-\s*\*\*Score\*\*:\s*(?P<score>\d+(?:\.\d+)?)")
_DATE_RE = re.compile(r"(?P<d>\d{4}-\d{2}-\d{2})")

# Age buckets used in the summary (days).
BUCKETS = ((0, 7), (8, 30), (31, 60), (61, 90), (91, 10**6))


def _as_day(value: str) -> date | None:
    if not value:
        return None
    match = _DATE_RE.search(value)
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group("d")).date()
    except ValueError:
        return None


def parse_report(path: Path) -> tuple[date | None, dict[str, list[dict]]]:
    """Return (report_date, {section: [listing, ...]}) for a wiki report."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    head = re.search(r"^#\s+.*?—\s+(?P<d>\d{4}-\d{2}-\d{2})", text, re.MULTILINE)
    report_date = _as_day(head.group("d")) if head else None

    sections: dict[str, list[dict]] = {}
    current: str | None = None
    listing: dict | None = None
    for line in text.splitlines():
        name = section_heading_label(line)
        if name:
            current = name
            sections.setdefault(current, [])
            listing = None
            continue
        heading = _LISTING_RE.match(line)
        if heading and current:
            listing = {"org": heading.group("org").strip(),
                       "title": heading.group("title").strip()}
            sections[current].append(listing)
            continue
        if listing is None:
            continue
        if not listing.get("url"):
            m = _URL_RE.match(line)
            if m:
                listing["url"] = m.group("url").strip()
        if not listing.get("lifecycle"):
            m = _LIFECYCLE_RE.match(line)
            if m:
                listing["lifecycle"] = m.group("life").strip()
        if listing.get("score") is None:
            m = _SCORE_RE.match(line)
            if m:
                listing["score"] = float(m.group("score"))
    return report_date, sections


def load_registry(state_root: Path) -> tuple[dict[tuple[str, str], dict], dict[str, dict]]:
    """Index the principal's registry by (org, title) and by url, both lowercased."""
    by_pair: dict[tuple[str, str], dict] = {}
    by_url: dict[str, dict] = {}
    path = state_root / "cache" / "jobs.csv"
    if not path.is_file():
        return by_pair, by_url
    with path.open(newline="", encoding="utf-8", errors="ignore") as handle:
        for row in csv.DictReader(handle):
            org = (row.get("org") or "").strip().lower()
            title = (row.get("title") or "").strip().lower()
            if org and title and (org, title) not in by_pair:
                by_pair[(org, title)] = row
            url = (row.get("url") or "").strip().lower()
            if url and url not in by_url:
                by_url[url] = row
    return by_pair, by_url


def annotate(listings: list[dict], by_pair: dict, by_url: dict, at: date | None) -> None:
    """Attach the registry's age signals to each listing, in place.

    `age_days` is the best age we have — the **larger** of the two lower bounds
    the matcher uses (a source-reported `posted_at` and the observed-open floor
    `first_seen`) — and `age_signal` says which one it came from. `floor_days`
    and `posted_days` always carry both, so a relist (floor > posted) is visible
    as such (§WP3a).
    """
    for item in listings:
        row = by_pair.get((item["org"].lower(), item["title"].lower()))
        if row is None and item.get("url"):
            row = by_url.get(item["url"].lower())
        if row is None:
            item["age_days"] = None
            item["first_seen"] = None
            item["posted_at"] = None
            item["age_signal"] = None
            item["status"] = None
            continue
        first = _as_day(row.get("first_seen", ""))
        posted = _as_day(row.get("posted_at", ""))
        floor_days = (at - first).days if (at and first) else None
        posted_days = (at - posted).days if (at and posted) else None
        if posted_days is not None and posted_days < 0:
            # A future stamp is clock skew, not an age: the matcher drops it, so
            # the audit must measure the same policy.
            posted_days = None
        item["first_seen"] = first.isoformat() if first else None
        item["posted_at"] = posted.isoformat() if posted else None
        item["status"] = row.get("status")
        item["floor_days"] = floor_days
        item["posted_days"] = posted_days
        # The matcher takes the larger of the two lower bounds; the audit must
        # measure the policy actually in force, not a different one.
        if posted_days is not None and floor_days is not None:
            if floor_days > posted_days:
                item["age_days"], item["age_signal"] = floor_days, "relist"
            else:
                item["age_days"], item["age_signal"] = posted_days, "posted"
        elif posted_days is not None:
            item["age_days"], item["age_signal"] = posted_days, "posted"
        else:
            item["age_days"] = floor_days
            item["age_signal"] = "floor" if floor_days is not None else None


def _bucket_label(days: int) -> str:
    for low, high in BUCKETS:
        if low <= days <= high:
            return f"{low}-{high}d" if high < 10**6 else f"{low}+d"
    return "?"


def summarise(listings: list[dict]) -> dict:
    ages = [i["age_days"] for i in listings if i.get("age_days") is not None]
    missing = sum(1 for i in listings if i.get("age_days") is None)
    out = {"listings": len(listings), "unknown_age": missing}
    if not ages:
        return out
    out.update({
        "min_days": min(ages),
        "median_days": int(statistics.median(ages)),
        "max_days": max(ages),
        "buckets": {_bucket_label(d): sum(1 for a in ages if _bucket_label(a) == _bucket_label(d))
                    for d in ages},
    })
    out["buckets"] = {label: sum(1 for a in ages if _bucket_label(a) == label)
                      for low, high in BUCKETS
                      for label in [_bucket_label(low)]}
    return out


def _policy_stale_days() -> int:
    """The matcher's live staleness threshold, so the audit measures the policy
    actually in force rather than a number copied here that can drift from it."""
    from matcher import STALE_FLAG_DAYS

    return STALE_FLAG_DAYS


def audit(report: Path, state_root: Path, *, section: str | None = None,
          oldest: int = 5, stale_days: int | None = None) -> dict:
    stale_days = _policy_stale_days() if stale_days is None else stale_days
    report_date, sections = parse_report(report)
    by_pair, by_url = load_registry(state_root)
    names = [n for n in SECTION_ORDER if n in sections]
    names += [n for n in sections if n not in names]
    if section:
        names = [n for n in names if n.lower() == section.lower()]
    result = {"report": str(report), "report_date": report_date.isoformat() if report_date else None,
              "registry_rows": len(by_pair), "sections": {}}
    for name in names:
        items = sections[name]
        annotate(items, by_pair, by_url, report_date)
        stale = [i for i in items if (i.get("age_days") or 0) >= stale_days]
        result["sections"][name] = {
            "summary": summarise(items),
            "stale_over_%dd" % stale_days: len(stale),
            "oldest": sorted(
                (i for i in items if i.get("age_days") is not None),
                key=lambda i: i["age_days"], reverse=True)[:oldest],
        }
    return result


def render(result: dict, *, stale_days: int) -> str:
    lines = [f"report: {result['report']}",
             f"report date: {result['report_date']}   registry rows: {result['registry_rows']}",
             "age = the advert's posted date when the source gives one (true age);",
             "      otherwise report date - first seen (a LOWER bound). The signal used is",
             "      named per listing.", ""]
    for name, data in result["sections"].items():
        s = data["summary"]
        if not s.get("min_days") and s.get("min_days") != 0:
            lines.append(f"{name}: {s['listings']} listings, no registry age available")
            continue
        buckets = " ".join(f"{k}={v}" for k, v in s["buckets"].items() if v)
        lines.append(f"{name}: {s['listings']} listings · age min={s['min_days']}d "
                     f"median={s['median_days']}d max={s['max_days']}d · "
                     f">= {stale_days}d: {data['stale_over_%dd' % stale_days]} · {buckets}")
        for item in data["oldest"]:
            score = f"{item['score']:.1f}" if item.get("score") is not None else "—"
            signal = item.get("age_signal")
            if signal == "posted":
                source = (f"posted {item['posted_at']}"
                          + (f", floor {item['floor_days']}d"
                             if item.get("floor_days") is not None else ""))
            elif signal == "relist":
                source = (f"open {item['floor_days']}d, relisted "
                          f"{item['posted_at']} (floor wins)")
            else:
                source = f"first seen {item['first_seen']} (floor)"
            lines.append(f"    {item['age_days']:>4}d  {score:>4}  {item['org']} — {item['title'][:60]}"
                         f"  [{source}, {item.get('status')}]")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", required=True, help="lj / vj / kj / pk")
    parser.add_argument("--report", type=Path, default=None,
                        help="report path (default: the principal's newest)")
    parser.add_argument("--section", default=None, help="only this section (e.g. GO)")
    parser.add_argument("--oldest", type=int, default=5, help="oldest rows to list per section")
    parser.add_argument("--stale-days", type=int, default=None,
                        help="staleness threshold to count (default: the matcher's "
                             "live LOVEWORK_STALE_FLAG_DAYS)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config

    state_root = config.STATE_DIR / args.profile
    report = args.report
    if report is None:
        reports = sorted((state_root / "wiki" / "reports").glob("*.md"),
                         key=lambda p: p.name, reverse=True)
        if not reports:
            print(f"no reports under {state_root}/wiki/reports", file=sys.stderr)
            return 66
        report = reports[0]

    stale_days = _policy_stale_days() if args.stale_days is None else args.stale_days
    result = audit(report, state_root, section=args.section, oldest=args.oldest,
                   stale_days=stale_days)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(render(result, stale_days=stale_days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
