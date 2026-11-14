#!/usr/bin/env python3
"""retire.py — the two closing piles: GONE and EXPIRED.

LJ approved the design on 2026-10-14 (`docs/45-the-two-closing-piles.md`) with four
answers, and they are all in here:

1. **45-day horizon, 15-day half-life** — *"we should have half-life 15 days so in
   45 days it decays 3 times, 1/8-th of the original probability"*.
2. **A GONE pack stays browsable** — the pile is served like `applications/`.
3. **"Applied" means applied** — a real submission record, *not* a prepared pack:
   *"if we genuinely applied, i.e. there is a record of application submission, not
   a mere application pack that was never followed on"*. Measured 2026-10-14: 1
   `SUBMITTED` marker against 452 `PREPARED`.
4. **The retire pass runs in the tick's maintenance phase** — cheap, idempotent,
   hourly.

And the division he picked:

| The advert was… | Closed → | Why |
|---|---|---|
| **a GO** — material prepared | `state/<p>/gone/` | the material is the value; "what happened to the ones we wanted?" stays answerable |
| **MAYBE / FLAG / DROP** — judged only | `state/<p>/expired/` | a record that we looked; nothing to revisit |

**The move writes a map.** The directory moves whole (nothing collected is lost)
and a row goes into `state/<p>/cache/moves.json` (old path → new path), which the
serving layer resolves, so every existing link — the wiki's GO rows, a lead page,
a bookmark — keeps working.

The first version left a **relative symlink** at the old path instead, on the
grounds that a link cannot drift from the truth. It works for readers and it is
wrong for the principal: **`git add` refuses a path beyond a symlink**, so his own
staging list (`git add $(cat qwen)`) died on the first retired pack — *"fatal:
pathspec … is beyond a symbolic link"* (measured 2026-10-14). A map keeps the tree
ordinary: old paths simply do not exist, and the resolver carries the history.

**The indices are append-only.** `gone/index.md` and `expired/index.md` are opened
with `"a"` and never rewritten: once an advert is closed it is not coming back, and
an index that gets regenerated is an index that can lose a row.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from lead_identity import advert_url_key

#: The horizon: after this many days an advert we did not pursue stops being worth
#: checking (LJ, 2026-10-14).
HORIZON_DAYS = int(os.getenv("LOVEWORK_RETIRE_HORIZON_DAYS", "45"))

#: The decay half-life (LJ: "half-life 15 days so in 45 days it decays 3 times,
#: 1/8-th of the original probability"). A closed *or* stale advert is recorded
#: with the value it had reached, so the pile says how interesting it was, not
#: merely that it is over.
HALF_LIFE_DAYS = float(os.getenv("LOVEWORK_RETIRE_HALF_LIFE_DAYS", "15"))

#: Link verdicts that mean the advert is closed **to us**.
#:
#: `location_ineligible` is the odd one and belongs here: the advert is not gone,
#: it is *unworkable* — on-site in a place the principal may not work (LJ's
#: Poetiq case, 2026-10-14) — and holding a pack for it is exactly the low-SNR
#: clutter the piles exist to remove. The pile's index says which reason applied.
CLOSED_VERDICTS = ("expired", "unreachable", "location_ineligible")
#: Registry lifecycles that mean the same.
CLOSED_STATUSES = ("disappeared",)

#: HTTP statuses that mean the advert's page is **gone**, as opposed to unreadable.
#:
#: `unreachable` is the link check's word for "we did not get the page", and it
#: covers two different facts: `404`/`410` (the advert's page does not exist any
#: more — closure) and `403` (the site refused *us* — bot protection, a login wall,
#: a rate limit). Measured on kj 2026-10-14: of 25 `unreachable` verdicts, **19
#: were 404, 2 were 410 and 4 were 403**. Retiring on a 403 says "the advert is
#: closed" when what happened is "we were not allowed to look" — the same
#: conflation as the soft-404, from the other side.
GONE_STATUSES = (404, 410)

INDEX_NAME = "index.md"
LEDGER_NAME = "retired.jsonl"


def advance(days: float, *, half_life: float = HALF_LIFE_DAYS) -> float:
    """How much of its first-day value an advert still has after `days`.

    `0.5 ** (days / half_life)` — LJ's rule, and the number the index prints, so a
    retired advert carries *what it was worth*, not just that it ended.
    """
    return 0.5 ** (max(0.0, days) / half_life)


@dataclass
class Candidate:
    """One advert LoveWork holds material for."""
    principal: str
    kind: str            # "pack" (was a GO) | "lead" (was judged)
    directory: Path
    org: str
    role: str
    url: str
    first_seen: date
    decision: str
    score: float | None = None
    applied: bool = False
    closed_reason: str = ""
    signals: list[str] = field(default_factory=list)

    @property
    def pile(self) -> str:
        return "gone" if self.kind == "pack" else "expired"

    def days_old(self, today: date) -> int:
        return (today - self.first_seen).days

    def verdict(self, today: date, *, horizon: int = HORIZON_DAYS) -> str:
        """`keep`, or the reason to retire: `closed` or `stale-but-open`."""
        if self.closed_reason:
            return "closed"
        if self.days_old(today) > horizon:
            return "stale-but-open"
        return "keep"


def _date_from_name(name: str) -> date | None:
    match = re.match(r"(\d{4})(\d{2})(\d{2})-", name)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _pack_link(pack: Path) -> str:
    from application_packs import advert_url, markdown_link_target

    markdown = pack / "application_pack.md"
    if not markdown.is_file():
        return ""
    try:
        match = re.search(r"^- \*\*Link\*\*: (.+)$",
                          markdown.read_text(encoding="utf-8", errors="ignore"),
                          re.MULTILINE)
    except OSError:
        return ""
    if not match:
        return ""
    # The cell is a markdown link on a converted pack and bare on a legacy
    # one; `markdown_link_target` unwraps both, `advert_url` refuses the
    # placeholders. Recorded raw (2026-10-18, Together AI), the URL kept its
    # `[url](url)` wrapping — so `advert_url_key` never matched the block's
    # plain form, and the retirement was invisible to the URL identity the
    # view uses precisely for the names that truncate.
    value = advert_url(markdown_link_target(match.group(1)).strip("<>"))
    return value if "://" in value else ""


def _pack_parts(pack: Path) -> tuple[str, str]:
    from application_packs import pack_parts

    return pack_parts(pack)


def _lead_facts(lead: Path) -> dict:
    """Org, role, URL, decision and score from a lead page."""
    page = lead / "lead.md"
    text = ""
    if page.is_file():
        try:
            text = page.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
    facts: dict = {"url": "", "decision": "", "score": None}

    def field(name: str) -> str:
        match = re.search(rf"^- \*\*{re.escape(name)}\*\*:\s*(.+)$", text, re.MULTILINE)
        return match.group(1).strip() if match else ""

    url = field("Advert")
    if "://" in url:
        facts["url"] = url.split()[0]
    decision = field("Decision")
    if decision:
        facts["decision"] = decision.split()[0].upper().rstrip(",")
    score = re.search(r"(\d+(?:\.\d+)?)\s*/\s*10", decision or text[:2000])
    if score:
        facts["score"] = float(score.group(1))
    return facts


def _applied(state_root: Path, org: str, advert_hash: str) -> bool:
    """Is there a real submission record — not merely a prepared pack?

    LJ, 2026-10-14: *"if we genuinely applied, i.e. there is a record of
    application submission, not a mere application pack that was never followed
    on"*. Two authorities, either is enough: the pack's own `SUBMITTED` marker,
    and an outcome row the case-ingest pass wrote from the principal's case
    directory.
    """
    ledger = state_root / "dataset" / "outcomes.jsonl"
    if not ledger.is_file():
        return False
    try:
        with ledger.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if advert_hash and row.get("advert_hash") == advert_hash:
                    return True
                if org and str(row.get("case_org") or "").lower() == org.lower():
                    return True
    except OSError:
        return False
    return False


#: How alike two titles must be to be the *same* advert observed twice.
#:
#: Not exact equality: the title is extracted, and extraction varies. Optibrium's
#: Senior or Principal Scientist reads `– AL/ML` in the 2026-10-01 row and
#: `– AI/ML` in the 2026-10-11 row — one character — and with an exact key the two
#: rows never met, so a September/October `disappeared` closed a pack for an advert
#: the newest row says is open (2026-10-14). Distinct roles at one org stay apart:
#: "Lead Platform Engineer" scores far below this against any of them, and
#: "Commercial Contracts Manager" does not merge with "Senior Applied AI
#: Cheminformatician" (both measured).
TITLE_MERGE_RATIO = 0.90


def _advert_groups(rows: list[dict]) -> dict[str, list[dict]]:
    """Cluster registry rows into the adverts they describe.

    The registry is an append-only log, so one advert holds a row per source and
    URL spelling. Rows are the same advert when they name the same organisation
    **and** a title that is the same role however the extractor spelled it.
    """
    from difflib import SequenceMatcher

    by_org: dict[str, list[dict]] = {}
    for row in rows:
        org = str(row.get("org") or "").strip()
        title = str(row.get("title") or "").strip()
        if org and title:
            by_org.setdefault(org.casefold(), []).append(row)

    groups: dict[str, list[dict]] = {}
    for org_key, org_rows in by_org.items():
        clusters: list[list[dict]] = []
        for row in org_rows:
            title = str(row.get("title") or "").strip().casefold()
            for cluster in clusters:
                other = str(cluster[0].get("title") or "").strip().casefold()
                if title == other or SequenceMatcher(None, title, other).ratio() >= TITLE_MERGE_RATIO:
                    cluster.append(row)
                    break
            else:
                clusters.append([row])
        for index, cluster in enumerate(clusters):
            groups[f"{org_key}#{index}"] = cluster
    return groups


def _closed_signals(state_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(url → reason, advert-hash → reason) from the newest recorded checks.

    Two authorities, read once: the registry's lifecycle (`disappeared` means the
    source page lost it) and the link re-check's dated verdicts.
    """
    # Keyed by the canonical advert URL, not the spelling: a verdict recorded
    # against LinkedIn's `/comm/` form must attach to a pack that stores the plain
    # one (the 2026-10-14 John Lewis case — the closed verdict existed and never
    # met the pack).
    from application_packs import pack_title_key

    by_url: dict[str, str] = {}
    by_hash: dict[str, str] = {}
    registry = state_root / "cache" / "jobs.csv"
    if registry.is_file():
        try:
            with registry.open(encoding="utf-8", errors="replace", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except OSError:
            rows = []
        # **The newest observation of an advert is its lifecycle** (2026-10-14).
        #
        # The registry is an append-only *log*, so one advert holds several rows —
        # one per source and URL spelling, each with its own hash — and an older
        # row's `disappeared` was closing adverts that a newer row says are open.
        # Measured on kj: **46 of the 52 packs retired that day were closed by a
        # `disappeared` row**, and Boltz's *Small Molecule Computational Scientist*
        # was open the whole time — `still_open`, last seen 2026-10-11, from
        # `market_players` (the company's own board) — while three LinkedIn-digest
        # copies from September said `disappeared` and won by being read first.
        # LJ: "KJ GO-s got most of the closed. Investigate if there was some error
        # there." There was.
        #
        # Grouping is by the role the advert *is* (`pack_title_key`: org + title),
        # because the rows disagree precisely about the URL — that is why they are
        # separate rows. The verdict is then published under **every** spelling the
        # group carries, so a pack holding an old URL or hash gets the advert's
        # current state rather than the state of the row it happens to match.
        groups = _advert_groups(rows)

        def _seen(row: dict) -> tuple:
            """How late this row's observation is: last_seen, then first_seen, then id."""
            return (str(row.get("last_seen") or ""), str(row.get("first_seen") or ""),
                    int(str(row.get("id") or "0") or 0))

        for group in groups.values():
            newest = max(group, key=_seen)
            if str(newest.get("status") or "") not in CLOSED_STATUSES:
                continue                      # the advert's current state is open
            reason = f"registry: {newest.get('status')}"
            for row in group:
                key = advert_url_key(str(row.get("url") or ""))
                if key:
                    by_url.setdefault(key, reason)
                if row.get("hash"):
                    by_hash.setdefault(str(row["hash"]), reason)
    directory = state_root / "cache" / "link_checks"
    if directory.is_dir():
        for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for verdict in payload.get("verdicts", []):
                kind = verdict.get("verdict")
                if kind not in CLOSED_VERDICTS or not verdict.get("url"):
                    continue
                if kind == "unreachable" and verdict.get("status") not in GONE_STATUSES:
                    # "We could not read it" is not "it is gone" — see
                    # GONE_STATUSES. A 403 leaves the advert *unknown*, and an
                    # unknown advert is not a closed one.
                    continue
                by_url[advert_url_key(verdict["url"])] = f"link-check: {kind}"
    return by_url, by_hash


def candidates(principal: str, *, state_dir: Path | None = None) -> list[Candidate]:
    """Everything this principal holds material for, GO packs and judged leads."""
    state_dir = state_dir or config.STATE_DIR
    state_root = state_dir / principal
    by_url, by_hash = _closed_signals(state_root)
    out: list[Candidate] = []

    packs = state_root / "applications"
    for pack in sorted(packs.glob("*-LoveWork")) if packs.is_dir() else []:
        if not pack.is_dir() or pack.is_symlink():
            continue                      # a link is somebody else's material
        org, role = _pack_parts(pack)
        url = _pack_link(pack)
        first = _date_from_name(pack.name) or date.today()
        text = ""
        markdown = pack / "application_pack.md"
        if markdown.is_file():
            try:
                text = markdown.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
        identity = ""
        from application_packs import dossier_path

        dossier = dossier_path(pack)
        if dossier is not None:
            try:
                found = re.search(r"^Advert identity:\s*(\S+)",
                                  dossier.read_text(encoding="utf-8", errors="ignore"),
                                  re.MULTILINE)
                identity = found.group(1) if found else ""
            except OSError:
                identity = ""
        reason = by_url.get(advert_url_key(url), "") or by_hash.get(identity, "")
        out.append(Candidate(
            principal=principal, kind="pack", directory=pack, org=org, role=role,
            url=url, first_seen=first, decision="GO",
            applied=("LoveWork status: SUBMITTED" in text)
            or _applied(state_root, org, identity),
            closed_reason=reason))

    leads = state_root / "leads"
    for lead in sorted(leads.iterdir()) if leads.is_dir() else []:
        if not lead.is_dir() or lead.is_symlink():
            continue
        facts = _lead_facts(lead)
        parts = lead.name.split("-")
        org = parts[1].replace("_", " ") if len(parts) > 1 else lead.name
        role = " ".join(parts[2:-1]).replace("_", " ") if len(parts) > 2 else ""
        first = _date_from_name(lead.name) or date.today()
        out.append(Candidate(
            principal=principal, kind="lead", directory=lead, org=org, role=role,
            url=facts["url"], first_seen=first,
            decision=facts["decision"] or "MAYBE", score=facts["score"],
            closed_reason=by_url.get(advert_url_key(facts["url"]), "")))
    return out


def move_map_path(state_root: Path) -> Path:
    return state_root / "cache" / "moves.json"


def move_map(state_root: Path) -> dict:
    """old state-relative path → new one, from every retirement so far."""
    path = move_map_path(state_root)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def record_move(state_root: Path, old: Path, new: Path) -> None:
    """Remember where a directory went, so an old link still resolves.

    Relative to the principal's state root, because that is what a served URL is
    relative to — and because an absolute path would be host-local in a file that
    travels between hosts by git (the 2026-10-11 lesson).
    """
    try:
        key = str(old.relative_to(state_root))
        value = str(new.relative_to(state_root))
    except ValueError:
        return
    moves = move_map(state_root)
    if moves.get(key) == value:
        return
    moves[key] = value
    path = move_map_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(moves, indent=1, sort_keys=True), encoding="utf-8")
    except OSError:
        pass                               # a missing map breaks links, not data


