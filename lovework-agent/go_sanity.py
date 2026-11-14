"""go_sanity.py — what a human agent would refuse to put in front of the principal.

LJ, 2026-11-10: *"In the profile lj current wiki there are GO cases where I see
obvious problems, not in outright errors. But in lacking knowledge that in the next
step leads to un-intelligent behaviour and time-wasting… An HI would not waste
principal's time."*

This is the cheap half of that answer: the failures a program can see without
reading the advert — a company name that is a board's URL slug, a "role" that is a
location, the same employer queued three times, a pack that was never built. Each
check names the evidence it fired on; none of them decides for the principal, they
mark the work he should not spend an evening on.

It is deliberately a *reader*: it never edits the wiki, the packs or the registry.

    python3 go_sanity.py --profile lj [--json] [--max-age-days 10] [--limit N]

Exit 0 when nothing fires, 1 when anything does, 3 when the principal has no wiki.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import config
from principal_runtime import resolve_principal_runtime

#: An organisation written as a board's URL slug: lowercase, hyphenated, often
#: with the board's disambiguating suffix (`addepar1`, `coreweaveu`,
#: `iwoca-co-uk`, `8westconsulting`). A real employer name has a capital letter
#: or a space.
SLUG_ORG = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: Names a slug keeps when the board appends its own disambiguator.
SLUG_NOISE = re.compile(r"(\d+|u|co|uk|com|ltd|inc)$", re.I)

#: A role that is really a place: `City, Country`, `Freiburg, Germany`, `Remote`.
PLACE_ROLE = re.compile(
    r"^(remote|hybrid|on-?site|anywhere|[A-Z][a-zà-ÿ'-]+, *[A-Z][A-Za-zà-ÿ' .-]+)$")

#: A title carrying an agency's reference number rather than a role.
REF_TITLE = re.compile(r"\b(ID|Ref|REF|Req|JR)\s*[-:]?\s*\d{4,}\b")

#: Recruiters and boards that are a *route*, never the employer of record.
AGENCIES = frozenset({
    "harnham", "humand talent", "hays", "robert walters", "michael page",
    "reed", "totaljobs", "cwjobs", "jobserve", "digitaljobs", "linkedin",
    "indeed", "glassdoor", "lensa", "jobleads", "talent.com", "jooble",
})


def known_orgs(runtime) -> set[str]:
    """Every organisation the principal's own corpora already name.

    The keep-list, the wiki's org pages, the memory's org memories and the case
    directories. A board slug that matches none of them is *unverified*, which is
    a different and more honest finding than "wrong": `Elevenlabs` is a real
    company whose slug happens to be its name, `Addepar1` is not.
    """
    names: set[str] = set()
    try:
        import yaml
        path = runtime.state_root / "profiles" / runtime.profile_name / "company_pages.yaml"
        if not path.is_file():
            path = config.profile_dir(runtime.profile_name) / "company_pages.yaml"
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            entries = data.get("companies") if isinstance(data, dict) else data
            for entry in entries or []:
                if isinstance(entry, dict):
                    names.add(str(entry.get("org") or entry.get("name") or ""))
                elif isinstance(entry, str):
                    names.add(entry)
    except Exception:
        pass
    for folder in (runtime.wiki_root / "orgs", runtime.dataset_dir / "memory" / "orgs"):
        try:
            for entry in folder.iterdir():
                names.add(entry.stem if entry.suffix else entry.name)
        except OSError:
            pass
    # Case directories are deliberately NOT a source: their name is built from the
    # same slug the check is testing, so seeding from them would make every slug
    # "known" by its own case.
    return {_norm(n) for n in names if n}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _slug_evidence(org: str, advert: str = "", known: set[str] | None = None) -> str:
    """Why this organisation name is a board slug, or "".

    The decisive test is not the spelling but the provenance: a board builds its
    advert URL from the company slug, so when the displayed name equals the first
    token(s) of the advert path, the name *was* the slug. That catches
    `addepar1`, `coreweaveu`, `castaigroupinc`, `sproutsocial` and `iwoca.co.uk`
    without guessing at capitalisation.
    """
    text = (org or "").strip()
    if not text:
        return ""
    if text.lower() in AGENCIES:
        return ""
    if advert:
        m = re.search(r"/job/([a-z0-9._-]+)", advert)
        if m:
            slug = m.group(1).lower()
            name = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
            if name and slug.startswith(name) and _norm(text) not in (known or set()):
                return (f"'{text}' came from the board's URL slug ('{slug[:40]}…') and matches "
                        f"no organisation in the principal's lists")
    # Without the advert URL, only shapes that no company name has can be called:
    # a long name ending in a digit (`Addepar1`), a TLD inside the name
    # (`Iwoca.Co.Uk`), a glued corporate suffix (`Castaigroupinc`). `C3` is left
    # alone: short names do end in digits.
    low = text.lower()
    if " " not in text:
        if len(text) >= 5 and text[-1].isdigit():
            return f"'{text}' carries a board's numeric suffix, not a company name"
        if low.endswith(".co.uk") or re.search(r"\.(co|com|io|net|org)$", low):
            return f"'{text}' carries a domain, not a company name"
        if len(text) >= 8 and re.search(r"(inc|ltd|llc)$", low):
            return f"'{text}' glues a corporate suffix onto the slug"
    return ""


def parse_go_rows(index_md: str) -> list[dict]:
    """The GO section of the wiki index, as rows. Tolerant of the row shapes."""
    if "## GO Listings" not in index_md:
        return []
    section = index_md.split("## GO Listings", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith("- **"):
            continue
        date_m = re.search(r"\*\*(\d{4}-\d{2}-\d{2})\*\*", line)
        pack_m = re.search(r"\*\*(PACK|NO PACK)\*\*", line)
        head = re.search(r"\[([^\]]+)\]", line)
        score = re.search(r"\| *(\d+(?:\.\d+)?)/10", line)
        link = re.search(r"\]\(([^)]+)\)", line)
        advert = re.search(r"\[https?://[^]]+\]\(([^)]+)\)", line)
        if not (date_m and pack_m and head):
            continue
        org, _, role = head.group(1).partition(" — ")
        case = ""
        if link and "/applications/" in link.group(1):
            case = link.group(1).split("/applications/", 1)[1].split("/", 1)[0]
        rows.append({"date": date_m.group(1), "pack": pack_m.group(1),
                     "org": org.strip(), "role": role.strip(),
                     "score": float(score.group(1)) if score else None,
                     "case": case, "advert": advert.group(1) if advert else ""})
    return rows


def check(rows: Iterable[dict], *, today: Optional[date] = None,
          max_age_days: int = 10, duplicate_window_days: int = 7,
          known: Optional[set[str]] = None) -> list[dict]:
    """Every flag, one dict per row that fires. A row may fire several checks."""
    today = today or date.today()
    known = known or set()
    rows = list(rows)
    by_org: dict[str, list[dict]] = defaultdict(list)
    by_case: dict[str, dict] = {}

    for row in rows:
        by_org[_norm(row["org"])].append(row)
        fired = []
        why = []

        slug = _slug_evidence(row["org"], row.get("advert", ""), known)
        if slug:
            fired.append("ENTITY")
            why.append(slug)
        if row["org"].strip().lower() in AGENCIES:
            fired.append("AGENCY")
            why.append(f"'{row['org']}' is a recruiter/board, not the employer")
        if PLACE_ROLE.match(row["role"].strip()):
            fired.append("TITLE")
            why.append(f"'{row['role']}' is a place, not a role")
        if REF_TITLE.search(row["role"]):
            fired.append("TITLE")
            why.append("the title carries an agency reference number")
        advert = row.get("advert") or ""
        if advert and re.match(r"^https?://[^/]+/?$", advert):
            fired.append("LIVE")
            why.append(f"the 'advert' is a bare domain root ({advert}), not an advert")
        if row.get("pack") == "NO PACK":
            fired.append("NO-PACK")
            why.append("no pack exists, so the GO has nothing to send")
        try:
            age = (today - datetime.strptime(row["date"], "%Y-%m-%d").date()).days
            if age > max_age_days:
                fired.append("STALE")
                why.append(f"the case is {age} days old (cap {max_age_days})")
        except (ValueError, KeyError):
            pass
        if fired:
            by_case[f"{row.get('case') or row['org']}|{row['role']}"] = {
                "org": row["org"], "role": row["role"], "case": row["case"],
                "score": row["score"], "fired": set(fired), "why": list(why)}

    # Duplicates are a property of the pair, so they are computed once per org.
    for norm, group in by_org.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: r["date"])
        span = 0
        try:
            first = datetime.strptime(group[0]["date"], "%Y-%m-%d").date()
            last = datetime.strptime(group[-1]["date"], "%Y-%m-%d").date()
            span = (last - first).days
        except ValueError:
            pass
        if span <= duplicate_window_days:
            window = "on the same day" if span == 0 else f"within {span} days"
            for row in group:
                key = f"{row.get('case') or row['org']}|{row['role']}"
                entry = by_case.setdefault(key, {
                    "org": row["org"], "role": row["role"], "case": row["case"],
                    "score": row["score"], "fired": set(), "why": []})
                entry["fired"].add("DUPLICATE")
                entry["why"].append(f"{len(group)} GO cases for one employer {window} "
                                    f"— one approach, not {len(group)}")
    flags = []
    for entry in by_case.values():
        flags.append({"org": entry["org"], "role": entry["role"], "case": entry["case"],
                      "score": entry["score"], "fired": sorted(entry["fired"]),
                      "why": "; ".join(entry["why"])})
    flags.sort(key=lambda f: (-len(f["fired"]), f["org"]))
    return flags


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--max-age-days", type=int, default=10,
                        help="staleness cap for a GO case (docs/27: 10-day cap)")
    parser.add_argument("--duplicate-window-days", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="print at most N rows")
    args = parser.parse_args(argv)

    runtime = resolve_principal_runtime(args.profile)
    index_path = runtime.wiki_root / "index.md"
    if not index_path.is_file():
        print(f"no wiki index at {index_path}", file=sys.stderr)
        return 3

    rows = parse_go_rows(index_path.read_text(encoding="utf-8", errors="replace"))
    flags = check(rows, max_age_days=args.max_age_days,
                  duplicate_window_days=args.duplicate_window_days,
                  known=known_orgs(runtime))
    counts = Counter(f for flag in flags for f in flag["fired"])
    report = {"principal": args.profile, "go_rows": len(rows), "flagged_rows": len(flags),
              "by_check": dict(counts), "flags": flags}

    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
    else:
        print(f"GO rows {len(rows)}, flagged {len(flags)} (= {len(flags)}/{len(rows)})")
        print(f"by check: {dict(counts)}")
        shown = flags[: args.limit] if args.limit else flags
        for flag in shown:
            print(f"  [{','.join(flag['fired'])}] {flag['org']} — {flag['role'][:44]}")
            print(f"      {flag['why']}")
    return 1 if flags else 0


if __name__ == "__main__":
    sys.exit(main())
