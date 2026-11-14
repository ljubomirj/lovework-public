#!/usr/bin/env python3
"""apply_by_email.py — which GO-s can be applied to without a browser.

LJ, 2026-10-13: *"We lack way to auto-apply to ads typically, b/c it requires
browser control, and we are lacking that. But maybe among many GO-s, maybe there
are some where application is possible by email?? We could that. Maybe we should
1-count and 2-highlight those GO-s that one can apply by email."*

An email application is the one route LoveWork can act on end to end: compose,
attach, and hand to the principal — no browser, no form, no anti-bot. This module
finds those adverts, says what the evidence is, and leaves the count and the
markers where a reader already looks.

**The rule is phrase-anchored, not address-anchored.** An address on a careers
page is usually *not* an application route: the disability-adjustment contact
(`user@example.invalid` — "let us know when you apply"), the informal-enquiry
line, and the recruitment-agency inbox all sit next to the word "apply". So the
detector requires an application *phrase* near the address and reports the
sentence it found, and the noise patterns are explicit and testable.

    ../venv/bin/python3 apply_by_email.py --profile all --count
    ../venv/bin/python3 apply_by_email.py --profile lj --index      # the listing page
    ../venv/bin/python3 apply_by_email.py --profile lj --annotate-packs
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import jsonl_io

#: Addresses that are never an application route.
NOISE = re.compile(
    r"(no-?reply|do-?not-?reply|unsubscribe|privacy|dpo@|gdpr|abuse@|postmaster@|"
    r"example\.(com|org)|sentry|wixpress|@2x|\.(png|jpg|jpeg|svg|webp)\b|"
    r"@media|sentry\.io|cloudflare|wix\.com)", re.I)

#: A sentence that *makes* an address an application route.
STRONG = re.compile(
    r"(apply(?:ing)?\s+(?:by|via|through)?\s*e-?mail|e-?mail\s+(?:your|us|me)\b|"
    r"send\s+(?:your\s+)?(?:cv|resume|résumé|application)|"
    r"(?:cv|resume|résumé|application)s?\s+(?:to|at)\s|"
    r"applications?\s+(?:to|via|by|at)\s|"
    r"to\s+apply[^.]{0,60}(?:e-?mail|send|write)|"
    r"write\s+to\s+us|"
    r"send\s+us\s+(?:your\s+)?(?:cv|resume|résumé)|"
    r"e-?mail\s+your\s+application|"
    # "Email my resume" — the link label on an ATS page (Helical, 2026-10-13),
    # which the first version missed because it expected "your/us/me".
    r"e-?mail\s+(?:my|our|your|the)\s+(?:cv|resume|résumé)|"
    r"e-?mail[^.]{0,40}(?:cv|resume|résumé|application)|"
    r"(?:cv|resume|résumé|application)[^.]{0,40}e-?mail)", re.I)

#: Phrases that mean the address is *not* the application route.
CAVEAT = re.compile(
    r"(informal\s+(?:chat|discussion|enquir)|questions?\s+about\s+the\s+role|"
    r"for\s+(?:an\s+)?informal|data\s+protection|privacy\s+notice|"
    r"subject\s+access|recruitment\s+(?:agenc|contact)|your\s+(?:external|internal)\s+"
    r"recruitment|safeguarding|media\s+enquir|press\s+enquir)", re.I)

#: Phrases that are **decisive**: they describe an accommodation, a press line or
#: a legal notice, so the address beside them is never the application route —
#: even when the same sentence also contains the word "apply".
#:
#: The Crick advert is why this exists: *"If you need assistance with applying
#: (i.e., would like to apply by phone or post) please email:
#: user@example.invalid"* — an accessibility route that any "apply"-anchored rule
#: reads as the way in (measured 2026-10-13).
ASSISTANCE = re.compile(
    r"(reasonable\s+adjustment|adjustments?\s+if\s+you|accessibility|"
    r"assistance\s+with\s+applying|"
    r"if\s+you\s+(?:need|require)\s+(?:assistance|support|any\s+adjustment)|"
    r"apply\s+by\s+phone\s+or\s+post|"
    r"would\s+like\s+to\s+apply\s+by\s+phone)", re.I)

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}")


@dataclass
class Route:
    """One advert's email application route, with the evidence for it."""
    address: str
    sentence: str
    strength: str          # "strong" | "mention"

    def as_dict(self) -> dict:
        return {"address": self.address, "sentence": self.sentence,
                "strength": self.strength}


def _sentences(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", s).strip() for s in SENTENCE_SPLIT.split(text or "")
            if s.strip()]