def _compact(value: str) -> str:
    """Alphanumerics only, folded: how a pack slug and a report title agree."""
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def _effective_rows(principal: str, *, state_dir: Path | None = None) -> list[dict]:
    """The newest row about each retired directory — the piles are a log too.

    Append-only means a restoration is another row, and the same rule the rest of
    the system follows applies here: **the newest statement about a thing is its
    state**. A directory whose newest row is a restoration is not retired.
    """
    state_dir = state_dir or config.STATE_DIR
    newest: dict[str, tuple[str, dict]] = {}
    for pile in ("gone", "expired", "leads"):
        ledger = state_dir / principal / pile / LEDGER_NAME
        if not ledger.is_file():
            continue
        try:
            with ledger.open(encoding="utf-8", errors="replace") as handle:
                for position, line in enumerate(handle):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Keyed by the directory when the row has one — that is what a
                    # restoration names. A row without one (a hand-written or
                    # legacy record) still counts as a retirement: the org/role/url
                    # it carries is the advert, and dropping it would make the
                    # piles silently smaller than they are.
                    name = str(row.get("directory") or "") or "|".join(
                        (str(row.get("org") or ""), str(row.get("role") or ""),
                         str(row.get("url") or "")))
                    if not name.strip("|"):
                        continue
                    row.setdefault("_pile", pile)
                    stamp = (str(row.get("restored_on") or row.get("retired_on") or ""),
                             f"{position:08d}")
                    prior = newest.get(name)
                    if prior is None or stamp > prior[0]:
                        newest[name] = (stamp, row)
        except OSError:
            continue
    return [row for _stamp, row in newest.values() if not row.get("restored")]


