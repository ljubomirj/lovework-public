#!/usr/bin/env python3
"""The Watch W1 — the watchtower (docs/35).

**The phrase "the Shoulder" is retired** (LJ, 2026-10-15). What it meant, in plain
words: *an agent that checks ten things about the principal's search every hour and
speaks only when something changed.* "Sitting on the principal's shoulder" does not
say what that entails, and a once-a-day brief contradicts it. The module keeps its
name — internal names are not the phrase a reader meets — and `brief.py` now runs
hourly beside this watch, writing nothing on a quiet hour.

A deterministic, idempotent, bounded watch over one principal's authorities:
DUE clocks, registry staleness floors, run records, seals, outcomes and the
Jack & Jill mail archive. Every observation becomes one append-only row in
``state/<p>/dataset/events.jsonl``; a heartbeat row records the counts,
including the zeros and the absent sources (negative space is data).

Design rules (docs/27 v3, binding):

* **No LLM. No network.** The watch reads fields, never executes content —
  crawled data is data, not instructions (the 2026-09-07 learning).
* **Events fire on crossing, not on state.** A row that crossed a staleness
  floor since the last watch is an event; a row that has been stale for
  weeks is history. First run uses a bounded lookback instead of flooding.
* **Idempotent and cursor-safe.** The cursor advances only after the ledger
  append; a dry run computes and prints without writing; a re-run over
  unchanged state emits only the heartbeat.
* **Bounded.** Each event kind is capped per watch (default 50); the
  remainder is counted, not listed.
* **Lease-guarded.** The watch writes principal state (events ledger +
  cursor), so it takes the writer lease like every direct writer — during
  an 8-hour crawl it defers (exit 75) and the cursor catches up after.

Usage::

    ../venv/bin/python3 watch.py --profile lj [--dry-run] [--horizon-days 14] [--json]
    ../venv/bin/python3 watch.py --profile all --json
"""

from __future__ import annotations
import jsonl_io

import argparse
import csv
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = logging.getLogger("watch")

SCHEMA_VERSION = 1
EVENTS_NAME = "events.jsonl"
CURSOR_NAME = "shoulder-cursor.json"

# Aligned with matcher.py's live policy (pinned by test_aligns_with_matcher_policy).
STALE_FLAG_DAYS = int(os.getenv("LOVEWORK_STALE_FLAG_DAYS", "10"))
STALE_DROP_DAYS = int(os.getenv("LOVEWORK_STALE_DROP_DAYS", "25"))
APPLICATION_QUIET_DAYS = int(os.getenv("LOVEWORK_SHOULDER_QUIET_DAYS", "14"))
# 2026-10-03 decay policy: 30d+ open applications are closed by
# snapshot.append_decay_expirations (P halves monthly from day 30).
DECAY_CLOSE_DAYS = int(os.getenv("LOVEWORK_SHOULDER_DECAY_CLOSE_DAYS", "30"))
MAX_EVENTS_PER_KIND = int(os.getenv("LOVEWORK_SHOULDER_MAX_EVENTS", "50"))
FIRST_RUN_LOOKBACK_DAYS = int(os.getenv("LOVEWORK_SHOULDER_LOOKBACK", "7"))
DEFAULT_HORIZON_DAYS = 14

_DUE_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:\s*[→-]\s*(?:(\d{4})-)?(\d{2}-\d{2}))?")
_REGISTRY_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.is_file():
        return rows
    for line in jsonl_io.lines(path):
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


#: True for the duration of a dry watch pass. `--dry-run` says "compute and
#: print; write nothing", and the per-watch cursors are writes too: a dry pass
#: that consumed a crossing left the NEXT real pass silent about it (found
#: 2026-10-08, after a dry run ate a 60-problem link re-check). One flag, one
#: writer, so every watch — present and future — honours it.
_DRY_RUN = False


