#!/usr/bin/env python3
"""AS1 — the bounded front door (docs/34 §AS1).

One versioned situation projection, rendered as the root ``STATE.md`` and
served to any consumer with a per-consumer cursor. Built **on AS0
selections** (``selection.py``) — this module never re-derives what
"latest result" means; it renders what AS0 answered, for every principal,
plus the due clocks and the continuation record.

The four AS1 contracts implemented here:

1. **Versioned situation projection** — ``situation()`` builds a dict:
   per-principal AS0 answers (result / world / snapshot / execution) with
   evidence refs, the due projection, the continuation record's presence,
   a ``watermark`` (the digests of everything it read), ``generated_at``,
   and ``oldest_observation`` (how stale the freshest input is). Raw
   sensitive payload (mail bodies, profile text, listing rows) never
   enters the projection — ids, statuses, reasons and counts only.
2. **Rebuildable index with a priced rebuild** — the projection is cached
   at ``state/idx/situation.json`` keyed by an input digest. A read is
   pure: cache fresh → serve; cache stale/missing → serve a **bounded
   partial** (recompute from AS0, which is cheap and local) plus a
   ``rebuild`` field naming what a full refresh costs. Never a hidden
   scan: the response always says whether it served cache or computed.
3. **Root STATE.md render** — ``render_state_md()``: due first (earliest
   clocks), then the per-principal table, then one bounded section per
   principal, then not-answerable-here and pointers. ≤1,200 words by
   construction (counts, not listings; ≤5 decision-changing items;
   paging via ``--page``/``--section``).
4. **Per-consumer cursor** — ``read(consumer, since=cursor)`` returns the
   delta since that consumer's acknowledged cursor: which principals'
   answers changed, which clocks fired. ``acknowledge(consumer)`` advances
   it. A lost/invalid cursor names ``baseline_invalidation`` and a
   rebaseline path — never "no changes".

Plus the **continuation record**: ``CONTINUE.md`` beside the owning plan
(docs/34's sixth box — objective, hypothesis, pending, next step, stop
predicate), read into the projection's header so a cold agent sees the
handoff before anything else.

Usage::

    ../venv/bin/python3 situation.py --render            # write root STATE.md
    ../venv/bin/python3 situation.py --json              # the projection
    ../venv/bin/python3 situation.py --read --consumer glm --since <cursor>
    ../venv/bin/python3 situation.py --ack --consumer glm
    ../venv/bin/python3 situation.py --benchmark         # the 1×/10× gate
"""

from __future__ import annotations
import jsonl_io

import argparse
import json
import logging
import os
import re
import socket
import sys
import time
from dataclasses import asdict
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from selection import (select_execution, select_latest_result,  # noqa: E402
                       select_state_snapshot, select_world_observation)

logger = logging.getLogger("situation")

SCHEMA_VERSION = 1
PROJECTION_VERSION = 5  # v5: the lock is the liveness marker (a record can be days old
# while a run is live). v4: the template profile excluded from the table; the live-run
# cell shows the run record's own status (AS0 reconciles a stale "running"), ordered by
# the record's name because mtime lies for copied state. v3: standing (seal count + the
# newest run record, sealed or not), usable dates, the fourth principal named, hidden
# clocks named, authority age. v2: host + age in the header, due clocks ranked before the
# cap (a dated deadline could be evicted by earlier undated rows), sealed-only note.
MAX_WORDS = 1200
MAX_ITEMS = 5
PAGE_SECTIONS = ("due", "principals", "detail", "continuation")

CONTINUATION_NAME = "CONTINUE.md"
DUE_NAME = "DUE.md"
CURSOR_DIR = "state/idx/cursors"
PROJECTION_CACHE = "state/idx/situation.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _words(text: str) -> int:
    return len(text.split())


# ── due projection: manual commitments + derived clocks ─────────────────


_CLOCK_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _clock_date(when: str) -> date | None:
    """The date a clock's `when` cell carries, if any.

    DUE.md mixes dated deadlines ("2026-10-15") with standing commitments ("Mon
    weekly", "when 3 briefs rated"), and only the dated ones can be evicted safely.
    """
    match = _CLOCK_DATE_RE.search(when or "")
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _clock_key(what: str) -> str:
    """Fold a clock's text far enough that a derived restatement matches its row."""
    return re.sub(r"[^a-z0-9]+", "", (what or "").casefold())[:12]