def retired_keys(principal: str, *, state_dir: Path | None = None) -> set:
    """(org, role) of everything in this principal's two piles — one authority.

    Every builder that would otherwise *recreate* retired material asks this: the
    lead pages (it wanted to rebuild 534 retired leads for vj — measured
    2026-10-14), the pack cutter (a later sweep re-reporting a retired GO), the
    wiki index, and the audit. A retirement that a builder can undo is not a
    retirement.
    """
    keys: set = set()
    for row in _effective_rows(principal, state_dir=state_dir):
        keys.add((_compact(row.get("org", "")), _compact(row.get("role", ""))))
    return keys


def is_retired(org: str, role: str, keys: set) -> bool:
    return (_compact(org), _compact(role)) in keys


def retired_urls(principal: str, *, state_dir: Path | None = None) -> set:
    """The advert URLs in this principal's piles — the robust identity.

    The `(org, role)` key is what a *builder* has (it works from the pack or lead
    directory name), but a wiki **block** carries the organisation as the page
    names it — `40grid---full-time-remote` against the pile's `40grid` — so the
    audit needs the URL, which both sides have verbatim (found 2026-10-14).
    """
    urls: set = set()
    for row in _effective_rows(principal, state_dir=state_dir):
        url = advert_url_key(str(row.get("url") or ""))
        if url:
            urls.add(url)
    return urls