def _atomic_json(path: Path, payload: dict) -> None:
    if _DRY_RUN:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class WatchResult:
    principal: str
    at: str
    from_cursor: str | None
    dry_run: bool
    events: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    capped: dict[str, int] = field(default_factory=dict)   # remainder per kind

    def summary(self) -> str:
        bits = [f"{k}={v}" for k, v in sorted(self.counts.items())]
        caps = [f"{k}+{v} more" for k, v in sorted(self.capped.items()) if v]
        head = (f"{self.principal} @ {self.at} "
                f"(cursor from {self.from_cursor or 'first-run'})"
                f"{' DRY-RUN' if self.dry_run else ''}")
        return head + ("\n  " + ", ".join(bits) if bits else "") \
            + (f"\n  capped: {', '.join(caps)}" if caps else "")


def _event(kind: str, principal: str, at: str, from_c: str, subject: str,
           detail: dict, evidence: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "kind": kind,
        "principal": principal,
        "observed_at": at,
        "subject": subject,
        "detail": detail,
        "evidence_ref": evidence,
        "cursor": {"from": from_c, "to": at},
    }


# ── watches ──────────────────────────────────────────────────────────────


def watch_due_clocks(repo_root: Path, today: date, horizon_days: int,
                     out: list[dict], principal: str, at: str, from_c: str) -> dict:
    """Dated clocks in DUE.md's open table: near windows and past-unmarked."""
    counts = {"due_window_open": 0, "due_overdue": 0, "due_entries_seen": 0}
    due_path = repo_root / "DUE.md"
    if not due_path.is_file():
        counts["due_entries_seen"] = -1          # source absent
        return counts
    text = due_path.read_text(encoding="utf-8")
    if "## Open clocks" not in text:
        counts["due_entries_seen"] = -1
        return counts
    open_section = text.split("## Open clocks", 1)[1].split("##", 1)[0]
    for line in open_section.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("When", "---", ":---") or set(cells[0]) <= set("-: "):
            continue
        counts["due_entries_seen"] += 1
        when, what = cells[0], cells[1]
        for m in _DUE_DATE_RE.finditer(cells[0]):
            start = _parse_date(m.group(1))
            dates = [start]
            if m.group(3):  # range end, maybe year-less ("11-01")
                year = m.group(2) or start.year
                end = _parse_date(f"{year}-{m.group(3)}")
                if end:
                    dates.append(end)
            for d in dates:
                if d is None:
                    continue
                if d < today:
                    out.append(_event(
                        "due_overdue", principal, at, from_c,
                        what[:80],
                        {"when": d.isoformat(), "days_past": (today - d).days},
                        "DUE.md"))
                    counts["due_overdue"] += 1
                elif (d - today).days <= horizon_days:
                    out.append(_event(
                        "due_window_open", principal, at, from_c,
                        what[:80],
                        {"when": d.isoformat(), "days_left": (d - today).days},
                        "DUE.md"))
                    counts["due_window_open"] += 1
                break_outer = False  # noqa: F841 — placeholder, see below
    return counts


def watch_staleness(state_root: Path, from_date: date | None, today: date,
                     out: list[dict], principal: str, at: str, from_c: str,
                     lookback_days: int) -> dict:
    """Registry rows crossing a staleness floor since the last watch."""
    counts = {"staleness_floor_crossed": 0, "registry_rows": 0}
    csv_path = state_root / "cache" / "jobs.csv"
    if not csv_path.is_file():
        counts["registry_rows"] = -1
        return counts
    thresholds = (("drop", STALE_DROP_DAYS), ("flag", STALE_FLAG_DAYS))
    with csv_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            counts["registry_rows"] += 1
            if row.get("status") not in ("new", "still_open"):
                continue
            first = _parse_date(row.get("first_seen", ""))
            if first is None:
                continue
            for level, threshold in thresholds:
                age_now = (today - first).days
                age_then = ((from_date - first).days
                            if from_date is not None
                            else age_now - lookback_days)
                if age_then < threshold <= age_now:
                    out.append(_event(
                        "staleness_floor_crossed", principal, at, from_c,
                        f"{row.get('org', '?')} — {row.get('title', '?')}"[:80],
                        {"level": level, "threshold_days": threshold,
                         "age_days": age_now, "first_seen": first.isoformat(),
                         "job_id": row.get("id", "")},
                        f"cache/jobs.csv#{row.get('id', '')}"))
                    counts["staleness_floor_crossed"] += 1
    return counts