def _keys_overlap(a: str, b: str) -> bool:
    """True when two folded clock keys name the same commitment."""
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def _describe_age(generated_at: str) -> str:
    """How old the rendered door is.

    The AS1 gate demo (2026-10-01) found the door 5 h stale and printed only its
    timestamp, so a cold agent had to work out the age itself.
    """
    try:
        then = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    minutes = int((datetime.now(tz=then.tzinfo) - then).total_seconds() // 60)
    if minutes < 90:
        return f"{max(minutes, 0)} min"
    if minutes < 36 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} d"


def _standing(state_root: Path, answers: dict) -> dict:
    """Seal count + the newest run: the "where does this principal stand" line a cold
    agent needs. The run stream is the append-only ledger (`dataset/runs.jsonl`),
    written at run start by every path including a manual `main.py`; the per-run
    `cache/runs/*.json` records are older and can be months stale (they were, on
    2026-10-01). A held lock outranks both — it is the only thing that says "now".
    """
    strata = state_root / "strata"
    bundles = ([b for b in strata.iterdir() if (b / "manifest.json").is_file()]
               if strata.is_dir() else [])
    live = None
    ledger = state_root / "dataset" / "runs.jsonl"
    if ledger.is_file():
        lines = [l for l in jsonl_io.lines(ledger) if l.strip()]
        if lines:
            try:
                record = json.loads(lines[-1])
            except ValueError:
                record = {}
            started = str(record.get("started_at") or "?")
            # A run has sealed iff a bundle carries its start stamp YYYYMMDD-HHMM.
            probe = re.sub(r"[^0-9]", "", started)[:12]
            sealed = any(probe and probe in re.sub(r"[^0-9]", "", b.name)
                         for b in bundles)
            live = {"run_id": str(record.get("run_id") or "?")[:16],
                    "mode": record.get("mode") or "?",
                    "started_at": started,
                    "state": "sealed" if sealed else "unsealed"}
    lock = state_root / "cache" / "crawl.lock"
    if lock.is_file():
        fields = dict(line.split("=", 1)
                      for line in lock.read_text(encoding="utf-8").splitlines()
                      if "=" in line)
        if fields.get("status") == "running":
            live = {"run_id": (fields.get("pid") or "?"),
                    "mode": fields.get("type") or "?",
                    "started_at": fields.get("start") or "?",
                    "state": "running (lock held)"}
    return {"seals": len(bundles),
            "newest_result": _event_date(answers.get("result")) or None,
            "live": live}


def _principal_profiles(repo_root: Path) -> list[str]:
    """Principals the repo knows (profiles/<p>/soul.md) even when state/<p>/ does
    not exist yet — the door must name them rather than silently omitting them.
    `profiles/example/` is the anonymised template, not a principal.
    """
    profiles = repo_root / "profiles"
    if not profiles.is_dir():
        return []
    # **LJ's priority order**, not the alphabet (2026-10-19: *"New priority order
    # 1-vj 2-kj 3-lj"*). The loop's round-robin drains principals in the order this
    # returns, so an alphabetical sort silently made it kj, lj, vj — the reverse of
    # the ruling, for as long as nobody read the list. `config.PRINCIPAL_ORDER` is
    # the one authority; this asks it.
    import config

    return sorted((p.name for p in profiles.iterdir()
                   if p.is_dir() and p.name != "example"
                   and (p / "soul.md").is_file()),
                  key=config.principal_sort_key)