def index_row(candidate: Candidate, *, today: date, reason: str) -> dict:
    """The append-only row: what it was, why it closed, and what it was worth."""
    days = candidate.days_old(today)
    return {
        "retired_on": today.isoformat(),
        "principal": candidate.principal,
        "kind": candidate.kind,
        "org": candidate.org,
        "role": candidate.role,
        "url": candidate.url,
        "decision": candidate.decision,
        "score": candidate.score,
        "applied": candidate.applied,
        "first_seen": candidate.first_seen.isoformat(),
        "days_open": days,
        "closed": reason,
        "value_remaining": round(advance(days), 4),
        "directory": candidate.directory.name,
    }


def markdown_rows(rows: list[dict]) -> str:
    """The same rows as a readable table, for the pile's own index page."""
    lines = ["| retired | org | role | decision | applied | days open | why | "
             "value left |", "|---|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(
            f"| {row['retired_on']} | {row['org']} | {row['role']} | "
            f"{row['decision']} | {'yes' if row['applied'] else '—'} | "
            f"{row['days_open']} | {row['closed']} | "
            f"{row['value_remaining']:.0%} |")
    return "\n".join(lines) + "\n"


def _append_index(pile: Path, rows: list[dict], *, principal: str,
                  today: date, reason: str) -> None:
    """Append — never rewrite. The pile only grows."""
    if not rows:
        return
    pile.mkdir(parents=True, exist_ok=True)
    index = pile / INDEX_NAME
    new = not index.is_file()
    with index.open("a", encoding="utf-8") as handle:
        if new:
            if pile.name == "leads":
                header = (
                    f"# LEADS — {principal}\n\n"
                    "_Packs a review re-judged out of the GO set — the advert did "
                    "not close, our reading of it changed. The whole directory "
                    "moved from applications/, pack and notes preserved; nothing "
                    "deleted. Restorable by moving it back and re-reviewing._\n\n")
            elif pile.name == "gone":
                header = (
                    f"# GONE — {principal}\n\n"
                    "_Adverts that were a GO and are now closed. The material stays "
                    "here; a link at the old path points to it. Never checked "
                    "again._\n\n")
            else:
                header = (
                    f"# EXPIRED — {principal}\n\n"
                    "_Adverts we judged (MAYBE / FLAG / DROP) and that are now "
                    "closed. A record that we looked. Never checked again._\n\n")
            handle.write(header)
        # A batch mixes the two reasons, so the heading reports the mix rather
        # than whichever row happened to be first (it said "177 retired
        # (stale-but-open)" over a batch that was half closed — 2026-10-14).
        tally = {}
        for row in rows:
            tally[row["closed"]] = tally.get(row["closed"], 0) + 1
        handle.write(
            f"## {today.isoformat()} — {len(rows)} retired ("
            + ", ".join(f"{count} {name}" for name, count in sorted(tally.items()))
            + ")\n\n")
        handle.write(markdown_rows(rows))
        handle.write("\n")
    with (pile / LEDGER_NAME).open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def retire_one(principal: str, directory: str, reason: str, *,
               state_dir: Path | None = None, today: date | None = None,
               dry_run: bool = False) -> dict:
    """Retire one named advert with a reason the principal gives.

    The escape hatch the automatic reasons cannot provide: the Poetiq pack
    (2026-10-14) was kept alive because the *cached* advert said "unspecified"
    while the live page said Los Altos, on-site — visible to LJ and not yet to the
    re-check, whose per-day guard had already recorded today's verdict. The
    principal may close what he can see; the reason is recorded verbatim in the
    pile's index, so the pile never claims more than it was told.
    """
    state_dir = state_dir or config.STATE_DIR
    today = today or date.today()
    target = None
    for candidate in candidates(principal, state_dir=state_dir):
        if candidate.directory.name == directory:
            target = candidate
            break
    if target is None:
        return {"principal": principal, "error": f"no such advert: {directory}"}
    pile = state_dir / principal / target.pile
    destination = pile / target.directory.name
    row = index_row(target, today=today, reason=reason)
    if dry_run:
        return {"principal": principal, "would_retire": row}
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.rename(target.directory, destination)
    record_move(state_dir / principal, target.directory, destination)
    _append_index(pile, [row], principal=principal, today=today, reason=reason)
    return {"principal": principal, "retired": row, "pile": target.pile}


