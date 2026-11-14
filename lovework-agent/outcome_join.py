"""Phase A of `docs/52` — make the outcome ledger attributable, and stop it lying.

**The contract (v1).**

* reads `state/<p>/dataset/outcomes.jsonl` — **never writes it**; the ledger is
  append-only, so a repair is a derived view or a new row, never an edit.
* reads the case directories (names only) from the principal's applications
  root and from the unified view; either may be absent.
* writes one derived artefact, `state/<p>/dataset/funnel.jsonl`: sorted,
  attributed, and rebuildable. Same input bytes in, byte-identical file out.
* prints a gate report; `--json` makes it machine-readable.

**Why it exists.** Measured on lj, 2026-11-09 (`docs/52` §1.2):

- `org_name` is the wrong name on 929 of 1,694 rows (=54.9%) — every one of them
  `"Iai"` — while `path` still names the real company;
- 43 rows carry entity junk (`"Near you"`, `"Leerink Partners New York, NY"`);
- **all 50 rows in the `interview`/`offer` kinds are job-alert digests**, not
  outcomes: 44 from "JJ Alerts", plus JobLeads, LinkedIn and CWJobs. Their
  subjects read "Jobs at … Trading Interview …" and the old classifier matched
  the word. `history._classify_email` now returns `job_alert` for them and
  `snapshot` no longer writes them; this module quarantines the rows already on
  disk so the funnel cannot be read as if they were real;
- **0 of the real interviews join to a case**, because there are none recorded —
  which is itself the finding.

**Portability.** The two hosts write absolute paths and merge the same ledger, so
nothing here trusts a prefix: a row's case is the last path component that looks
like `YYYYMMDD-Org-Role`. The organisation is recovered by **right-aligning the
row's own role** against that component, which beats splitting on `-`
(`20261024-d-Matrix-Senior_Staff_…` would otherwise yield the org `d`).

**Determinism.** Rows are keyed and sorted; no clock enters the file. Rebuilding
after a no-op run produces the same bytes, so a diff is evidence about the data.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import config
import history
import mem
from cases import _slugify_part
from principal_runtime import resolve_principal_runtime

SCHEMA = "funnel-v1"

#: The case directory a row's path points at, wherever the row was written.
CASE_RE = re.compile(r"^(?P<date>\d{8})-(?P<rest>[A-Za-z0-9][A-Za-z0-9._-]*)$")

PACK_SUFFIX = "-LoveWork"
ATA_SUFFIX = "-LoveWork-ATA"

#: Names a source emits that are not organisations. `mem.NOT_AN_ORG` holds the
#: prose-shaped noise; these are the labels a board or a mailbox uses for itself.
SOURCE_LABELS = frozenset({
    "hn", "hacker news", "linkedin", "indeed", "glassdoor", "totaljobs",
    "cwjobs", "reed", "gmail", "digitaljobs", "harnham", "jackandjill",
    "jack and jill", "compbiojobs", "neolabs", "neoclouds", "research orgs",
    "market players", "company pages", "linkedin related", "speech companies",
    "cheminfo companies", "hf startups", "hn hiring", "hn jobs",
})

#: Kinds that mean "the principal acted on something". `job_alert` and
#: `decay_expiration` are deliberately absent: one is a newsletter, the other is
#: an inference about silence.
OUTCOME_KINDS = ("application_sent", "applied", "interview", "offer", "rejection")

#: How much a join can be trusted, reported per row and never hidden.
CONFIDENCE_EXACT = 1.0     # the same advert hash on a row that already has a case
CONFIDENCE_ORG_ROLE = 0.9  # the case corpus has exactly this org + role
CONFIDENCE_ORG_DATE = 0.6  # the org matches and the dates are close


@dataclass(frozen=True)
class CaseRef:
    """One case directory: the unit an outcome belongs to."""

    name: str
    when: Optional[date]
    org: str
    role: str
    stream: str                 # "manual" | "pack" | "ata"

    @property
    def org_slug(self) -> str:
        return _slug(self.org)

    @property
    def role_slug(self) -> str:
        return _slug(self.role)


@dataclass
class FunnelRow:
    """The funnel for one case. Fields are a contract: see the module docstring."""

    case: str
    org: str
    role: str
    stream: str
    events: Counter = field(default_factory=Counter)
    first: str = ""
    latest: str = ""

    def add(self, kind: str, when: str) -> None:
        self.events[kind] += 1
        if when:
            if not self.first or when < self.first:
                self.first = when
            if not self.latest or when > self.latest:
                self.latest = when

    def as_dict(self, schema: str = SCHEMA) -> dict:
        interviewed = self.events.get("interview", 0) + self.events.get("offer", 0)
        resolved = interviewed + self.events.get("rejection", 0)
        return {
            "schema": schema,
            "case": self.case,
            "org": self.org,
            "role": self.role,
            "stream": self.stream,
            "first": self.first,
            "latest": self.latest,
            "applied": self.events.get("applied", 0) + self.events.get("application_sent", 0),
            "interview": interviewed,
            "rejection": self.events.get("rejection", 0),
            "offer": self.events.get("offer", 0),
            # A ratio that hides its inputs is not a fact (docs/52 house rule).
            "interview_share": (round(interviewed / resolved, 4) if resolved else None),
            "resolved": resolved,
        }


# ── names ────────────────────────────────────────────────────────────────


def _slug(value: str) -> str:
    """A comparable form of a name: `cases._slugify_part` lowercased, hyphens out.

    The *same* function that minted the directory names, then reduced to
    alphanumerics. Punctuation carries no identity here and the two sides do not
    agree on it: the ledger's "Forward‑Deployed AI Engineer" (U+2011) must meet
    the directory's "Forward-Deployed", and "a past employer" must meet the
    directory's "G_Research".
    """
    return re.sub(r"[^a-z0-9]", "", _slugify_part(value).lower())


def case_name_from_path(path: str) -> str:
    """The case directory a ledger path names, or "" — prefix-independent.

    A row may point at the case directory or at a file inside it; a merged
    ledger carries `/Users/…` and `/opt/…` prefixes from two hosts. Only the
    components matter, and only the last one that parses is used.
    """
    if not path:
        return ""
    for part in reversed(Path(str(path)).parts):
        # A row may name the dossier file rather than its directory.
        candidates = [part]
        stem = Path(part).stem
        if stem != part and Path(part).suffix.lower() in (".txt", ".md", ".json", ".pdf"):
            candidates.insert(0, stem)      # the dossier file, not the case name
        for candidate in candidates:
            if CASE_RE.match(candidate):
                return candidate
    return ""


def parse_case(name: str, role_hint: str = "") -> Optional[CaseRef]:
    """`YYYYMMDD-Org-Role[-LoveWork]` → a CaseRef.

    The organisation is whatever is left when the role is peeled off the right;
    `role_hint` is the row's own role, which is what makes a hyphenated
    organisation (`d-Matrix`) recoverable. Without a hint the first segment wins
    — the rule `application_packs.pack_parts` already uses.
    """
    m = CASE_RE.match(name or "")
    if not m:
        return None
    when = _parse_date(m.group("date"))
    rest = m.group("rest")
    stream = "manual"
    if rest.endswith(ATA_SUFFIX):
        rest, stream = rest[: -len(ATA_SUFFIX)], "ata"
    elif rest.endswith(PACK_SUFFIX):
        rest, stream = rest[: -len(PACK_SUFFIX)], "pack"

    org, role = "", rest.replace("_", " ")
    parts = rest.split("-")
    hint = _slug(role_hint)
    if hint:
        # Segment-aware and slug-normalised: compare the slug of the *remaining
        # tail*, never a character suffix, so punctuation cannot defeat it.
        for cut in range(len(parts) - 1, 0, -1):
            if _slug("-".join(parts[cut:])) == hint:
                org = "-".join(parts[:cut]).replace("_", " ")
                role = role_hint
                break
    if not org:
        org = parts[0].replace("_", " ")
        role = " ".join(parts[1:]).replace("_", " ") if len(parts) > 1 else ""
    return CaseRef(name=name, when=when, org=org, role=role, stream=stream)


def _parse_date(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def agrees_with_name(org: str, name: str) -> bool:
    """Whether a declared organisation could have produced this case name.

    The consistency test that lets the column win when it is right: `"Iai"` on a
    path called `20250716-HRT-Hudson-River-Trading` does not agree, so the path
    wins; `"Prime Intellect"` on `20250719-Prime-Intellect` does agree, so the
    column wins and the role is not mangled into "Intellect".
    """
    m = CASE_RE.match(name or "")
    if not m or not org:
        return False
    rest = m.group("rest")
    for suffix in (ATA_SUFFIX, PACK_SUFFIX):
        if rest.endswith(suffix):
            rest = rest[: -len(suffix)]
            break
    return _slug(rest).startswith(_slug(org))


def org_vocabulary(rows: Iterable[dict]) -> set[str]:
    """The organisation slugs the ledger itself asserts, for name refinement."""
    vocab = set()
    for row in rows:
        declared = str(row.get("org_name") or "").strip()
        if is_org(declared):
            vocab.add(_slug(declared))
    return vocab


def refine_org(ref: CaseRef, name: str, vocab: set[str]) -> CaseRef:
    """Split a case name at the longest organisation the ledger already knows.

    `20250719-Prime-Intellect` is the org *Prime Intellect*, not the org *Prime*
    with the role *Intellect*; the separator is the same `-` on both sides, so
    the only evidence is which candidate the rest of the data recognises. Falls
    back to the first segment when nothing is recognised.
    """
    m = CASE_RE.match(name)
    if not m or not vocab:
        return ref
    rest = m.group("rest")
    for suffix in (ATA_SUFFIX, PACK_SUFFIX):
        if rest.endswith(suffix):
            rest = rest[: -len(suffix)]
            break
    parts = rest.split("-")
    for cut in range(len(parts) - 1, 0, -1):
        candidate = "-".join(parts[:cut]).replace("_", " ")
        if _slug(candidate) in vocab:
            role = " ".join(parts[cut:]).replace("_", " ")
            if candidate != ref.org:
                return CaseRef(name=ref.name, when=ref.when, org=candidate,
                               role=role, stream=ref.stream)
            return ref
    return ref


def is_org(name: str, *, from_path: bool = False) -> bool:
    """Whether a name may be used as an organisation in a fact.

    `mem`'s two guards are the authority for prose-shaped noise; this adds the
    labels a *source* uses for itself (`hn`, `linkedin`), which reach the ledger
    as organisations because a parser had nothing better.

    `from_path` relaxes only the label guard: a case directory's organisation is
    an entity the principal worked with by construction — Harnham is a recruiter
    LJ applies *through*, so it is a valid case org even though `harnham` is also
    a source name. The ledger column gets no such benefit of the doubt.
    """
    text = (name or "").strip()
    if not text or not mem.is_org_name(text):
        return False
    if not from_path and text.casefold() in SOURCE_LABELS:
        return False
    return bool(mem.slug(text))


# ── the ledger ───────────────────────────────────────────────────────────


def load_rows(path: Path) -> list[dict]:
    """Every parseable row of the append-only ledger. Unparseable lines are skipped."""
    rows: list[dict] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def resolve_org(row: dict) -> tuple[str, str]:
    """The row's organisation and where it came from: `(org, source)`.

    **The path wins.** The case directory is the identity the principal worked
    with, and it is the only field the two hosts still agree on after a merge;
    the `org_name` column is what the scanners got wrong. `source` is `path`,
    `ledger`, or `none`.
    """
    name = case_name_from_path(str(row.get("path") or ""))
    declared = str(row.get("org_name") or "").strip()
    if name:
        # The column wins when it agrees with the directory; the path wins when
        # it does not (the measured 929 `"Iai"` rows), because the directory is
        # the identity the principal worked with.
        if is_org(declared) and agrees_with_name(declared, name):
            return declared, "ledger"
        ref = parse_case(name, str(row.get("role") or ""))
        if ref and is_org(ref.org, from_path=True):
            return ref.org, "path"
        return "", "none"
    if is_org(declared):
        return declared, "ledger"
    return "", "none"


def resync_rows(rows: Iterable[dict]) -> list[dict]:
    """The rows with a trustworthy `org_name`, in memory only.

    The ledger is not edited: this is the view every reader should use. Each row
    keeps `org_source` so a consumer can see which names were repaired.
    """
    out = []
    for row in rows:
        org, source = resolve_org(row)
        fixed = dict(row)
        fixed["org_name"] = org
        fixed["org_source"] = source
        out.append(fixed)
    return out


def alert_reason(row: dict) -> str:
    """Why this row is a board's digest rather than an outcome, or "".

    Two independent signatures, either sufficient: the sender/subject shape the
    classifiers share, or the evidence shape a real outcome never has (no
    advert, no role, no case).
    """
    if history._is_job_alert(str(row.get("subject") or ""), str(row.get("from") or "")):
        return "board_digest"
    if (str(row.get("kind") or "") in ("interview", "offer")
            and not any(row.get(k) for k in ("advert_hash", "role", "path"))):
        return "no_evidence"
    return ""


def outcome_kind(row: dict) -> tuple[str, str]:
    """`(kind, quarantine_reason)` — the kind the funnel may count, and why not."""
    reason = alert_reason(row)
    if reason:
        return "job_alert", reason
    kind = str(row.get("kind") or "").strip()
    return kind, ""


# ── joining an outcome to the case that produced it ──────────────────────


def case_index(rows: Iterable[dict], *, applications_dir: Optional[Path] = None,
               packs_dir: Optional[Path] = None) -> dict[str, CaseRef]:
    """Every case this host can see, from the ledger and from the disk.

    Two sources, because neither is complete alone: the ledger names cases that
    may have been pruned from disk, and the disk holds cases that never produced
    an outcome row.
    """
    rows = list(rows)
    vocab = org_vocabulary(rows)
    refs: dict[str, CaseRef] = {}
    for row in rows:
        name = case_name_from_path(str(row.get("path") or ""))
        if name and name not in refs:
            ref = parse_case(name, str(row.get("role") or ""))
            if ref:
                declared = str(row.get("org_name") or "").strip()
                if (is_org(declared) and agrees_with_name(declared, name)
                        and str(row.get("role") or "").strip()):
                    ref = CaseRef(name=name, when=ref.when, org=declared,
                                  role=str(row.get("role")).strip(), stream=ref.stream)
                refs[name] = refine_org(ref, name, vocab)
    for root in (applications_dir, packs_dir):
        if not root:
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name in refs or not CASE_RE.match(name):
                continue
            ref = parse_case(name)
            if ref:
                refs[name] = refine_org(ref, name, vocab)
    return refs


def attribute(rows: Iterable[dict], refs: dict[str, CaseRef]) -> dict[int, dict]:
    """Attach a case to every outcome row that has none. `{row_index: join}`.

    Three keys, strongest first, method recorded rather than hidden:

    1. **exact advert hash** — another row for the same advert already names a case;
    2. **org + role** — the case corpus has exactly this pair;
    3. **org + date window** — the org matches and the dates are within a
       fortnight, which is enough for "which application is this?" and no more.
    """
    by_hash: dict[str, str] = {}
    by_org_role: dict[tuple[str, str], list[str]] = defaultdict(list)
    by_org: dict[str, list[CaseRef]] = defaultdict(list)
    for name, ref in refs.items():
        by_org[ref.org_slug].append(ref)
        by_org_role[(ref.org_slug, ref.role_slug)].append(name)

    rows = list(rows)
    for row in rows:
        name = case_name_from_path(str(row.get("path") or ""))
        h = str(row.get("advert_hash") or "")
        if name and h:
            by_hash.setdefault(h, name)

    joins: dict[int, dict] = {}
    for index, row in enumerate(rows):
        if outcome_kind(row)[0] not in OUTCOME_KINDS:
            continue
        if case_name_from_path(str(row.get("path") or "")):
            continue
        h = str(row.get("advert_hash") or "")
        if h and h in by_hash:
            joins[index] = {"case": by_hash[h], "method": "advert_hash",
                            "confidence": CONFIDENCE_EXACT}
            continue
        org, _ = resolve_org(row)
        org_slug = _slug(org)
        role_slug = _slug(str(row.get("role") or ""))
        if not org_slug:
            continue
        if role_slug:
            names = by_org_role.get((org_slug, role_slug)) or []
            if len(names) == 1:
                joins[index] = {"case": names[0], "method": "org_role",
                                "confidence": CONFIDENCE_ORG_ROLE}
                continue
        when = _row_date(row)
        candidates = by_org.get(org_slug) or []
        if when and candidates:
            same_day = [c for c in candidates if c.when == when]
            if len(same_day) == 1:
                joins[index] = {"case": same_day[0].name, "method": "org_date_exact",
                                "confidence": CONFIDENCE_EXACT}
                continue
            close = [c for c in candidates
                     if c.when and abs((c.when - when).days) <= 14]
            if len(close) == 1:
                joins[index] = {"case": close[0].name, "method": "org_date",
                                "confidence": CONFIDENCE_ORG_DATE}
    return joins


def _row_date(row: dict) -> Optional[date]:
    raw = str(row.get("date") or "")[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


# ── the funnel ───────────────────────────────────────────────────────────


def build_funnel(rows: Iterable[dict], refs: dict[str, CaseRef],
                 joins: dict[int, dict]) -> list[FunnelRow]:
    """One row per case that has at least one **real** outcome event."""
    funnel: dict[str, FunnelRow] = {}
    seen_events: set[tuple] = set()
    for index, row in enumerate(rows):
        kind, _ = outcome_kind(row)
        if kind not in OUTCOME_KINDS:
            continue
        name = case_name_from_path(str(row.get("path") or ""))
        if not name:
            join = joins.get(index)
            name = join["case"] if join else ""
        if not name:
            continue
        ref = refs.get(name) or parse_case(name, str(row.get("role") or ""))
        if ref is None:
            continue
        entry = funnel.get(name)
        if entry is None:
            entry = FunnelRow(case=name, org=ref.org, role=ref.role, stream=ref.stream)
            funnel[name] = entry
        # The ledger is append-only and repeated scans append the same event
        # again; one event per (case, kind, day) is the honest count.
        when = str(row.get("date") or "")[:10]
        key = (case_name_from_path(str(row.get("path") or "")) or name, kind, when)
        if key in seen_events:
            continue
        seen_events.add(key)
        entry.add(kind, when)
    return [funnel[name] for name in sorted(funnel)]


def gates(rows: list[dict], refs: dict[str, CaseRef], joins: dict[int, dict],
          funnel: list[FunnelRow]) -> dict:
    """Phase A's acceptance numbers (`docs/52` §6).

    Three questions, each with its inputs beside the ratio:

    * **org** — does every row resolve to an organisation, and does the path
      agree with the column where both exist?
    * **classification** — is any board digest counted as an outcome?
    * **attribution** — do the outcome rows that are real reach a case?
    """
    total = len(rows)
    alerts = [r for r in rows if outcome_kind(r)[0] == "job_alert"]
    real = [(i, r) for i, r in enumerate(rows) if outcome_kind(r)[0] in OUTCOME_KINDS]
    # Org coverage is asked of the rows that carry a fact. A quarantined
    # newsletter needs no organisation: it is not an outcome.
    from_path = sum(1 for _, r in real if r.get("org_source") == "path")
    usable = sum(1 for _, r in real if r.get("org_name"))
    # Attribution is counted over *distinct events*, not rows: the append-only
    # ledger carries the same scan's row three times (24 for Longshot Systems).
    events: dict[tuple, bool] = {}
    for i, r in real:
        key = (str(r.get("kind")), str(r.get("org_name")), str(r.get("role")),
               str(r.get("date")))
        events[key] = events.get(key, False) or bool(
            case_name_from_path(str(r.get("path") or "")) or i in joins)
    attributed = [k for k, ok in events.items() if ok]
    unjoined = [k for k, ok in events.items() if not ok]
    interviews = [(i, r) for i, r in real if r.get("kind") in ("interview", "offer")]
    interviews_attributed = [i for i, _ in interviews
                             if case_name_from_path(str(rows[i].get("path") or ""))
                             or i in joins]
    return {
        "rows": total,
        "org_usable": usable,
        "org_rows": len(real),
        "org_usable_share": round(usable / len(real), 4) if real else None,
        "org_repaired_from_path": from_path,
        "org_gate_met": bool(real) and usable == len(real),
        "alert_rows_quarantined": len(alerts),
        "classification_gate_met": bool(total) and len(alerts) + len(real) == total,
        "outcome_rows": len(real),
        "outcome_events": len(events),
        "outcome_events_attributed": len(attributed),
        "attribution_share": (round(len(attributed) / len(events), 4) if events else None),
        "unjoined_events": [list(k) for k in unjoined],
        "interviews_real": len(interviews),
        "interviews_attributed": len(interviews_attributed),
        "interview_attribution_share": (round(len(interviews_attributed) / len(interviews), 4)
                                        if interviews else None),
        "cases_in_funnel": len(funnel),
    }


# ── CLI ──────────────────────────────────────────────────────────────────


def _runtime(principal: str):
    runtime = resolve_principal_runtime(principal)
    apps = Path(config.APPLICATIONS_DIR) if getattr(config, "APPLICATIONS_DIR", None) else None
    return runtime, apps


def report(principal: str) -> dict:
    runtime, apps = _runtime(principal)
    ledger = runtime.dataset_dir / "outcomes.jsonl"
    rows = resync_rows(load_rows(ledger))
    refs = case_index(rows, applications_dir=apps,
                      packs_dir=runtime.state_root / "applications")
    joins = attribute(rows, refs)
    funnel = build_funnel(rows, refs, joins)
    return {
        "principal": principal,
        "ledger": str(ledger),
        "cases_seen": len(refs),
        "joins": joins,
        "funnel": funnel,
        "gates": gates(rows, refs, joins, funnel),
    }


def write_funnel(result: dict, dataset_dir: Path) -> Path:
    path = dataset_dir / "funnel.jsonl"
    lines = [json.dumps(row.as_dict(), sort_keys=True) for row in result["funnel"]]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def _print_summary(result: dict, funnel_path: Optional[Path] = None) -> None:
    g = result["gates"]
    print(f"outcome ledger : {result['ledger']}")
    print(f"rows           : {g['rows']}  (real outcomes {g['outcome_rows']}, "
          f"alerts quarantined {g['alert_rows_quarantined']})")
    print(f"org resolved   : {g['org_usable']}/{g['rows']} "
          f"(= {g['org_usable_share']}); repaired from the path: {g['org_repaired_from_path']}")
    print(f"outcomes joined: {g['outcome_events_attributed']}/{g['outcome_events']} distinct "
          f"events (= {g['attribution_share']}); {g['outcome_rows']} raw rows")
    print(f"interviews     : {g['interviews_real']} real, "
          f"{g['interviews_attributed']} attributed "
          f"(= {g['interview_attribution_share']})")
    print(f"cases seen     : {result['cases_seen']}; in funnel: {g['cases_in_funnel']}")
    methods = Counter(j["method"] for j in result["joins"].values())
    print(f"join methods   : {dict(methods) or '{}'}")
    if funnel_path:
        print(f"funnel         : {funnel_path}")
    print(f"gates          : org {'PASS' if g['org_gate_met'] else 'FAIL'}, "
          f"classification {'PASS' if g['classification_gate_met'] else 'FAIL'}, "
          f"attribution {'PASS' if (g['attribution_share'] or 0) >= 0.99 else 'FAIL'}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", default="lj", help="Principal (default: lj)")
    parser.add_argument("--write", action="store_true",
                        help="Write state/<p>/dataset/funnel.jsonl (derived, rebuildable)")
    parser.add_argument("--json", action="store_true", help="Machine-readable report")
    parser.add_argument("--explain", action="store_true",
                        help="Name the rows that could not be attributed")
    args = parser.parse_args(argv)

    result = report(args.profile)
    g = result["gates"]
    funnel_path = None
    if args.write:
        runtime, _ = _runtime(args.profile)
        funnel_path = write_funnel(result, runtime.dataset_dir)

    if args.json:
        payload = {k: v for k, v in result.items() if k != "funnel"}
        payload["funnel"] = [row.as_dict() for row in result["funnel"]]
        print(json.dumps(payload, indent=1, sort_keys=True))
    else:
        _print_summary(result, funnel_path)
        if args.explain:
            runtime, _ = _runtime(args.profile)
            refs = case_index(resync_rows(load_rows(runtime.dataset_dir / "outcomes.jsonl")),
                              applications_dir=_runtime(args.profile)[1],
                              packs_dir=runtime.state_root / "applications")
            for kind, org, role, when in g["unjoined_events"]:
                same_org = [c for c in refs.values() if c.org_slug == _slug(org)]
                reason = ("no case on this host" if not same_org
                          else "ambiguous: more than one case for that org"
                          if len(same_org) > 1 else "no join key")
                print(f"  unattributed: {kind} {org!r} {role[:40]!r} on {when} — {reason}")

    if not g["rows"]:
        return 3                      # no ledger on this host: not a failure
    ok = (g["org_gate_met"] and g["classification_gate_met"]
          and (g["attribution_share"] or 0) >= 0.99)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