def due_projection(repo_root: Path, *, today: date | None = None) -> dict:
    """DUE.md's manual table (the authority) + scheduler-derived clocks,
    each with provenance. No deadline is trusted from old prose: only the
    table rows and the derived sources named here."""
    today = today or date.today()
    manual, parse_note = [], ""
    due_path = repo_root / DUE_NAME
    if due_path.is_file():
        text = due_path.read_text(encoding="utf-8")
        if "## Open clocks" in text:
            section = text.split("## Open clocks", 1)[1].split("##", 1)[0]
            for line in section.splitlines():
                if not line.strip().startswith("|"):
                    continue
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) < 2 or cells[0] in ("When",) \
                        or set(cells[0]) <= set("-: "):
                    continue
                # The table is `When | What | Consequence | Next action | Owner`.
                # The last three are kept, not dropped: the *owner* is the
                # declared authority for the clock, and reading it beats
                # guessing from the text (decide.py used to guess). A `|` inside
                # a cell would split it, so the tail rejoins into Owner.
                if len(cells) > 5:
                    cells = cells[:4] + ["|".join(cells[4:])]
                cells += [""] * (5 - len(cells))
                manual.append({"when": cells[0], "what": cells[1],
                               "consequence": cells[2],
                               "next_action": cells[3], "owner": cells[4],
                               "provenance": "DUE.md (manual commitment)"})
        else:
            parse_note = "DUE.md has no '## Open clocks' table"
    else:
        parse_note = "DUE.md absent"

    derived = [
        {"when": "Mon weekly", "what": "J&J letter (engine gate)",
         "provenance": "jnj_letter.py MIN_INTERVAL_DAYS"},
        {"when": "per crawl schedule", "what": "LJ seal → KJ consume unblocks",
         "provenance": "lovework-kj-consume cron + consume deferral"},
    ]
    # No cap here: the projection carries every open clock it parsed and the
    # renderer decides what fits. Capping at the source evicted real deadlines
    # silently — DUE.md lists undated commitments first, so a cap drops the dated
    # clocks last-in-first-out (2026-10-15 went missing this way; the AS1 gate demo
    # caught it, 2026-10-01).
    edited = None
    if due_path.is_file():
        edited = _describe_age(datetime.fromtimestamp(
            due_path.stat().st_mtime, tz=timezone.utc).isoformat())
    return {"manual": manual, "derived": derived, "edited": edited,
            "note": parse_note, "count": len(manual)}


# ── continuation record ──────────────────────────────────────────────────


def read_continuation(repo_root: Path) -> dict:
    """CONTINUE.md beside the owning plan: the warm-handoff record. Parsed
    as key: value front-matter lines + free text; absence is honest."""
    path = repo_root / CONTINUATION_NAME
    if not path.is_file():
        return {"present": False,
                "note": "no CONTINUE.md — no work was left mid-flight"}
    text = path.read_text(encoding="utf-8")
    fields: dict[str, str] = {}
    body_lines = []
    for line in text.splitlines():
        candidate = line.split(": ", 1)[0].strip() if ": " in line else ""
        # keys are lowercase words ("objective", "next step",
        # "stop predicate") — multi-word allowed, markdown excluded
        if ": " in line and re.fullmatch(r"[a-z][a-z ]*", candidate):
            fields[candidate] = line.split(": ", 1)[1].strip()[:200]
        else:
            body_lines.append(line)
    return {"present": True, "fields": fields,
            "excerpt": " ".join(" ".join(body_lines).split()[:60])}


# ── the situation projection ────────────────────────────────────────────