def retire(principal: str, *, state_dir: Path | None = None, today: date | None = None,
           dry_run: bool = False, limit: int | None = None,
           horizon: int = HORIZON_DAYS) -> dict:
    """Move every closed or stale advert into its pile. Idempotent.

    The directory moves whole and a relative symlink stays behind, so nothing is
    lost and no existing link breaks. A run with no candidates writes nothing.
    """
    state_dir = state_dir or config.STATE_DIR
    state_root = state_dir / principal
    today = today or date.today()
    result: dict = {"principal": principal, "moved": {"gone": 0, "expired": 0},
                    "rows": [], "dry_run": dry_run, "keep": 0}
    if not state_root.is_dir():
        return result

    by_pile: dict[str, list[dict]] = {"gone": [], "expired": []}
    moves: list[tuple[Candidate, str, Path]] = []
    for candidate in candidates(principal, state_dir=state_dir):
        reason = candidate.verdict(today, horizon=horizon)
        if reason == "keep":
            result["keep"] += 1
            continue
        pile = state_root / candidate.pile
        destination = pile / candidate.directory.name
        if destination.exists():
            continue                       # already retired (a re-run, or a race)
        moves.append((candidate, reason, destination))
    if limit is not None:
        moves = moves[:limit]

    for candidate, reason, destination in moves:
        row = index_row(candidate, today=today, reason=reason)
        by_pile[candidate.pile].append(row)
        if dry_run:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(candidate.directory, destination)
        except OSError as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"[:120]
            continue
        record_move(state_root, candidate.directory, destination)
    for pile_name, rows in by_pile.items():
        if rows and not dry_run:
            _append_index(state_root / pile_name, rows, principal=principal,
                          today=today, reason=rows[0]["closed"])
        result["moved"][pile_name] = len(rows)
        result["rows"].extend(rows)
    # **The index is rebuilt after a retirement** (2026-10-20). Without it the closing
    # piles left the index listing rows whose packs had moved: measured on vj, two Swiss
    # Re adverts sat in the GO section with `PACK` links pointing at `applications/`
    # directories that were in `gone/`, and their pages 404'd. The repairs and the
    # review-ruled pass already rebuilt; this pass — the one that moves the most — did not.
    if any(result["moved"].values()) and not dry_run:
        try:
            from wiki_store import WikiStore

            store = WikiStore(root=state_root / "wiki")
            store.rebuild_index(store._entries_from_org_pages(
                decisions=("GO", "MAYBE", "FLAG", "DROP")))
            result["index_rebuilt"] = True
        except Exception as exc:                # never fail the pass over the index
            result["index_rebuilt"] = f"{type(exc).__name__}: {exc}"[:120]
    return result


def retire_review_ruled(principal: str, *, state_dir: Path | None = None,
                        today: date | None = None,
                        dry_run: bool = False) -> dict:
    """Enact the review ledger's DROP rulings: the pack moves to `leads/`.

    LJ, 2026-10-18: *"So DROP should be acted on and that lead moved from
    applications/ to leads/ and the index updated. No information should be
    lost. … Only the entry the entire directory moves from applications/ to
    leads. We are after increasing SNR of the applications/, honing the
    applications to be maximally useful. … That we have to move a lead b/c it
    was wrongly judged 7.7 whereas now it's judged 0.0 - that's a plus."*

    The distinction from the two closing piles: `gone`/`expired` close because
    the **advert** closed or went stale — the world changed. A review-ruled
    DROP closes because **our reading** changed: the re-read scored the advert
    itself and the GO did not hold. The pack is our own work, so the directory
    moves whole (nothing deleted, the review note travels with it), the ledger
    row records the scores that moved it, and `retired_keys` picks it up from
    the `leads` pile like any other retirement — the view, the index and the
    builders all stop treating it as a live GO.

    Only **DROP** moves. A MAYBE/FLAG re-read is a downgrade, not a closure —
    the pack stays in `applications/` and the board carries the move. The
    authority is the folded review ledger (`loop/reviews.jsonl`, newest row per
    pack); a later review that says otherwise is a new newest row, and moving
    the directory back plus that row is the reversal path.
    """
    from tick import read_reviews

    state_dir = state_dir or config.STATE_DIR
    state_root = state_dir / principal
    today = today or date.today()
    result = {"principal": principal, "moved": 0, "rows": [],
              "dry_run": dry_run, "index_rebuilt": False}
    if not state_root.is_dir():
        return result
    reviews = read_reviews(state_root)
    if not reviews:
        return result
    applications = state_root / "applications"
    by_name = {c.directory.name: c for c in candidates(principal, state_dir=state_dir)
               if c.directory.parent == applications}
    moves: list[tuple] = []
    for name, row in sorted(reviews.items()):
        if str(row.get("new_decision") or "").upper() != "DROP":
            continue
        if not row.get("moved"):
            continue                  # a tier change always sets moved; be explicit
        candidate = by_name.get(name)
        if candidate is None:
            continue                  # already moved, or not a pack we hold
        destination = state_root / "leads" / name
        if destination.exists():
            continue                  # a name collision: never overwrite anything
        moves.append((candidate, row, destination))
    rows: list[dict] = []
    for candidate, row, destination in moves:
        reason = (f"review: {row.get('old_score')}→{row.get('new_score')} "
                  f"({row.get('model') or 're-read'})")
        index = index_row(candidate, today=today, reason=reason)
        rows.append(index)
        if dry_run:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(candidate.directory, destination)
        except OSError as exc:
            index["error"] = f"{type(exc).__name__}: {exc}"[:120]
            continue
        record_move(state_root, candidate.directory, destination)
    if rows and not dry_run:
        _append_index(state_root / "leads", rows, principal=principal,
                      today=today, reason="review-ruled")
        # The index is built by the crawl; a retirement that lands after it
        # leaves the index listing a GO the decision space no longer holds —
        # the same stale-index class the repairs close with their own rebuild
        # (2026-10-17). Rebuild only when something actually moved.
        try:
            from wiki_store import WikiStore

            store = WikiStore(root=state_root / "wiki")
            store.rebuild_index(store._entries_from_org_pages(
                decisions=("GO", "MAYBE", "FLAG", "DROP")))
            result["index_rebuilt"] = True
        except Exception as exc:      # never fail the pass over the index
            result["index_rebuilt"] = f"{type(exc).__name__}: {exc}"[:120]
    result["moved"] = sum(1 for r in rows if not r.get("error"))
    result["rows"] = rows
    return result