def watch_runs(state_root: Path, from_dt: datetime | None,
               out: list[dict], principal: str, at: str, from_c: str) -> dict:
    counts = {"run_failed": 0, "runs_seen": 0}
    runs_dir = state_root / "cache" / "runs"
    if not runs_dir.is_dir():
        counts["runs_seen"] = -1
        return counts
    for path in sorted(runs_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        counts["runs_seen"] += 1
        finished = record.get("finished_at")
        if not finished:
            continue                      # running / never terminal — not ours
        try:
            fin = datetime.fromisoformat(finished)
        except ValueError:
            continue
        if fin.tzinfo is None:
            fin = fin.replace(tzinfo=timezone.utc)
        if from_dt is not None and fin <= from_dt:
            continue
        status = record.get("status", "")
        exit_code = record.get("exit_code", 0)
        if status == "failed" or (isinstance(exit_code, int)
                                  and exit_code not in (0, None)):
            out.append(_event(
                "run_failed", principal, at, from_c,
                record.get("run_id", path.stem)[:80],
                {"status": status, "exit_code": exit_code,
                 "finished_at": finished},
                f"cache/runs/{path.name}"))
            counts["run_failed"] += 1
    return counts


def watch_seals(state_root: Path, from_dt: datetime | None,
                out: list[dict], principal: str, at: str, from_c: str) -> dict:
    counts = {"seal_landed": 0, "bundles_seen": 0}
    strata = state_root / "strata"
    if not strata.is_dir():
        counts["bundles_seen"] = -1
        return counts
    for bundle in sorted(strata.iterdir()):
        if (not bundle.is_dir() or bundle.is_symlink()
                or bundle.name.startswith((".", "history-index"))):
            continue
        manifest_path = bundle / "manifest.json"
        if not manifest_path.is_file():
            continue
        counts["bundles_seen"] += 1
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sealed_at = (manifest.get("clock") or {}).get("sealed_at")
        if not sealed_at:
            continue
        try:
            sealed = datetime.fromisoformat(sealed_at)
        except ValueError:
            continue
        if sealed.tzinfo is None:
            sealed = sealed.replace(tzinfo=timezone.utc)
        if from_dt is not None and sealed <= from_dt:
            continue
        out.append(_event(
            "seal_landed", principal, at, from_c,
            bundle.name[:80],
            {"sealed_at": sealed_at,
             "execution": (manifest.get("status") or {}).get("execution", "?"),
             "capture": (manifest.get("status") or {}).get("capture", "?")},
            f"strata/{bundle.name}/manifest.json"))
        counts["seal_landed"] += 1
    return counts


def watch_case_submissions(state_root: Path, from_date: date | None, today: date,
                           out: list[dict], principal: str, at: str,
                           from_c: str, lookback_days: int) -> dict:
    """Applications submitted in the world, read from the case dossiers.

    The authority is the dossier's deliberate ``LoveWork outcome: <kind> <date>``
    marker — the convention `history.py` and `outcome_link.py` already read —
    never a guess from page text: watched content is data, and a captured page
    cannot promote itself into a fact (docs/27, the 2026-09-07 learning).

    Added 2026-10-06 (docs/41 P1), after the a16z SR008 submission — LJ's click
    at 03:02 BST on host-b, pulled to host-a at ~03:03, four hours before the
    cron — went unseen by the 07:00 brief: nothing in the daily loop read case
    directories at all, and the brief instead urged him to draft what he had
    already sent.
    """
    counts = {"submission_observed": 0, "case_dossiers": 0,
              "case_applications_absent": 0}
    cases = state_root / "applications"
    if not cases.is_dir():
        counts["case_applications_absent"] = 1
        return counts
    from outcome_link import CASE_OUTCOME_RE          # one authority for the marker

    for case in sorted(p for p in cases.iterdir() if p.is_dir()):
        from application_packs import dossier_path

        for dossier in [d for d in (dossier_path(case),) if d]:
            counts["case_dossiers"] += 1
            try:
                text = dossier.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in CASE_OUTCOME_RE.finditer(text):
                kind = match.group("kind").lower()
                if kind not in ("applied", "application_sent"):
                    continue            # rejections/interviews are their own kinds
                when = _parse_date(match.group("date") or "")
                if when is None:
                    continue
                fresh = (when >= from_date if from_date is not None
                         else (today - when).days <= lookback_days)
                if not fresh:
                    continue
                out.append(_event(
                    "submission_observed", principal, at, from_c,
                    f"application {kind}: {case.name}",
                    {"kind": kind, "submitted": when.isoformat(),
                     "case": case.name, "days_since": (today - when).days},
                    f"applications/{case.name}/{dossier.name}"))
                counts["submission_observed"] += 1
    return counts


def watch_application_quiet(state_root: Path, today: date,
                            out: list[dict], principal: str, at: str,
                            from_c: str) -> dict:
    """Quiet = the chase window only (14d–30d); 30d+ is closed by decay.

    2026-10-03 decay policy (LJ): very old open cases are a NO by another
    channel — ``snapshot.append_decay_expirations`` closes them with a
    survival probability that halves monthly. Those orgs appear here as
    ``application_closed_by_decay`` (once, on the closing pass), never as
    endlessly 'quiet'."""
    counts = {"application_quiet": 0, "outcomes_rows": 0, "orgs_seen": 0,
              "application_closed_by_decay": 0}
    rows = _read_jsonl(state_root / "dataset" / "outcomes.jsonl")
    counts["outcomes_rows"] = len(rows) if rows else -1
    latest: dict[str, tuple[date, dict]] = {}
    decay_closed: set[str] = set()
    for row in rows:
        org = row.get("org_name") or row.get("org")
        when = _parse_date(str(row.get("date", "")))
        kind = row.get("kind", "")
        if not org or when is None:
            continue
        if kind == "decay_expiration":
            decay_closed.add(org)
            counts["application_closed_by_decay"] += 1
            continue
        if kind not in ("applied", "application_sent", "rejection"):
            continue
        counts["orgs_seen"] += 1
        if org not in latest or when > latest[org][0]:
            latest[org] = (when, row)
    for org, (when, row) in sorted(latest.items()):
        age = (today - when).days
        if org in decay_closed:
            continue                     # closed by the policy, not quiet
        if row.get("kind") in ("applied", "application_sent") \
                and APPLICATION_QUIET_DAYS <= age < DECAY_CLOSE_DAYS:
            out.append(_event(
                "application_quiet", principal, at, from_c,
                f"{org}"[:80],
                {"last_outcome": row.get("kind"), "date": when.isoformat(),
                 "quiet_days": age},
                f"dataset/outcomes.jsonl#outcome_id={row.get('outcome_id', '')}"))
            counts["application_quiet"] += 1
    return counts


def watch_jnj_drops(state_root: Path, from_dt: datetime | None, today: date,
                    out: list[dict], principal: str, at: str, from_c: str) -> dict:
    counts = {"drop_arrived": 0, "archive_files": 0}
    archive = state_root / "sources" / "gmail" / "Jack-and-Jill"
    if not archive.is_dir():
        counts["archive_files"] = -1      # jnj_source_absent (heartbeat)
        return counts
    for path in sorted(archive.glob("*.json")):
        counts["archive_files"] += 1
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        archived = row.get("archived_at")
        when = None
        if archived:
            try:
                when = datetime.fromisoformat(str(archived))
            except ValueError:
                when = None
        if when is None:
            when = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if from_dt is not None and when <= from_dt:
            continue
        subject = str(row.get("subject", path.stem))[:80]
        out.append(_event(
            "drop_arrived", principal, at, from_c,
            subject,
            {"archived_at": when.isoformat(timespec="seconds"),
             "msg_id": row.get("msg_id", path.stem)},
            f"sources/gmail/Jack-and-Jill/{path.name}"))
        counts["drop_arrived"] += 1
        # narrative mirror (D22, PLAN.md T3.1): the peer's memory learns the
        # drop arrived — note-if-exists, never minted, never fatal
        try:
            import mem as _mem
            _mem.note_if_exists(
                _mem.memory_dir(principal, agent="Jack and Jill"),
                "letter: drop arrived — %s" % subject[:60])
        except Exception:
            pass
    return counts


# ── the pass ─────────────────────────────────────────────────────────────


def watch_packs_uninvestigated(state_root: Path, today: date,
                               out: list[dict], principal: str, at: str,
                               from_c: str) -> dict:
    """GO packs whose company-reality block is still unfilled (LJ, 2026-10-06).

    A pack is not ready to send until its investigation block is filled
    (application_packs.NOT_INVESTIGATED_MARKER). This is the daily debt the
    07:00 pack job burns down (≤2/day); the watch reports the queue only
    when it CHANGES, so a standing queue does not nag (crossing-only,
    like every W1 event).
    """
    counts = {"packs_uninvestigated": 0}
    cursor = state_root / "cache" / "packs_uninvestigated.json"
    try:
        from application_packs import uninvestigated_packs
        apps_dir = state_root / "applications"
        pending = uninvestigated_packs(apps_dir)
    except Exception as exc:
        counts["packs_uninvestigated"] = -1   # watch absent (heartbeat)
        logger.debug("packs_uninvestigated watch failed: %s", exc)
        return counts
    n = len(pending)
    counts["packs_uninvestigated"] = n
    prior = None
    if cursor.is_file():
        try:
            prior = json.loads(cursor.read_text(encoding="utf-8")).get("count")
        except (json.JSONDecodeError, OSError):
            prior = None
    if n and prior != n:
        names = ", ".join(p.parent.name for p in pending[:3])
        more = "" if n <= 3 else f" (+{n - 3} more)"
        out.append(_event(
            "packs_uninvestigated", principal, at, from_c,
            f"{n} GO packs await company investigation",
            {"count": n, "newest": names + more},
            "applications/"))
    if prior != n:
        _atomic_json(cursor, {"count": n, "at": at})
    return counts


#: Link verdicts that are not a problem: a live advert, and one we chose not to
#: open (no URL to open).
_LINK_BENIGN_VERDICTS = frozenset({"ok", "skipped"})

def _clean_url(url: str) -> str:
    """The canonical link, from the wiki's own cleaner (one definition)."""
    try:
        from wiki_store import clean_url

        return clean_url(url)
    except Exception:                          # pragma: no cover - defensive
        return url


#: HTTP statuses that mean the advert is GONE, not merely blocked from us.
_LINK_GONE_STATUSES = frozenset({404, 410})

#: How many hard problems one brief carries; the rest is counted.
_LINK_RECHECK_MAX_EVENTS = 5


def _is_gone_verdict(verdict: dict) -> bool:
    """True when the re-check says the advert has ENDED, not merely evaded us.

    `expired` is an expiry marker in the page's own text; `unreachable` covers
    both a 404 and a bot-blocked 403 or a transient timeout, so only the
    statuses that mean "this page is gone" count. The rest is real evidence but
    it is not news: a title that did not match on a JS-rendered page, a LinkedIn
    sign-in wall, a work-mode conflict the crawl already knew about — those are
    summarised in one line instead of leading the brief.
    """
    if str(verdict.get("verdict")) == "expired":
        return True
    return (str(verdict.get("verdict")) == "unreachable"
            and verdict.get("status") in _LINK_GONE_STATUSES)


def watch_link_recheck(state_root: Path, today: date,
                       out: list[dict], principal: str, at: str,
                       from_c: str) -> dict:
    """Problems from the last advert-link re-check (LJ, 2026-10-08).

    The 06:00 `lovework-recheck` pass opens the actionable advert links and
    writes a machine record (`cache/link_checks/*.json`); this watch turns its
    *problems* into brief events, so a GO that has closed is in the 07:00 brief
    as a fact — not merely as an org-memory note that the brief happens to wake
    because some other event named the org. The subject carries the org name,
    which is also what makes `brief._org_memory_context` attach that org's
    narrative.

    Crossing-only like every W1 event: it reports the newest record once, and
    stays silent while the record is unchanged (a standing problem is not news;
    the next re-check re-reports it with a fresh record).
    """
    counts = {"link_recheck": 0}
    records = state_root / "cache" / "link_checks"
    cursor = state_root / "cache" / "link_recheck.json"
    if not records.is_dir():
        counts["link_recheck"] = -1        # the pass has not run yet (heartbeat)
        return counts
    # Newest by the record's OWN `checked_at`, not by file mtime: two records
    # can share an mtime (a re-check and its retry in the same second, or a tree
    # whose times were preserved by a copy), and picking by metadata is how the
    # cockpit once read a receipt child as the newest seal. The mtime only
    # narrows the candidates.
    candidates = sorted(records.glob("*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    newest, payload = None, None
    for path in candidates:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(candidate, dict):
            continue
        if newest is None or str(candidate.get("checked_at") or "") > str(
                payload.get("checked_at") or ""):
            newest, payload = path, candidate
    if newest is None or payload is None:
        counts["link_recheck"] = -1
        logger.debug("no readable link_checks record under %s", records)
        return counts

    checked_at = str(payload.get("checked_at") or "")
    prior: dict = {}
    if cursor.is_file():
        try:
            prior = json.loads(cursor.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prior = {}
    if prior.get("checked_at") == checked_at:
        # Already reported this record. Problems do not accumulate: the next
        # re-check writes a NEW record (dated), and that one speaks again.
        # Both keys are always present — a consumer that sums the counts must
        # not have to guess whether a missing key means zero.
        counts["link_recheck"] = int(prior.get("problems") or 0)
        counts["link_recheck_soft"] = int(prior.get("soft") or 0)
        return counts

    problems = [v for v in (payload.get("verdicts") or [])
                if str(v.get("verdict")) not in _LINK_BENIGN_VERDICTS]
    gone = [v for v in problems if _is_gone_verdict(v)]
    soft: dict[str, int] = {}
    for v in problems:
        if not _is_gone_verdict(v):
            soft[str(v.get("verdict"))] = soft.get(str(v.get("verdict")), 0) + 1
    counts["link_recheck"] = len(gone)
    counts["link_recheck_soft"] = sum(soft.values())
    if (gone or soft) and (prior or checked_at[:10] == today.isoformat()):
        # On a first run, report only a record from today: an older one was
        # already somebody's news (the quiet-first-run rule the watchtower uses).
        for v in gone[:_LINK_RECHECK_MAX_EVENTS]:
            verdict = str(v.get("verdict"))
            out.append(_event(
                "link_recheck", principal, at, from_c,
                f"{v.get('org', '?')} — {v.get('title', '?')}: {verdict}"
                + (f" ({v['detail']})" if v.get("detail") else ""),
                {"verdict": verdict, "org": v.get("org", ""),
                 "title": v.get("title", ""),
                 # cleaned again here: records written before the checker
                 # cleaned at source still carry the tracked URL, and the brief
                 # is what a reader sees (2026-10-08)
                 "url": _clean_url(str(v.get("url") or "")),
                 "status": v.get("status"), "checked_at": checked_at,
                 "report": Path(str(payload.get("report") or "")).name},
                "cache/link_checks/"))
        if len(gone) > _LINK_RECHECK_MAX_EVENTS:
            out.append(_event(
                "link_recheck", principal, at, from_c,
                f"{len(gone) - _LINK_RECHECK_MAX_EVENTS} further advert(s) closed "
                f"in the same re-check",
                {"remainder": len(gone) - _LINK_RECHECK_MAX_EVENTS},
                "cache/link_checks/"))
        if soft:
            # A separate kind, deliberately: the brief's item budget is three,
            # and a summary of title-mismatches must not crowd out the advert
            # that actually closed (which keeps `link_recheck`).
            summary = ", ".join(f"{n} {name.replace('_', ' ')}"
                                for name, n in sorted(soft.items()))
            out.append(_event(
                "link_recheck_soft", principal, at, from_c,
                f"{sum(soft.values())} link(s) worth a look: {summary}",
                {"soft": soft, "checked_at": checked_at},
                "cache/link_checks/"))
    _atomic_json(cursor, {"checked_at": checked_at, "problems": len(gone),
                          "soft": sum(soft.values()),
                          "record": newest.name, "at": at})
    return counts


def watch_wiki_defects(state_root: Path, today: date,
                       out: list[dict], principal: str, at: str,
                       from_c: str) -> dict:
    """The wiki QA audit in the daily brief, per class (2026-10-07).

    Crossing-only like every W1 event, and 0 is silent. The cursor keeps the
    whole class breakdown, not just the total: a total that stays flat while
    one class grows and another shrinks is exactly the state that hides a new
    defect, and a per-class *rise* is the only honest signal that something
    started producing them again.
    """
    counts = {"wiki_defects": 0}
    # ONE authority for the measurement: the self-healing pass (`selfheal.py`)
    # audits, repairs and writes the baseline; the watch reads it. Running a
    # second audit here cost 9 s per principal, and two cursors over one
    # measurement is how the brief and the healer come to disagree about the
    # same tree (2026-10-08). The morning run calls the pass first, so this
    # reads the post-repair state.
    state = state_root / "cache" / "selfheal.json"
    if not state.is_file():
        counts["wiki_defects"] = -1   # no pass yet (heartbeat), not a defect
        logger.debug("no self-heal baseline at %s", state)
        return counts
    try:
        payload = json.loads(state.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        counts["wiki_defects"] = -1
        logger.debug("self-heal baseline unreadable: %s", exc)
        return counts
    total = payload.get("total") or 0
    classes = dict(payload.get("classes") or {})
    rose = list(payload.get("rose") or [])
    counts["wiki_defects"] = total
    if total and (payload.get("reported_total") != total or rose):
        detail = (f" (rising: {', '.join(rose)})" if rose else "")
        out.append(_event(
            "wiki_defects", principal, at, from_c,
            f"wiki audit: {total} defects on file{detail}",
            {"total": total, "classes": classes, "rising": rose,
             "gaps": payload.get("gaps") or {}},
            "wiki/orgs/"))
        # The alarm is consumed once: `rose` belongs to the pass that found it,
        # and leaving it in the file would re-fire the same rise in every brief
        # until the next pass (the class the cursor exists to prevent).
        payload["reported_total"] = total
        payload["rose"] = []
        _atomic_json(state, payload)
    return counts


def run_watch(state_root: Path, repo_root: Path, *, now: datetime | None = None,
              dry_run: bool = False, horizon_days: int = DEFAULT_HORIZON_DAYS,
              ) -> WatchResult:
    """One watchtower pass over one principal. Writes only when not dry."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    principal = state_root.name
    at = now.isoformat(timespec="seconds")

    cursor_path = state_root / "cache" / CURSOR_NAME
    from_c: str | None = None
    from_dt: datetime | None = None
    from_date: date | None = None
    if cursor_path.is_file():
        try:
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
            from_c = cursor.get("last_watch_at")
            from_dt = datetime.fromisoformat(from_c)
            from_date = from_dt.date()
        except (json.JSONDecodeError, ValueError, OSError):
            from_c, from_dt, from_date = None, None, None
    if from_dt is not None and from_dt.tzinfo is None:
        from_dt = from_dt.replace(tzinfo=timezone.utc)

    global _DRY_RUN
    _DRY_RUN = dry_run
    events: list[dict] = []
    counts: dict[str, int] = {}
    counts.update(watch_due_clocks(repo_root, today, horizon_days,
                                   events, principal, at, from_c or "first-run"))
    counts.update(watch_staleness(state_root, from_date, today,
                                  events, principal, at, from_c or "first-run",
                                  FIRST_RUN_LOOKBACK_DAYS))
    counts.update(watch_runs(state_root, from_dt, events,
                             principal, at, from_c or "first-run"))
    counts.update(watch_seals(state_root, from_dt, events,
                              principal, at, from_c or "first-run"))
    counts.update(watch_case_submissions(state_root, from_date, today,
                                         events, principal, at, from_c or "first-run",
                                         FIRST_RUN_LOOKBACK_DAYS))
    counts.update(watch_application_quiet(state_root, today, events,
                                          principal, at, from_c or "first-run"))
    counts.update(watch_jnj_drops(state_root, from_dt, today, events,
                                  principal, at, from_c or "first-run"))
    counts.update(watch_packs_uninvestigated(state_root, today, events,
                                             principal, at,
                                             from_c or "first-run"))
    counts.update(watch_wiki_defects(state_root, today, events,
                                     principal, at, from_c or "first-run"))
    counts.update(watch_link_recheck(state_root, today, events,
                                     principal, at, from_c or "first-run"))
    # Every watch has run; the remaining writes in this function are already
    # guarded by `dry_run` explicitly. Reset so a later pass in the same process
    # (a test, or a batch over principals) writes its cursors normally.
    _DRY_RUN = False

    # bounded output: cap each kind, count the remainder
    capped: dict[str, int] = {}
    kept: list[dict] = []
    per_kind: dict[str, list[dict]] = {}
    for event in events:
        per_kind.setdefault(event["kind"], []).append(event)
    for kind, rows in per_kind.items():
        if len(rows) > MAX_EVENTS_PER_KIND:
            capped[kind] = len(rows) - MAX_EVENTS_PER_KIND
        kept.extend(rows[:MAX_EVENTS_PER_KIND])

    heartbeat = _event(
        "watch_completed", principal, at, from_c or "first-run",
        "watchtower heartbeat",
        {"counts": counts, "capped": capped, "dry_run": dry_run,
         "horizon_days": horizon_days},
        f"cache/{CURSOR_NAME}")
    kept.append(heartbeat)

    result = WatchResult(principal=principal, at=at, from_cursor=from_c,
                         dry_run=dry_run, events=kept, counts=counts,
                         capped=capped)
    if not dry_run:
        events_path = state_root / "dataset" / EVENTS_NAME
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a", encoding="utf-8") as fh:
            for event in kept:
                fh.write(json.dumps(event, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        _atomic_json(cursor_path, {
            "last_watch_at": at,
            "watch_count": _prior_watch_count(cursor_path) + 1,
            "schema_version": SCHEMA_VERSION,
        })
    return result


def _prior_watch_count(cursor_path: Path) -> int:
    if not cursor_path.is_file():
        return 0
    try:
        return int(json.loads(cursor_path.read_text(encoding="utf-8"))
                   .get("watch_count", 0))
    except (json.JSONDecodeError, OSError, ValueError):
        return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The Shoulder W1 — watchtower")
    parser.add_argument("--profile", default="all",
                        help="principal name or 'all' (default)")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and print; write nothing")
    parser.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON_DAYS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    import config

    repo_root = Path(config.LOVEWORK_ROOT)
    principals = (
        sorted(p.name for p in config.STATE_DIR.iterdir() if p.is_dir())
        if args.profile == "all" else [args.profile])
    results = []
    for principal in principals:
        state_root = config.STATE_DIR / principal
        if not state_root.is_dir():
            print(f"no state dir: {state_root}", file=sys.stderr)
            return 66
        from writer_boundary import WriterBusy, standalone_writer_guard
        try:
            with standalone_writer_guard(principal, mode="watch",
                                         state_root=state_root,
                                         run_id=f"watch-{uuid.uuid4().hex[:8]}"):
                results.append(run_watch(
                    state_root, repo_root,
                    dry_run=args.dry_run, horizon_days=args.horizon_days))
        except WriterBusy as busy:
            print(f"[watch] deferred — writer busy: {busy}")
            continue
    if args.json:
        print(json.dumps(
            [{"principal": r.principal, "at": r.at, "dry_run": r.dry_run,
              "counts": r.counts, "capped": r.capped,
              "events": r.events} for r in results],
            indent=2, sort_keys=True))
    else:
        for r in results:
            print(r.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