def situation(repo_root: Path, *, principals: list[str] | None = None,
              compute: bool = False) -> dict:
    """The versioned projection. Reads the cache when fresh unless
    ``compute``; always records how it was served."""
    import config

    state_dir = Path(config.STATE_DIR)
    cache_path = repo_root / PROJECTION_CACHE
    inputs_digest = _inputs_digest(repo_root, state_dir, principals)

    if not compute:
        cached = _read_cache(cache_path)
        if cached and cached.get("inputs_digest") == inputs_digest:
            cached["served_from"] = "cache"
            return cached

    principals = principals or _authorized_principals(state_dir)
    per_principal = {}
    oldest = None
    standing: dict = {}
    for principal in principals:
        state_root = state_dir / principal
        answers = {}
        for name, fn in (("result", select_latest_result),
                         ("world", select_world_observation),
                         ("snapshot", select_state_snapshot),
                         ("execution", select_execution)):
            sel = fn(state_root)
            answers[name] = {"status": sel.status, "at": sel.at,
                             "id": sel.selected_id, "reason": sel.reason,
                             "qualification": _slim_qual(sel)}
            if sel.ok and sel.at < (oldest or "9999"):
                oldest = sel.at
        per_principal[principal] = answers
        standing[principal] = _standing(state_root, answers)

    due = due_projection(repo_root)
    continuation = read_continuation(repo_root)
    projection = {
        "schema_version": SCHEMA_VERSION,
        "projection_version": PROJECTION_VERSION,
        "generated_at": _utc_now_iso(),
        "served_from": "computed",
        "inputs_digest": inputs_digest,
        "watermark": inputs_digest,          # same digest: everything read
        "oldest_observation": oldest,
        "principals": per_principal,
        "standing": standing,
        "principal_profiles": _principal_profiles(repo_root),
        "due": due,
        "continuation": continuation,
        "bounds": {"max_words": MAX_WORDS, "max_items": MAX_ITEMS,
                   "sections": PAGE_SECTIONS},
    }
    _write_cache(cache_path, projection)
    return projection


def _slim_qual(sel) -> dict:
    q = sel.qualification or {}
    return {
        "state_snapshot_claim":
            (q.get("capability_claims") or {}).get("state_snapshot"),
        "assets_ok": all(v for k, v in (q.get("assets") or {}).items()
                         if isinstance(v, bool)),
        "inputs_pinned":
            (q.get("dependency_closure") or {}).get("inputs_pinned"),
    }


def _authorized_principals(state_dir: Path) -> list[str]:
    import re
    return sorted(p.name for p in state_dir.iterdir()
                  if p.is_dir() and (p / "dataset").is_dir()
                  and re.fullmatch(r"[a-z][a-z0-9-]*", p.name))


def _inputs_digest(repo_root: Path, state_dir: Path,
                   principals: list[str] | None) -> str:
    """Digest over the authorities the projection reads: manifests
    (newest mtime per principal bundle dir), DUE.md, CONTINUE.md — plus the
    projection version, so a change to the generator itself cannot keep serving
    a stale door (it did, 2026-10-01: the cache is keyed on inputs only)."""
    h = sha256()
    h.update(f"projection:v{PROJECTION_VERSION}".encode())
    # The generator's own source, so a code change can never be masked by the cache
    # (it was, twice, before this: the cache keyed on inputs only).
    h.update(Path(__file__).read_bytes())
    for name in ("DUE.md", CONTINUATION_NAME):
        p = repo_root / name
        h.update(name.encode())
        if p.is_file():
            h.update(str(p.stat().st_mtime_ns).encode())
    for principal in (principals or _authorized_principals(state_dir)):
        h.update(principal.encode())
        for rel in ("strata", "dataset/runs.jsonl", "cache"):
            d = state_dir / principal / rel
            if d.is_dir():
                newest = max((p.stat().st_mtime_ns for p in d.iterdir()),
                             default=0)
            elif d.is_file():
                newest = d.stat().st_mtime_ns
            else:
                continue
            h.update(str(newest).encode())
    return h.hexdigest()[:16]


def _read_cache(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(path: Path, projection: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(projection, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


# ── per-consumer cursor ──────────────────────────────────────────────────


def read_delta(repo_root: Path, consumer: str, since: str | None) -> dict:
    """What changed since this consumer's cursor. An invalid/missing cursor
    is baseline_invalidation with a rebaseline path — never 'no changes'."""
    import config

    current = situation(repo_root)
    cursor_path = (repo_root / CURSOR_DIR / f"{consumer}.json")
    acknowledged = None
    if cursor_path.is_file():
        try:
            acknowledged = json.loads(
                cursor_path.read_text(encoding="utf-8")).get("watermark")
        except (json.JSONDecodeError, OSError):
            acknowledged = None
    baseline = since or acknowledged
    if baseline is None:
        return {"consumer": consumer,
                "status": "baseline_invalidation",
                "reason": "no cursor supplied or recorded — this is your "
                          "first read or the cursor was lost",
                "rebaseline": "acknowledge the current watermark, then "
                              "future reads return deltas",
                "watermark": current["watermark"]}
    if baseline == current["watermark"]:
        return {"consumer": consumer, "status": "ok",
                "changes": [], "watermark": current["watermark"],
                "note": "nothing changed since this cursor"}
    return {"consumer": consumer, "status": "ok",
            "changes": _diff_against(repo_root, baseline),
            "watermark": current["watermark"],
            "note": "changes since the cursor; acknowledge to advance"}


def acknowledge(repo_root: Path, consumer: str) -> dict:
    current = situation(repo_root)
    path = repo_root / CURSOR_DIR / consumer + ".json" \
        if False else repo_root / CURSOR_DIR / f"{consumer}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "consumer": consumer, "watermark": current["watermark"],
        "acknowledged_at": _utc_now_iso()}, sort_keys=True) + "\n",
        encoding="utf-8")
    os.replace(tmp, path)
    return {"consumer": consumer, "acknowledged": current["watermark"]}