def retire_linkless(principal: str, *, state_dir: Path | None = None,
                    today: date | None = None, dry_run: bool = False) -> dict:
    """A GO with **no advert link anywhere** is not a GO — there is nothing to apply to.

    LJ, 2026-10-20, on Barnett Waddingham at 9.7: *"NO LINK … Well then — there is nothing
    there to apply for or to. This should not be in the GO section. Should not be even in
    the MAYBE section. There is NOTHING there to be done, as there is no advert?? No link
    no text nothing. We only have the name of the firm."*

    He is right, and it is the strongest statement in the set: a GO is a promise that
    prepared material is waiting, and an advert we cannot even open breaks the promise
    before any work starts. Unlike `retire_unpreparable` this needs **no pack directory** —
    there may be nothing on disk at all — so the retirement is a ledger row (the identity
    the view filters on) rather than a directory move. Nothing is deleted because there is
    nothing to delete; what changes is that the row leaves the decision space.
    """
    from application_packs import pack_for, pack_index
    from tick import pack_advert_url
    from wiki_store import WikiStore

    state_dir = state_dir or config.STATE_DIR
    state_root = state_dir / principal
    today = today or date.today()
    result = {"principal": principal, "moved": 0, "rows": [], "dry_run": dry_run,
              "considered": 0, "index_rebuilt": False}
    if not (state_root / "wiki").is_dir():
        return result
    try:
        store = WikiStore(root=state_root / "wiki", create=False)
        _current, fresh = store.current_view(today=today)
        packs = pack_index(principal, root=state_root / "applications")
    except Exception as exc:                    # never silently swallow
        result["error"] = f"{type(exc).__name__}: {exc}"[:160]
        return result

    rows: list[dict] = []
    for entry in fresh:
        if entry.decision != "GO":
            continue
        result["considered"] += 1
        pack = pack_for(packs, entry.org_name, entry.title,
                        entry.original_url or entry.url or "")
        if pack is not None and pack_advert_url(pack):
            continue                            # the pack has a link: work, not closure
        if (entry.original_url or entry.url or "").strip():
            continue                            # the block's own link: ditto
        # The row carries every column the pile's own table renders — a ledger row that
        # cannot be printed is a row the reader cannot audit (`markdown_rows` raised
        # `KeyError: 'retired_on'` on the first live run, which is how this list got
        # written down).
        rows.append({"closed": "no advert link", "date": today.isoformat(),
                     "retired_on": today.isoformat(),
                     "org": entry.org_name, "role": entry.title, "url": "",
                     "decision": entry.decision, "score": entry.score or 0.0,
                     "applied": False, "days_open": 0, "value_remaining": 0.0,
                     "directory": "",
                     "reason": "no advert link and no retained text — nothing to "
                               "apply to"})
    if rows and not dry_run:
        _append_index(state_root / "leads", rows, principal=principal, today=today,
                      reason="linkless")
        try:
            store.rebuild_index(store._entries_from_org_pages(
                decisions=("GO", "MAYBE", "FLAG", "DROP")))
            result["index_rebuilt"] = True
        except Exception as exc:                # never fail the pass over the index
            result["index_rebuilt"] = f"{type(exc).__name__}: {exc}"[:120]
    result["rows"] = rows
    result["moved"] = len(rows)
    return result