def detect(text: str, *, window: int = 2) -> list[Route]:
    """Every application-by-email route in an advert, with its sentence.

    `window` is how many sentences either side of an address are searched: an
    advert often says "To apply, email us." and then gives the address in the
    next line, and a one-sentence window misses it.
    """
    out: list[Route] = []
    sentences = _sentences(text)
    for index, sentence in enumerate(sentences):
        for match in EMAIL.finditer(sentence):
            address = match.group(0)
            if NOISE.search(address) or NOISE.search(sentence):
                continue
            around = " ".join(sentences[max(0, index - window): index + window + 1])
            # An assistance or legal line is decisive: the address beside it is
            # never the way in, however close the word "apply" is.
            if ASSISTANCE.search(sentence) or ASSISTANCE.search(around):
                out.append(Route(address, sentence[:300], "mention"))
                continue
            # A caveat in the address's own sentence is decisive too; one further
            # away only matters when nothing here says how to apply.
            if CAVEAT.search(sentence) and not STRONG.search(sentence):
                out.append(Route(address, sentence[:300], "mention"))
                continue
            if STRONG.search(sentence) or (STRONG.search(around)
                                           and not CAVEAT.search(sentence)):
                out.append(Route(address, sentence[:300], "strong"))
            elif CAVEAT.search(around):
                out.append(Route(address, sentence[:300], "mention"))
    # one row per address, strongest first
    best: dict[str, Route] = {}
    for route in out:
        prior = best.get(route.address)
        if prior is None or (route.strength == "strong" and prior.strength != "strong"):
            best[route.address] = route
    return sorted(best.values(), key=lambda r: (r.strength != "strong", r.address))


def advert_text(url: str, *, cache_dir: Path | None = None) -> str:
    """The advert's retained text, or "" — never a fetch."""
    if "://" not in (url or ""):
        return ""
    cache = (cache_dir or config.CACHE_DIR) / \
        f"page_{hashlib.sha256(url.encode()).hexdigest()[:16]}.md"
    try:
        return cache.read_text(encoding="utf-8", errors="replace") if cache.is_file() else ""
    except OSError:
        return ""


def pack_url(pack: Path) -> str:
    markdown = pack / "application_pack.md"
    if not markdown.is_file():
        return ""
    try:
        match = re.search(r"^- \*\*Link\*\*: (.+)$",
                          markdown.read_text(encoding="utf-8", errors="ignore"),
                          re.MULTILINE)
    except OSError:
        return ""
    value = match.group(1).strip().strip("<>") if match else ""
    return value if "://" in value else ""


def verdict_path(principal: str, state_dir: Path | None = None) -> Path:
    return (state_dir or config.STATE_DIR) / principal / "cache" / "email-routes.json"


def scan(principal: str, *, state_dir: Path | None = None) -> dict:
    """Every pack of a principal, with its advert's email route if it has one.

    `inspected` is reported next to the count on purpose: an advert whose page we
    never retained cannot be judged, and a count without its coverage is the kind
    of number this project has learned to distrust.
    """
    state_dir = state_dir or config.STATE_DIR
    root = state_dir / principal / "applications"
    packs = sorted(d for d in root.glob("*-LoveWork") if d.is_dir()) if root.is_dir() else []
    # The principal's own cache, not the module default: a scan of vj must read
    # vj's pages, and a test must be able to point the reader at its own tree.
    cache_dir = state_dir / principal / "cache"
    rows, inspected = [], 0
    for pack in packs:
        url = pack_url(pack)
        text = advert_text(url, cache_dir=cache_dir)
        if text:
            inspected += 1
        routes = detect(text) if text else []
        strong = [r for r in routes if r.strength == "strong"]
        rows.append({
            "pack": pack.name, "url": url, "retained": bool(text),
            "routes": [r.as_dict() for r in routes],
            "applicable_by_email": bool(strong),
        })
    return {
        "principal": principal,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "packs": len(packs), "inspected": inspected,
        "coverage": round(100.0 * inspected / len(packs), 1) if packs else 0.0,
        "applicable": sum(1 for r in rows if r["applicable_by_email"]),
        "rows": rows,
    }