def _diff_against(repo_root: Path, baseline: str) -> list[dict]:
    """Bounded change list: principals whose selection ids differ from the
    baseline-cached projection, plus fired clocks. The cache keeps the
    baseline projection under its own digest so the diff is a comparison,
    not a re-derivation."""
    archive = repo_root / PROJECTION_CACHE.replace(
        "situation.json", "situation-history")
    archive.mkdir(parents=True, exist_ok=True)
    old_path = archive / f"{baseline}.json"
    if not old_path.is_file():
        return [{"kind": "baseline_unavailable",
                 "reason": f"baseline {baseline} not archived — treat all "
                           "current facts as fresh"}]
    old = json.loads(old_path.read_text(encoding="utf-8"))
    current = situation(repo_root)
    changes = []
    for principal, answers in current["principals"].items():
        old_answers = (old.get("principals") or {}).get(principal) or {}
        for question, answer in answers.items():
            old_id = (old_answers.get(question) or {}).get("id")
            if answer["id"] != old_id:
                changes.append({"kind": "selection_changed",
                                "principal": principal, "question": question,
                                "from": old_id, "to": answer["id"]})
    return changes[:MAX_ITEMS * 4]


# ── the root STATE.md render ─────────────────────────────────────────────


def render_state_md(projection: dict, *, section: str = "all") -> str:
    """The front door, ≤1,200 words by construction, due first."""
    generated = projection["generated_at"]
    lines = [f"# LoveWork — operational state",
             "",
             f"_Front door for this repo on **{socket.gethostname()}** — the bounded "
             "operational projection. Read this file and `DUE.md` and you are "
             "oriented. `AGENTS.md` is the project map, `MANUAL.md` the operator's "
             "manual (LJ-facing), `README.md` the public face, "
             "`docs/03-profiles.md` who the principals are — "
             "none of them is the operational door._",
             "",
             f"_Generated {generated} by situation.py "
             f"(projection v{PROJECTION_VERSION}; served from "
             f"{projection['served_from']}; oldest input "
             f"{projection.get('oldest_observation') or '—'}). Evidence "
             f"pointers inline; raw payloads never enter this file._",
             "",
             f"_Age: {_describe_age(generated)} — nothing re-renders this file "
             "automatically; `situation.py --render` refreshes it, and a stale door "
             "should be re-rendered before it is trusted._",
             ""]
    due = projection.get("due") or {}
    lines += [f"## Due (dated first, earliest first) — `DUE.md` edited "
              f"{due.get('edited') or '?'} ago", ""]
    # Rank before capping, or the cap silently eats deadlines: DUE.md lists undated
    # commitments first, so a plain [:N] drops the *dated* clocks at the end — a real
    # deadline (2026-10-15, decide pr_0001) disappeared that way until the AS1 gate
    # demo caught it (2026-10-01). Dated clocks now sort first and can only be evicted
    # by other dated clocks, and anything evicted is reported rather than dropped.
    manual_clocks = due.get("manual") or []
    dated = sorted((c for c in manual_clocks if _clock_date(c.get("when", ""))),
                   key=lambda c: _clock_date(c["when"]) or date.max)
    undated = [c for c in manual_clocks if not _clock_date(c.get("when", ""))]
    merged = dated + undated
    rows = merged[:MAX_ITEMS]
    omitted = len(merged) - len(rows)
    # A derived clock that restates a manual one is not a second deadline.
    manual_keys = [_clock_key(c.get("what", "")) for c in manual_clocks]
    derived_rows = [c for c in (due.get("derived") or [])
                    if not any(_keys_overlap(_clock_key(c.get("what", "")), k)
                               for k in manual_keys)][:3]
    for clock in rows:
        lines.append(f"- **{clock['when']}** — {clock['what']} "
                     f"_({clock['provenance']})_")
    for clock in derived_rows:
        lines.append(f"- {clock['when']} — {clock['what']} "
                     f"_(derived: {clock['provenance']})_")
    if omitted:
        hidden = " · ".join(c.get("when", "?") for c in merged[len(rows):])
        lines.append(f"- ⚠ {omitted} further open clock(s) — also open: {hidden} "
                     "(full rows in `DUE.md`)")
    if due.get("note"):
        lines.append(f"- ⚠ {due['note']}")
    if not rows and not due.get("derived"):
        lines.append("_No open clocks._")
    lines += ["", "## Principals", "",
              "| P | latest result | world obs | live run | bundles |",
              "|---|---|---|---|---|"]
    standing = projection.get("standing") or {}
    known = sorted(set(projection["principals"])
                   | set(projection.get("principal_profiles") or []))
    for principal in known:
        answers = (projection["principals"] or {}).get(principal)
        if not answers:
            lines.append(f"| {principal} | — | — | — | no `state/{principal}/` "
                         "directory (known principal, not yet initialised) |")
            continue
        st = standing.get(principal) or {}
        live = st.get("live") or {}
        cells = [_dated(answers.get("result")), _dated(answers.get("world")),
                 (f"{live.get('mode')} run started "
                  f"{str(live.get('started_at'))[:16]} · {live.get('state')}"
                  if live else "—"),
                 _seals_cell(st)]
        lines.append(f"| {principal} | " + " | ".join(cells) + " |")
    lines += ["",
              "_`latest result` is the newest **sealed** bundle — a result, not merely "
              "activity. The `live run` column is the newest run in the append-only "
              "ledger (`dataset/runs.jsonl`) whatever its outcome, so unsealed and "
              "in-flight work is visible here; `bundles` counts every bundle dir in "
              "`state/<p>/strata/` — deliberately broader than the per-principal "
              "STATE.md's *result* seals, because it includes the operation children "
              "(email receipts, link rechecks) that seal after the run they report on. "
              "The two differ by exactly that many children._"]
    cont = projection.get("continuation") or {}
    lines += ["", "## Continuation (warm handoff)", ""]
    if cont.get("present"):
        for key, value in (cont.get("fields") or {}).items():
            lines.append(f"- **{key}**: {value}")
        if cont.get("excerpt"):
            lines.append(f"  _{cont['excerpt']}…_")
    else:
        lines.append(f"_{cont.get('note', 'none')} — "
                     "sessions end by updating the plan-of-record_")
    lines += ["", "## Not answerable here", "",
              "- event-level knowledge between seals (WP3 capture)",
              "- qualified liveness (registry transitions are legacy labels)",
              "- counterfactual replay (WP5)", ""]
    lines += ["## Pointers", "",
              "- per-principal STATE.md: `state/<p>/STATE.md`",
              "- narrative memory (OptMem, D22): `python3 mem.py wake --profile <p>` — wake beside this file",
              "- who the principals are: `docs/03-profiles.md`",
              "- the work plan and its acceptance gates: `docs/34-agent-system-plan.md`",
              "- this file's generator: `situation.py --render`",
              f"- watermark: `{projection['watermark']}`", ""]
    text = "\n".join(lines)
    if _words(text) > MAX_WORDS:               # by construction, but pinned
        text = "\n".join(text.splitlines()[:60]) + "\n…(truncated)\n"
    return text