def retire_unpreparable(principal: str, *, state_dir: Path | None = None,
                        today: date | None = None, dry_run: bool = False) -> dict:
    """A GO whose pack **cannot be prepared** is not a GO — it is a lead.

    LJ, 2026-10-19: *"either application pack is created => remains on GO; or if we
    fail to create an application pack, we retry; but if not possible to create a pack
    b/c the advert is gone and we no longer have it => then this should not be in GO,
    should be in leads."*

    This is the third closing reason, and it is distinct from the other two
    (docs/45): `gone`/`expired` close because the **world** changed, a review-ruled
    DROP closes because **our reading** changed, and this one closes because the
    work cannot be done at all — the pack carries no advert to write material from,
    so the promise a GO makes ("prepared material, ready to apply") is one nothing
    can keep. Retry is the loop's job (`decide._go_pack_work`); this is where the
    retry's *permanent* failure goes.

    The narrow condition is deliberate: **no advert URL and no retained advert
    text**. A link that resolves is work, not a closure, and a link that is dead is
    the link check's and the review's to judge — moving those here would retire
    adverts whose only problem is that nobody has fetched them yet.
    """
    from tick import pack_advert_url          # the one reader of a pack's advert link

    from application_packs import go_identities, pack_index, pack_readiness

    state_dir = state_dir or config.STATE_DIR
    state_root = state_dir / principal
    today = today or date.today()
    result = {"principal": principal, "moved": 0, "rows": [], "dry_run": dry_run,
              "considered": 0, "index_rebuilt": False}
    applications = state_root / "applications"
    if not applications.is_dir():
        return result
    try:
        current_go = go_identities(principal)
    except Exception as exc:            # never silently swallow
        result["error"] = f"go_identities: {type(exc).__name__}: {exc}"[:160]
        return result
    packs = pack_index(principal, root=applications)
    by_name = {c.directory.name: c for c in candidates(principal, state_dir=state_dir)
               if c.directory.parent == applications}
    for identity, directory in sorted(packs.items(), key=lambda kv: str(kv[1])):
        if identity not in current_go:
            continue                    # not a live GO: another pass owns it
        if str(directory.parent) != str(applications):
            continue                    # already moved (a pile, a lead, a gone/)
        readiness = pack_readiness(directory)
        if readiness["ready"]:
            continue
        result["considered"] += 1
        if pack_advert_url(directory):
            continue                    # there IS an advert: retry, don't retire
        candidate = by_name.get(directory.name)
        if candidate is None:
            continue                    # nothing to index: leave it alone
        index = index_row(candidate, today=today,
                          reason="no advert URL and no retained advert text "
                                 "— a pack cannot be prepared")
        result["rows"].append(index)
        if dry_run:
            continue
        destination = state_root / "leads" / directory.name
        if destination.exists():
            index["error"] = "a lead of that name exists: never overwrite"
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(directory, destination)
        except OSError as exc:
            index["error"] = f"{type(exc).__name__}: {exc}"[:120]
            continue
        record_move(state_root, directory, destination)
    moved = [r for r in result["rows"] if not r.get("error")]
    if moved and not dry_run:
        _append_index(state_root / "leads", moved, principal=principal,
                      today=today, reason="unpreparable")
        try:
            from wiki_store import WikiStore

            store = WikiStore(root=state_root / "wiki")
            store.rebuild_index(store._entries_from_org_pages(
                decisions=("GO", "MAYBE", "FLAG", "DROP")))
            result["index_rebuilt"] = True
        except Exception as exc:        # never fail the pass over the index
            result["index_rebuilt"] = f"{type(exc).__name__}: {exc}"[:120]
    result["moved"] = len(moved)
    return result


