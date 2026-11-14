#!/usr/bin/env python3
"""Curate the historic crawls into the OptMem memories (docs/40, PLAN.md).

Why this exists: the memory tier was adopted on 2026-10-04 with hooks that note
*forward* (watch deltas, case milestones, keel, letters). Four months of ledger
history — 1,335 lj outcome rows and every case on disk — was therefore never
curated in; each org memory held a single generated `baseline:` line. This
script is the one-off catch-up, in the tier's own voice.

What it writes, per principal:

  * `outcomes.jsonl`   → one `case:` note per milestone into `orgs/<org>`
                         (applied / rejection / interview / offer), each
                         carrying its outcome id and source as provenance.
  * case dirs on disk  → one `case:` note for applications that predate the
                         ledger row (the 2025 case directories).
  * `list_decisions`   → `keel:` notes (promoted / demoted) into those orgs.
  * letters ledger     → `letter:` notes into `agents/jack-and-jill`.
  * `principal/`       → one monthly aggregate line: counts only, honest.

Then, with `--pay-naps`, it pays every pending compression with a generated
one-line summary built **only** from the children's dates, counts and tags —
the tool's own rule ("keep what has lasting effect, invent nothing") applied
mechanically, because the historic volume is far past what session naps can
absorb. Going forward sessions pay naps as PLAN.md §5.2 intends.

Idempotent: a record is skipped when its unique id (outcome id, decision id,
case directory) is already in that memory's log, checked with `recall`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jsonl_io
import config  # noqa: E402
import mem as memmod  # noqa: E402

MEM = Path(__file__).resolve().parent / "mem.py"
#: The tool's own per-record byte budget is authoritative (`mem.fit_one_line`).
CASE_KINDS = ("applied", "application_sent", "interview", "offer", "rejection")


def _norm_entity(entity: str | None) -> str | None:
    """`principal` is the default memory — it takes no entity flag."""
    return None if entity in (None, "principal") else entity


def _run(profile: str, args: list[str], *, entity: str | None = None) -> tuple[int, str]:
    """One `mem` call. `entity` is 'org:<slug>' | 'agent:<name>' | None."""
    cmd = [sys.executable, str(MEM), *args, "--profile", profile]
    entity = _norm_entity(entity)
    if entity:
        kind, _, value = entity.partition(":")
        cmd += [f"--{kind}", value]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and proc.stderr.strip():
        # memo's die() writes to stderr; without this a failure is reasonless
        print(f"    ! mem {args[0]} failed ({proc.returncode}) for "
              f"{entity or 'principal'}: {proc.stderr.strip()[:200]}", file=sys.stderr)
    return proc.returncode, proc.stdout


_ENSURED: set[tuple[str, str]] = set()


def _ensure(profile: str, entity: str | None) -> None:
    """Init the memory once per entity — `memo init` is safe to re-run, and an
    org we applied to is a tracked entity even if the baseline pass (tracked
    orgs only) never created it."""
    key = (profile, entity or "principal")
    if key in _ENSURED:
        return
    _ENSURED.add(key)
    _run(profile, ["init"], entity=entity)


def _seen(profile: str, entity: str | None, token: str) -> bool:
    """Is this record already in that memory? (`recall` on the unique id.)"""
    _ensure(profile, entity)
    code, out = _run(profile, ["recall", re.escape(token)], entity=entity)
    return code == 0 and "No match." not in out


def _note(profile: str, entity: str, line: str, token: str, *, dry: bool) -> bool:
    """Note one line, once. Returns True when it was (or would be) written."""
    line = memmod.fit_one_line(line)
    _ensure(profile, entity)
    if _seen(profile, entity, token):
        return False
    if dry:
        print(f"    [{entity}] {line}")
        return True
    code, out = _run(profile, ["note", line], entity=entity)
    if code != 0:
        print(f"    ! note failed ({code}) for {entity}", file=sys.stderr)
        return False
    return True


def _case_note(row: dict) -> tuple[str, str, str]:
    """(org, line, token) for one outcome row."""
    org = (row.get("org_name") or "").strip() or "unknown"
    role = (row.get("role") or "").strip() or "a role"
    kind = (row.get("kind") or row.get("event_type") or "outcome").strip()
    date = (row.get("date") or "")[:10]
    oid = (row.get("outcome_id") or "")[:12]
    source = row.get("source") or "?"
    line = f"case: {date} {kind} — {role} @ {org} [#{oid} {source}]"
    return org, line, oid


def _outcome_rows(dataset: Path) -> list[dict]:
    path = dataset / "outcomes.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in jsonl_io.lines(path):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _case_dir_org(name: str) -> tuple[str, str, str] | None:
    """(date, org, role-text) for a case directory, or None when it is not a
    record worth importing: no date prefix, or an obvious test fixture."""
    m = re.match(r"(\d{8})[-_](.+)$", name)
    if not m:
        return None
    date, rest = m.group(1), m.group(2)
    org = re.split(r"[-_]", rest, 1)[0]
    if org.lower().startswith("test"):
        return None                        # fixture cases (TestCo…) are not history
    role = rest.replace("_", " ")
    return f"{date[:4]}-{date[4:6]}-{date[6:]}", org, role


def _case_dirs_without_ledger(apps: Path, ledger_paths: set[str],
                              ledger_names: set[str]) -> list[Path]:
    """Case directories that are the principal's own applications (never a
    `-LoveWork` pack) and have no matching outcome row."""
    out = []
    if not apps.is_dir():
        return out
    for d in sorted(apps.iterdir()):
        if not d.is_dir() or d.name.endswith("-LoveWork") or d.name.endswith("-LoveWork-ATA"):
            continue
        if str(d) in ledger_paths or d.name in ledger_names:
            continue
        out.append(d)
    return out


def _principals(profile: str) -> list[str]:
    if profile != "all":
        return [profile]
    return sorted(p.name for p in config.STATE_DIR.iterdir()
                  if p.is_dir() and (p / "dataset").is_dir()
                  and re.fullmatch(r"[a-z][a-z0-9-]*", p.name))


def _monthly_lines(rows: list[dict]) -> list[tuple[str, str]]:
    """(month, line) aggregates over outcome rows — counts only."""
    buckets: dict[str, Counter] = {}
    for row in rows:
        month = (row.get("date") or "")[:7]
        if not month:
            continue
        buckets.setdefault(month, Counter())[row.get("kind") or "outcome"] += 1
    lines = []
    for month in sorted(buckets):
        counts = buckets[month]
        total = sum(counts.values())
        detail = ", ".join(f"{k} {v}" for k, v in counts.most_common())
        lines.append((month, f"month: {month} — {total} outcome record(s): {detail}"))
    return lines


IDENTITY: dict[str, list[str]] = {
    "lj": [
        "identity: User Example — London/the home town UK; UK+EU dual citizenship; remote OK for UK/EU or global-remote; NOT US-only or US-timezone [profiles/example/soul.md]",
        "decision: avoid US-only and US-timezone roles — the matcher's work-auth hard-kill enforces it [profiles/example/work_auth.md]",
        "direction: AI/ML research, agentic systems, applied ML and startups; also the ARC-AGI-2 and SWE-agents tracks [profiles/example/possibilities.md]",
        "standing: quant trading/research paused, exception for an AI/ML bridge [possibilities.md]",
        "history: PhD statistics; ex-a past employer (R&D framework, $100M→$10B+), a past employer, a past employer, a past employer (ASR/lattice search) [cv-short.md]",
        "practice: builds production systems and runs LLM/agentic tooling (DSPy, MCP); prefers bounded engagements over open-ended seats",
        "decision: application packs must precede decisions — agents do advert review, diligence and tailored material; Submit is the principal's alone (2026-10-03)",
        "decision: stale-advert doctrine — 10-day cap, 25-day drop (2026-10-03) [docs/27 §2b]",
        "decision: adopt OptMem as the narrative memory tier, D22 (2026-10-04) [DECISIONS.md]",
    ],
    "vj": [
        "identity: P2 Example — early-career quantitative analyst; 1:1 BSc Economics & Mathematics, MSc Artificial Intelligence (QMUL) [profiles/vj/soul.md]",
        "decision: does NOT want AI/ML/NLP research as a profession — chose insurance/actuarial/pricing/risk deliberately [soul.md]",
        "standing: insurance, actuarial, pricing and risk is the strong-GO cluster; graduate and entry-level roles [soul.md]",
        "work-auth: UK + EU citizenship, no sponsorship needed [work_auth.md]",
        "market: applies through graduate schemes and analyst intakes; deadlines cluster Sep–Nov",
    ],
    "kj": [
        "identity: P3 Example — computational chemist; the home town UK; UK+MK citizenship, no sponsorship needed [profiles/kj/soul.md]",
        "decision: avoid US-only or US-timezone-anchored roles [soul.md]",
        "standing: PhD in progress (UCL, year 2); industry target is computational chemistry / cheminformatics / ML-for-science",
        "market: small UK/EU set of cheminformatics and in-silico modelling employers; watches them by tracker",
    ],
}


def seed_identity(profile: str, *, dry: bool) -> int:
    """The principal's identity lines — only into an *empty* memory.

    The host-b session seeded these on 2026-10-04 (lj 10 records with naps
    paid, vj 6, kj 6). Re-seeding appends near-duplicates of the same facts,
    which is what happened to lj on 2026-10-05; skip when anything is there.
    """
    log = config.STATE_DIR / profile / "dataset" / "memory" / "principal" / "LOG.txt"
    existing = log.read_text(encoding="utf-8", errors="ignore") if log.is_file() else ""
    if existing.strip():
        print(f"   identity: {profile} principal memory is already seeded — skipped")
        return 0
    written = 0
    for line in IDENTITY.get(profile, []):
        # token = a prefix of the stored line, so `recall` finds what is already there
        if _note(profile, "principal", line, line[:60], dry=dry):
            written += 1
    return written


def import_principal(profile: str, dataset: Path, *, dry: bool) -> int:
    rows = _outcome_rows(dataset)
    written = 0
    for month, line in _monthly_lines(rows):
        if _note(profile, "principal", line, line[:60], dry=dry):
            written += 1
    return written


def import_orgs(profile: str, dataset: Path, *, dry: bool) -> tuple[int, int]:
    """Case milestones into org memories; returns (notes, orgs touched)."""
    apps = config.STATE_DIR / profile / "applications"
    rows = _outcome_rows(dataset)
    ledger_paths = {str(r.get("path") or "") for r in rows}
    ledger_names = {Path(str(r.get("path"))).name for r in rows if r.get("path")}
    notes, orgs, skipped = 0, set(), 0
    for row in rows:
        org, line, token = _case_note(row)
        if not memmod.is_org_name(org):
            skipped += 1                     # a role title or board label, not an org
            continue
        if _note(profile, f"org:{org}", line, token, dry=dry):
            notes += 1
            orgs.add(org)
    for d in _case_dirs_without_ledger(apps, ledger_paths, ledger_names):
        parsed = _case_dir_org(d.name)
        if parsed is None:
            continue
        date, org, role = parsed
        line = f"case: {date} applied — {role} @ {org} [case dir, pre-ledger]"
        if _note(profile, f"org:{org}", line, d.name, dry=dry):
            notes += 1
            orgs.add(org)
    return notes, len(orgs)


def import_keel(profile: str, dataset: Path, *, dry: bool) -> int:
    path = dataset / "list_decisions.jsonl"
    if not path.is_file():
        return 0
    written = 0
    for line in jsonl_io.lines(path):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        did = str(row.get("decision_id") or "")[:12]
        for kind, key in (("promoted", "promotions"), ("demoted", "demotions")):
            for item in row.get(key) or []:
                company = item.get("company") or "?"
                detail = item.get("reason") or f"tier {item.get('tier', '?')}"
                note = f"keel: {kind} {company} — {detail} [#{did}]"
                # token = the line's prefix: the id alone is not enough (a company can
                # be promoted in several rows), and the stored text must contain it
                if _note(profile, f"org:{company}", note, note[:60], dry=dry):
                    written += 1
    return written


def import_letters(profile: str, *, dry: bool) -> int:
    written = 0
    for path in sorted((config.STATE_DIR / profile / "letters").glob("*.jsonl")):
        for line in jsonl_io.lines(path):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ref = str(row.get("referencenumber") or "")[:12]
            title = row.get("title") or "?"
            company = row.get("company") or "?"
            when = row.get("lettered_at") or "?"
            note = f"letter: J&J {when} — {title} @ {company} [#{ref}]"
            if _note(profile, "agent:jack-and-jill", note, ref, dry=dry):
                written += 1
    return written


# ------------------------------------------------------------------ naps

def _memory_dirs(profile: str) -> list[tuple[str, Path]]:
    root = config.STATE_DIR / profile / "dataset" / "memory"
    out: list[tuple[str, Path]] = []
    if (root / "principal").is_dir():
        out.append(("principal", root / "principal"))
    for kind in ("orgs", "people", "agents"):
        parent = root / kind
        if parent.is_dir():
            for d in sorted(parent.iterdir()):
                if d.is_dir():
                    out.append((f"{kind[:-1]}:{d.name}", d))
    return out


def summarise(children: list[str]) -> str:
    """One line built only from the children's dates, counts and tags.

    No invention: months, record counts, the tag vocabulary, and what the
    children already said about "last". Deliberately coarse — that is the
    level's job; the log keeps the detail for `zoom`.
    """
    dates = [m.group(1) for c in children if (m := re.search(r"\b(20\d\d-\d\d(?:-\d\d)?)\b", c))]
    tags = Counter(m.group(1) for c in children
                   for m in re.finditer(r"\b(watch|case|keel|letter|source|baseline|month|react|note):", c))
    parts = []
    if dates:
        span = min(dates) if min(dates) == max(dates) else f"{min(dates)}–{max(dates)}"
        parts.append(span)
    parts.append(f"{len(children)} record(s)")
    if tags:
        parts.append(", ".join(f"{t} {n}" for t, n in tags.most_common(3)))
    text = "; ".join(parts)
    return memmod.fit_one_line(text)


def pay_naps(profile: str, memdir: Path, *, dry: bool, max_naps: int = 4000) -> int:
    """Pay every pending compression for one memory, oldest block first."""
    env_label = memdir.parent.name + "/" + memdir.name
    paid = 0
    for _ in range(max_naps):
        code, out = _nap_probe(memdir)
        if "Nothing left to compress." in out or "Compress memories #" not in out:
            break
        block = re.search(r"Compress memories #(\d+)-(\d+)", out)
        body = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("#")]
        if not block or not body:
            print(f"    ! unparsable nap prompt for {env_label}", file=sys.stderr)
            break
        lo, hi = block.group(1), block.group(2)
        line = summarise(body)
        if dry:
            print(f"    [{env_label}] #{lo}-{hi} → {line}")
            paid += 1
            continue
        code, out2 = _nap_apply(memdir, lo, hi, line)
        if code != 0:
            print(f"    ! nap {lo}-{hi} failed for {env_label}: {out2.strip()[:120]}",
                  file=sys.stderr)
            break
        paid += 1
    return paid


def _nap_probe(memdir: Path) -> tuple[int, str]:
    env = dict(__import__("os").environ, MEMORY_DIR=str(memdir))
    proc = subprocess.run([sys.executable, str(memmod.MEMO), "nap"], env=env,
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout


def _nap_apply(memdir: Path, lo: str, hi: str, line: str) -> tuple[int, str]:
    import os
    env = dict(os.environ, MEMORY_DIR=str(memdir))
    proc = subprocess.run([sys.executable, str(memmod.MEMO), "nap", f"{lo}-{hi}", line],
                          env=env, capture_output=True, text=True)
    return proc.returncode, proc.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Curate historic crawls into OptMem")
    ap.add_argument("--profile", default="all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pay-naps", action="store_true",
                    help="pay pending compressions with generated summaries")
    ap.add_argument("--naps-only", action="store_true",
                    help="skip the import, only pay naps")
    args = ap.parse_args(argv)

    total = 0
    for profile in _principals(args.profile):
        dataset = config.STATE_DIR / profile / "dataset"
        print(f"== {profile}")
        if not args.naps_only:
            n_identity = seed_identity(profile, dry=args.dry_run)
            n_principal = import_principal(profile, dataset, dry=args.dry_run)
            n_orgs, orgs = import_orgs(profile, dataset, dry=args.dry_run)
            n_keel = import_keel(profile, dataset, dry=args.dry_run)
            n_letters = import_letters(profile, dry=args.dry_run)
            total += n_identity + n_principal + n_orgs + n_keel + n_letters
            print(f"   notes: identity {n_identity}, principal-months {n_principal}, "
                  f"orgs {n_orgs} ({orgs} orgs), keel {n_keel}, letters {n_letters}")
        if args.pay_naps:
            paid_total = 0
            for label, memdir in _memory_dirs(profile):
                paid = pay_naps(profile, memdir, dry=args.dry_run)
                if paid:
                    print(f"   naps {label}: {paid}")
                    paid_total += paid
            print(f"   naps paid: {paid_total}")
    print(f"done: {total} note(s)" + (" (dry run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