def _seals_cell(st: dict) -> str:
    """The seal count, with the newest result's date beside it when there is one."""
    if not st.get("seals"):
        return "—"
    newest = str(st.get("newest_result") or "")[:10]
    return f"{st['seals']}" + (f" · newest {newest}" if newest else "")


_BUNDLE_STAMP_RE = re.compile(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})")


def _event_date(answer: dict | None) -> str:
    """A selected bundle's own date, from its id (`full-lj-20260926-100049-…`).

    AS0's `at` is when the selection was *made*, not when the event happened — a door
    that printed `at` would date every row "today" (it did, on 2026-10-01, and the
    closing gate caught it).
    """
    match = _BUNDLE_STAMP_RE.search(str((answer or {}).get("id") or ""))
    if not match:
        return ""
    year, month, day, hour, minute = match.groups()
    return f"{year}-{month}-{day} {hour}:{minute}"


def _dated(answer: dict | None) -> str:
    """"ok · 2026-09-26 10:00" — the event's date where its id carries one, else the
    status alone. Never a truncated bundle id."""
    answer = answer or {}
    stamp = _event_date(answer)
    return f"{answer.get('status', '?')}{(' ' + stamp) if stamp else ''}"


def _render_root(repo_root: Path) -> Path:
    projection = situation(repo_root)
    text = render_state_md(projection)
    target = repo_root / "STATE.md"
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    # archive for cursor diffs
    archive = repo_root / PROJECTION_CACHE.replace(
        "situation.json", "situation-history")
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f"{projection['watermark']}.json").write_text(
        json.dumps(projection, sort_keys=True) + "\n", encoding="utf-8")
    return target