def restore(principal: str, *, state_dir: Path | None = None,
            today: date | None = None, dry_run: bool = False) -> dict:
    """Bring back adverts whose closure evidence **no longer holds**.

    LJ, 2026-10-14: *"KJ GO-s got most of the closed. Investigate if there was some
    error there."* There was, in two ways — a superseded registry row and a 403 read
    as a closure — and a repair that only fixes the rule leaves the damage in place.
    A retirement is a claim about evidence; when the evidence is gone, the claim is
    gone with it.

    Only rows whose `closed` was a **closure** claim are reconsidered: an advert
    retired for `stale-but-open` was aged out by the horizon, which is a decision
    about the principal's attention, not a claim about the advert.

    Append-only, like everything in the piles: the restoration is a *new* row, and
    `_effective_rows` reads the newest statement about a directory as its state.
    """
    state_dir = state_dir or config.STATE_DIR
    state_root = state_dir / principal
    today = today or date.today()
    out: dict = {"principal": principal, "restored": [], "kept": 0,
                 "no_evidence": 0, "dry_run": dry_run}
    if not state_root.is_dir():
        return out
    by_url, by_hash = _closed_signals(state_root)

    touched: set[str] = set()
    for row in _effective_rows(principal, state_dir=state_dir):
        pile = str(row.get("_pile") or "gone")
        if str(row.get("closed") or "") != "closed":
            out["kept"] += 1                    # aged out, not closed on evidence
            continue
        name = str(row.get("directory") or "")
        source = state_root / pile / name
        if not name or not source.is_dir():
            continue
        identity = ""
        from application_packs import dossier_path

        dossier = dossier_path(source)
        if dossier is not None:
            try:
                found = re.search(r"(?m)^Advert identity:\s*(\S+)",
                                  dossier.read_text(encoding="utf-8", errors="ignore"))
                identity = found.group(1) if found else ""
            except OSError:
                identity = ""
        signal = (by_url.get(advert_url_key(str(row.get("url") or "")), "")
                  or by_hash.get(identity, ""))
        if signal:
            out["kept"] += 1                    # still closed, on evidence
            continue
        out["no_evidence"] += 1
        kind = str(row.get("kind") or "pack")
        home = "applications" if kind == "pack" else "leads"
        destination = state_root / home / name
        if destination.exists():
            continue                            # already back (a re-run)
        if dry_run:
            out["restored"].append({**row, "restored_on": today.isoformat()})
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(source, destination)
        except OSError as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"[:120]
            continue
        record_move(state_root, source, destination)
        restored = {**row, "restored": True, "restored_on": today.isoformat(),
                    "restored_reason": "no closure evidence remains"}
        with (state_root / pile / LEDGER_NAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(restored, sort_keys=True) + "\n")
        out["restored"].append(restored)
        touched.add(pile)

    if out["restored"] and not dry_run:
        for pile in sorted(touched):
            names = [r for r in out["restored"] if r.get("_pile", "gone") == pile]
            index = state_root / pile / INDEX_NAME
            if names and index.is_file():
                with index.open("a", encoding="utf-8") as handle:
                    handle.write(f"\n## {today.isoformat()} — {len(names)} restored "
                                 f"(the closure evidence no longer holds)\n\n")
                    handle.write(markdown_rows(names))
                    handle.write("\n")
    return out


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="all")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                        help="retire at most this many (the first cull may be large)")
    parser.add_argument("--horizon", type=int, default=HORIZON_DAYS)
    parser.add_argument("--pack", default="", help="retire this one directory")
    parser.add_argument("--reason", default="", help="the reason to record with --pack")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--restore", action="store_true",
                        help="bring back adverts whose closure evidence no longer holds")
    parser.add_argument("--review-only", action="store_true",
                        help="only enact review-ruled DROPs (packs -> leads/)")
    parser.add_argument("--unpreparable-only", action="store_true",
                        help="only move GOs whose pack cannot be prepared to leads/")
    parser.add_argument("--linkless-only", action="store_true",
                        help="only record GOs with no advert link anywhere (nothing "
                             "to apply to) as retired, so the view drops them")
    args = parser.parse_args()
    principals = list(config.principal_names(config.STATE_DIR)) \
        if args.profile == "all" else [args.profile]
    if args.linkless_only:
        results = [retire_linkless(p, today=None, dry_run=args.dry_run)
                   for p in principals]
        if args.json:
            print(json.dumps(results, indent=2, sort_keys=True))
            return 0
        for result in results:
            print(f"{result['principal']}: {result['moved']} linkless GO(s) -> retired "
                  f"({result['considered']} GO(s) considered)"
                  + ("  (dry run)" if result["dry_run"] else ""))
            for row in result["rows"][:8]:
                print(f"   {row['org'][:34]} — {str(row['role'])[:40]}  ({row['score']})")
        return 0
    if args.unpreparable_only:
        results = [retire_unpreparable(p, today=None, dry_run=args.dry_run)
                   for p in principals]
        if args.json:
            print(json.dumps(results, indent=2, sort_keys=True))
            return 0
        for result in results:
            print(f"{result['principal']}: {result['moved']} unpreparable GO(s) -> leads "
                  f"({result['considered']} unprepared pack(s) considered)"
                  + ("  (dry run)" if result["dry_run"] else ""))
            for row in result["rows"][:8]:
                print(f"   {row['org'][:34]} — {str(row['role'])[:40]}")
        return 0
    if args.review_only:
        results = [retire_review_ruled(p, today=None, dry_run=args.dry_run)
                   for p in principals]
        if args.json:
            print(json.dumps(results, indent=2, sort_keys=True))
            return 0
        for result in results:
            print(f"{result['principal']}: {result['moved']} review-ruled pack(s) "
                  f"-> leads" + ("  (dry run)" if result["dry_run"] else ""))
            for row in result["rows"][:8]:
                print(f"   {row['closed']:34s} {row['decision']:6s} "
                      f"{row['org'][:32]} — {str(row['role'])[:36]}")
        return 0
    if args.pack:
        import json as _json

        print(_json.dumps(retire_one(args.profile, args.pack,
                                     args.reason or "retired by the principal",
                                     dry_run=args.dry_run), indent=1, sort_keys=True))
        return 0
    if args.restore:
        results = [restore(p, dry_run=args.dry_run) for p in principals]
        if args.json:
            print(json.dumps(results, indent=2, sort_keys=True))
            return 0
        for result in results:
            print(f"{result['principal']}: restored {len(result['restored'])} "
                  f"(no closure evidence remains) · kept {result['kept']} still closed"
                  + ("  (dry run)" if result["dry_run"] else ""))
            for row in result["restored"][:8]:
                print(f"   {row.get('decision', ''):5s} {row.get('org', '')[:30]} — "
                      f"{str(row.get('role', ''))[:38]}")
        return 0
    out = []
    for principal in principals:
        result = retire(principal, dry_run=args.dry_run, limit=args.limit,
                        horizon=args.horizon)
        out.append(result)
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    for result in out:
        print(f"{result['principal']}: retired {result['moved']['gone']} to gone, "
              f"{result['moved']['expired']} to expired · kept {result['keep']}"
              + ("  (dry run)" if result["dry_run"] else ""))
        for row in result["rows"][:6]:
            print(f"   {row['closed']:15s} {row['decision']:6s} "
                  f"{row['days_open']:4d}d {row['value_remaining']:5.0%} "
                  f"{row['org'][:34]} — {row['role'][:34]}")
    # The review-ruled DROPs ride the same pass: the hourly maintenance runs
    # this bare invocation, so a review that drops a GO is enacted within the
    # hour without a second job (LJ, 2026-10-18).
    reviews = [retire_review_ruled(p, today=None, dry_run=args.dry_run)
               for p in principals]
    for result in reviews:
        if result["moved"]:
            print(f"{result['principal']}: {result['moved']} review-ruled pack(s) "
                  f"-> leads" + ("  (dry run)" if result["dry_run"] else ""))
    # …and the third reason, on the same footing (LJ, 2026-10-19): a GO whose pack
    # **cannot** be prepared — no advert URL and no retained advert text — is not a
    # GO. It rides this pass for the same reason the review sweep does: the hourly
    # maintenance runs the bare invocation, so nothing needs a second job and
    # nothing depends on someone remembering.
    linkless = [retire_linkless(p, today=None, dry_run=args.dry_run)
                for p in principals]
    for result in linkless:
        if result["moved"]:
            print(f"{result['principal']}: {result['moved']} linkless GO(s) retired "
                  f"(no advert to apply to)"
                  + ("  (dry run)" if result["dry_run"] else ""))
    unpreparable = [retire_unpreparable(p, today=None, dry_run=args.dry_run)
                    for p in principals]
    for result in unpreparable:
        if result["moved"]:
            print(f"{result['principal']}: {result['moved']} unpreparable GO(s) "
                  f"-> leads" + ("  (dry run)" if result["dry_run"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