def write_verdicts(principal: str, *, state_dir: Path | None = None) -> dict:
    """Persist the scan so index builders and pack writers read it, not the web."""
    result = scan(principal, state_dir=state_dir)
    path = verdict_path(principal, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
    return result


def load_verdicts(principal: str, *, state_dir: Path | None = None) -> dict:
    """The last scan, or an empty one — a reader must never fetch to answer."""
    path = verdict_path(principal, state_dir)
    if not path.is_file():
        return {"principal": principal, "packs": 0, "inspected": 0, "coverage": 0.0,
                "applicable": 0, "rows": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"principal": principal, "packs": 0, "inspected": 0, "coverage": 0.0,
                "applicable": 0, "rows": []}


def applicable_packs(principal: str, *, state_dir: Path | None = None) -> dict[str, dict]:
    """Pack name → its row, for the packs an email application can reach."""
    verdicts = load_verdicts(principal, state_dir=state_dir)
    return {row["pack"]: row for row in verdicts.get("rows", [])
            if row.get("applicable_by_email")}


def render_index(principal: str, verdicts: dict) -> str:
    """The listing LJ asked for: the GO-s one can apply to by email."""
    rows = [r for r in verdicts.get("rows", []) if r.get("applicable_by_email")]
    out = [f"# Apply by email — {principal}", "",
           f"_{verdicts.get('at', '')} · {verdicts.get('applicable', 0)} of "
           f"{verdicts.get('packs', 0)} pack(s) carry an email application route · "
           f"advert text retained for {verdicts.get('inspected', 0)} "
           f"({verdicts.get('coverage', 0.0)}% of packs) — the count is a floor, "
           f"because an advert whose page we never kept cannot be judged._", ""]
    if not rows:
        out.append("_None found. The detector needs the advert's own text: the "
                   "daily link-check now caches every GO advert it opens, so "
                   "coverage grows with each 06:00 pass._")
        return "\n".join(out) + "\n"
    for row in sorted(rows, key=lambda r: r["pack"]):
        strong = [x for x in row["routes"] if x["strength"] == "strong"]
        out.append(f"## {row['pack']}")
        out.append("")
        if row.get("url"):
            out.append(f"- Advert: {row['url']}")
        for route in strong:
            out.append(f"- **Email to apply: `{route['address']}`**")
            out.append(f"  - Evidence: “{route['sentence']}”")
        mentions = [x for x in row["routes"] if x["strength"] == "mention"]
        for route in mentions:
            out.append(f"- _Mentioned, not an application route_: `{route['address']}`")
            out.append(f"  - Evidence: “{route['sentence']}”")
        out.append("")
    out += ["---", "",
            "_Only the address and the sentence it came from are claimed. Nothing "
            "here sends anything: the principal composes and sends (MANIFESTO, "
            "docs/31)._", ""]
    return "\n".join(out) + "\n"


def write_index(principal: str, *, state_dir: Path | None = None) -> Path:
    verdicts = load_verdicts(principal, state_dir=state_dir)
    path = (state_dir or config.STATE_DIR) / principal / "wiki" / "apply-by-email.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_index(principal, verdicts), encoding="utf-8")
    return path


def annotate_packs(principal: str, *, state_dir: Path | None = None,
                   dry_run: bool = False) -> dict:
    """Add the route line to each applicable pack's front page, once.

    Additive and idempotent: the line goes above the status marker, nothing is
    rewritten, and a pack that already carries it is skipped.
    """
    state_dir = state_dir or config.STATE_DIR
    marked = 0
    for pack_name, row in applicable_packs(principal, state_dir=state_dir).items():
        pack = state_dir / principal / "applications" / pack_name
        markdown = pack / "application_pack.md"
        if not markdown.is_file():
            continue
        text = markdown.read_text(encoding="utf-8", errors="ignore")
        if "Apply by email" in text:
            continue
        strong = [x for x in row["routes"] if x["strength"] == "strong"]
        if not strong:
            continue
        marked += 1
        if dry_run:
            continue
        line = ("- **Apply by email**: " + ", ".join(f"`{x['address']}`" for x in strong)
                + f" — from the advert: “{strong[0]['sentence'][:160]}”\n")
        lines = text.split("\n")
        for index, existing in enumerate(lines):
            if existing.startswith("**LoveWork status:"):
                lines.insert(index, line.rstrip("\n"))
                break
        else:
            lines.append(line.rstrip("\n"))
        markdown.write_text("\n".join(lines), encoding="utf-8")
    return {"principal": principal, "packs_marked": marked, "dry_run": dry_run}


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="all")
    parser.add_argument("--count", action="store_true")
    parser.add_argument("--index", action="store_true", help="write the listing page")
    parser.add_argument("--annotate-packs", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    # The declared priority order, not a fourth copy of the old one (2026-10-19).
    principals = list(config.principal_names(config.STATE_DIR)) \
        if args.profile == "all" else [args.profile]
    out = []
    for principal in principals:
        verdicts = write_verdicts(principal)
        if args.annotate_packs:
            verdicts["annotated"] = annotate_packs(principal, dry_run=args.dry_run)
        if args.index:
            verdicts["index"] = str(write_index(principal))
        out.append(verdicts)
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    for verdicts in out:
        print(f"{verdicts['principal']}: {verdicts['applicable']} of "
              f"{verdicts['packs']} pack(s) applicable by email "
              f"(advert text retained for {verdicts['inspected']} = "
              f"{verdicts['coverage']}% — the count is a floor)")
        for row in verdicts["rows"]:
            if row["applicable_by_email"]:
                strong = [x for x in row["routes"] if x["strength"] == "strong"]
                print(f"   {row['pack'][:56]:58s} {', '.join(x['address'] for x in strong)[:52]}")
        if verdicts.get("annotated"):
            print(f"   annotated: {verdicts['annotated']['packs_marked']} pack(s)")
        if verdicts.get("index"):
            print(f"   index: {verdicts['index']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