# ── the 1×/10× benchmark (the AS1 gate measurement) ────────────────────


def benchmark(repo_root: Path) -> dict:
    """Bytes/files scanned, latency, cache-hit vs computed — at current
    history size (1×). The 10× claim is stated as a projection-cost model,
    not a measurement that was taken."""
    t0 = time.monotonic()
    import config
    state_dir = Path(config.STATE_DIR)
    files_scanned = sum(
        1 for p in state_dir.rglob("manifest.json"))
    projection = situation(repo_root, compute=True)
    computed_s = time.monotonic() - t0
    t1 = time.monotonic()
    cached = situation(repo_root, compute=False)
    cache_s = time.monotonic() - t1
    return {
        "manifests_scanned": files_scanned,
        "computed_seconds": round(computed_s, 3),
        "cache_seconds": round(cache_s, 3),
        "cache_fresh": cached.get("served_from") == "cache",
        "output_words": _words(render_state_md(projection)),
        "bounded": _words(render_state_md(projection)) <= MAX_WORDS,
        "note": "computed cost is linear in manifests (not history rows); "
                "10× manifests ⇒ ~10× compute, cache hit stays O(1) while "
                "the inputs digest is unchanged — stated as a model, the "
                "1× numbers are measured",
    }


# ── CLI ──────────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AS1 — the front door")
    parser.add_argument("--render", action="store_true",
                        help="write the root STATE.md")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--read", action="store_true")
    parser.add_argument("--ack", action="store_true")
    parser.add_argument("--consumer", default=None)
    parser.add_argument("--since")
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--section", default="all", choices=PAGE_SECTIONS + ("all",))
    args = parser.parse_args(argv)

    if args.benchmark:
        print(json.dumps(benchmark(Path.cwd().parent), indent=2,
                         sort_keys=True))
        return 0
    if not args.consumer and (args.read or args.ack):
        print("--consumer required for --read/--ack", file=sys.stderr)
        return 64

    repo_root = Path(__file__).resolve().parents[1]
    if args.ack:
        print(json.dumps(acknowledge(repo_root, args.consumer),
                         indent=2, sort_keys=True))
        return 0
    if args.read:
        print(json.dumps(read_delta(repo_root, args.consumer, args.since),
                         indent=2, sort_keys=True))
        return 0
    if args.render:
        target = _render_root(repo_root)
        print(f"rendered: {target}")
        return 0
    projection = situation(repo_root)
    if args.json:
        print(json.dumps(projection, indent=2, sort_keys=True))
    else:
        print(render_state_md(projection, section=args.section))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
