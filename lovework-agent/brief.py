#!/usr/bin/env python3
"""The Shoulder W2 — the brief (docs/35).

One ranked, priced, ≤3-item note from the events the watchtower collected
since the last brief — the watch *speaking*, at
most once a day, only when there is something worth attention.

Rules (docs/35 §2, binding):

* **Deterministic core, LLM polish.** Events are selected, clustered and
  ranked deterministically; one small LLM pass may rewrite them into plain
  language. If the LLM is unavailable or returns unparseable output, the
  brief is the deterministic one — never silently skipped, never invented.
* **Honest-state vocabulary only.** "Open at least N days", "deferred",
  "unknown"; never "posted N days ago", never claiming a crawl or a send.
* **Attention protection.** No new events since the last brief ⇒ **no
  brief** (exit 0, recorded) — a quiet day is a successful day. Cap: 3
  items, each with an evidence pointer and a priced next action drawn from
  the read ladder (docs/27 §3).
* **Cursor discipline.** The brief cursor advances only after the brief
  file is written; delivery (``--send`` → notify.py, receipted) is a
  separate step the operator opts into.

Usage::

    ../venv/bin/python3 brief.py --profile lj            # compute + write
    ../venv/bin/python3 brief.py --profile lj --send     # + email via notify.py
    ../venv/bin/python3 brief.py --profile lj --dry-run  # print, write nothing
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watch import EVENTS_NAME, _read_jsonl  # noqa: E402
import health  # noqa: E402

logger = logging.getLogger("brief")

SCHEMA_VERSION = 1
BRIEFS_DIR = "briefs"
CURSOR_NAME = "brief-cursor.json"
MAX_ITEMS = 3
MAX_CANDIDATES = 12          # bounded input to the LLM pass

# Deterministic priority: what most demands the principal's attention first.
# (negative-space and failure states outrank routine good news)
_PRIORITY = {
    "due_overdue": 0,
    "run_failed": 1,
    "drop_arrived": 2,
    # a closed advert invalidates a pack we have not sent yet — it saves an
    # evening, so it ranks above an application already in flight.
    # `link_problem` is the same fact from the other writer of it: the link step
    # inside the self-healing pass emits it when no fresh record exists (the
    # 06:00 pass did not run). One rank, because a reader does not care which
    # mechanism noticed.
    "link_recheck": 2.5,
    "link_problem": 2.5,
    "application_quiet": 3,
    # a submission supersedes the window that prompted it, so it ranks just
    # above due_window_open (docs/41 P1)
    "submission_observed": 4,
    # the pack queue: standing debt the 07:00 pack job burns down two a day
    "packs_uninvestigated": 4.5,
    "due_window_open": 5,
    "staleness_floor_crossed": 6,
    # evidence worth a look, not news: a title that did not match on a
    # JS-rendered page, a sign-in wall, a work-mode conflict the crawl knew
    "link_recheck_soft": 6.5,
    "seal_landed": 7,
    # hygiene: the wiki's defect classes, when the total changes
    "wiki_defects": 8,
}

# Priced next actions per kind (read-ladder rungs; the LLM may rephrase,
# never reprice).
_SUGGESTED_ACTIONS = {
    "due_overdue": ("Act on or re-date the clock in DUE.md", "free (rung 0)"),
    "due_window_open": ("Prepare while the window is open", "free (rung 0)"),
    "run_failed": ("Inspect the run record and log; retry only after a cause",
                   "free (rung 1)"),
    "seal_landed": ("No action — history recorded", "free (rung 0)"),
    "application_quiet": ("Consider a polite chase or mark the case cold",
                          "your reply (W3)"),
    "staleness_floor_crossed": ("Trust the floor: the advert is aging; verify "
                                "on the primary page before applying",
                                "one page fetch (rung 5, cents)"),
    "drop_arrived": ("Read the drop; the letter answers it", "free (rung 2)"),
    "link_recheck": ("Open the advert before spending an evening on its pack; "
                     "if it has closed, drop the GO and say so in the case",
                     "one page fetch (rung 5, cents)"),
    "link_problem": ("Open the advert before spending an evening on its pack; "
                     "if it has closed, drop the GO and say so in the case",
                     "one page fetch (rung 5, cents)"),
    "packs_uninvestigated": ("The 07:00 pack job burns two a day; open one "
                             "yourself if you want it sooner",
                             "free (rung 0)"),
    "wiki_defects": ("Skim the classes; `selfheal.py --profile <p>` names what "
                     "rose and what was repaired", "free (rung 0)"),
    "link_recheck_soft": ("Skim; open the ones you would act on. A title that "
                          "did not match may be a JS page, not a dead advert",
                          "free (rung 0)"),
    "submission_observed": ("Nothing to do — the case dossier records it; "
                            "watch the inbox for the answer",
                            "free (rung 0)"),
}

# The prompt lives in `prompts.py` — one place for all of them (2026-10-15).
from prompts import SHOULDER_BRIEF_PROMPT as _PROMPT


@dataclass
class BriefResult:
    principal: str
    at: str
    written: bool
    path: Path | None = None
    items: list[dict] = field(default_factory=list)
    source: str = "deterministic"      # or "llm"
    events_consumed: int = 0
    dry_run: bool = False
    sent: bool = False
    health: str = ""

    def render_markdown(self) -> str:
        """**One section of the day's file**, not a file of its own.

        LJ, 2026-10-16: *"Having every brief in its own file, on the hour — strikes me
        being excessive … it makes sense to append all the briefs in a single file,
        append only, one after the other, for the entire day."* He is right, and the
        hourly cadence made it acute: seventeen 1 KB files in one day, when the
        question a reader asks is *"what did the watch say today?"*. So the brief is
        appended to `brief-YYYY-MM-DD.md` under a timestamped heading — the day is the
        file, the hour is the section.
        """
        lines = [
            f"## {self.at[11:16]} · {self.events_consumed} event(s) · "
            f"{self.source} rendering",
            "",
        ]
        if self.health:
            lines += [self.health, ""]
        if not self.items:
            lines.append("_Nothing worth your attention. A quiet hour counts._")
        for i, item in enumerate(self.items, 1):
            lines += [
                f"### {i}. {item.get('headline', '?')}",
                "",
                item.get("why", ""),
                "",
                f"**Next:** {item.get('next_action', '—')} "
                f"· _{item.get('cost', 'free')}_ · "
                f"[{item.get('evidence_ref', '—')}]",
                "",
            ]
        lines += ["", "---", ""]
        return "\n".join(lines)


def _cluster(events: list[dict]) -> list[dict]:
    """Collapse repeats of a kind into one candidate with a count."""
    clusters: dict[str, dict] = {}
    for event in events:
        kind = event.get("kind", "unknown")
        cluster = clusters.get(kind)
        if cluster is None:
            clusters[kind] = {
                "kind": kind, "count": 1,
                "examples": [event], "newest": event,
            }
        else:
            cluster["count"] += 1
            cluster["examples"].append(event)
            if event.get("observed_at", "") > cluster["newest"].get(
                    "observed_at", ""):
                cluster["newest"] = event
    return sorted(clusters.values(),
                  key=lambda c: _PRIORITY.get(c["kind"], 99))


def _candidate_lines(candidates: list[dict]) -> str:
    lines = []
    for c in candidates:
        newest = c["newest"]
        example = (f"{newest.get('subject', '?')} — "
                   f"{json.dumps(newest.get('detail', {}), sort_keys=True)}")
        if c["count"] > 1:
            example = f"[{c['count']} such events; newest] {example}"
        action, cost = _SUGGESTED_ACTIONS.get(
            c["kind"], ("No action suggested", "free"))
        lines.append(f"- kind={c['kind']} | {example} | "
                     f"suggested: {action} | cost: {cost} | "
                     f"evidence: {newest.get('evidence_ref', '?')}")
    return "\n".join(lines)


def _deterministic_items(candidates: list[dict]) -> list[dict]:
    items = []
    for c in candidates[:MAX_ITEMS]:
        newest = c["newest"]
        action, cost = _SUGGESTED_ACTIONS.get(
            c["kind"], ("No action suggested", "free"))
        count = (f" ({c['count']} events)"
                 if c["count"] > 1 else "")
        items.append({
            "headline": f"{c['kind'].replace('_', ' ')}{count}: "
                        f"{newest.get('subject', '?')}",
            "why": json.dumps(newest.get("detail", {}), sort_keys=True),
            "next_action": action,
            "cost": cost,
            "evidence_ref": newest.get("evidence_ref", "?"),
        })
    return items


def _parse_llm_items(text: str) -> list[dict] | None:
    """Defensive JSON extraction; None on any failure (fallback follows)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return None
    clean = []
    for item in items[:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        if not item.get("headline") or not item.get("evidence_ref"):
            continue
        clean.append({
            "headline": str(item.get("headline", ""))[:160],
            "why": str(item.get("why", ""))[:400],
            "next_action": str(item.get("next_action", "—"))[:200],
            "cost": str(item.get("cost", "free"))[:80],
            "evidence_ref": str(item.get("evidence_ref", "?"))[:200],
        })
    return clean or None


def _org_memory_context(state_root: Path, candidates: list[dict],
                        *, max_orgs: int = 8) -> str:
    """Wake the org memories named by the brief's events (PLAN.md T2.5).

    Narrative context only — the events themselves stay the brief's
    evidence. At most `max_orgs` blocks (48-line wake budgets each); an org
    with no memory is simply absent, never minted here."""
    orgs_dir = state_root / "dataset" / "memory" / "orgs"
    if not orgs_dir.is_dir():
        return ""
    blob = " ".join(
        str(c["newest"].get("subject", "")) + " " +
        json.dumps(c["newest"].get("detail", {}), sort_keys=True)
        for c in candidates).casefold()
    blocks = []
    try:
        import mem as _mem
        for d in sorted(orgs_dir.iterdir()):
            if not d.is_dir():
                continue
            slug = d.name
            display = slug.replace("-", " ")
            if slug.casefold() not in blob and display.casefold() not in blob:
                continue
            wake = _mem.capture_memo(d, ["wake"])
            wake = "\n".join(
                ln for ln in wake.splitlines()
                if ln.strip() and ln.strip() != "You are awake.")
            if not wake:
                continue
            blocks.append("## org memory: %s\n%s" % (display, wake))
            if len(blocks) >= max_orgs:
                break
    except Exception as exc:                       # context is best-effort
        logger.debug("org memory context skipped: %s", exc)
        return ""
    return "\n\n".join(blocks)


def _llm_items(principal: str, candidates: list[dict], llm,
               memory_context: str = "") -> list[dict] | None:
    if llm is None:
        return None
    prompt = _PROMPT.format(
        principal=principal, max_items=MAX_ITEMS,
        events=_candidate_lines(candidates[:MAX_CANDIDATES]))
    if memory_context:
        prompt += ("\n\nOrg memories (narrative context — the events above "
                   "remain the evidence; use memory for arc and tone, never "
                   "for numbers):\n" + memory_context)
    try:
        text = llm.chat(
            [{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
            context="watch-brief",   # was "shoulder-brief" until 2026-10-15
        )
    except Exception as exc:                       # noqa: BLE001 — any LLM failure
        logger.warning("brief LLM pass failed: %s", exc)
        return None
    return _parse_llm_items(text)


def run_brief(state_root: Path, *, now: datetime | None = None,
              llm=None, dry_run: bool = False) -> BriefResult:
    """Compute and write one brief from events since the last brief cursor."""
    now = now or datetime.now(timezone.utc)
    principal = state_root.name
    at = now.isoformat(timespec="seconds")
    try:
        health_text = health.brief_line(health.read_status(state_root))
    except Exception:                       # the brief must survive a bad health file
        health_text = ""

    cursor_path = state_root / "cache" / CURSOR_NAME
    from_at: str | None = None
    if cursor_path.is_file():
        try:
            from_at = json.loads(
                cursor_path.read_text(encoding="utf-8")).get("last_brief_at")
        except (json.JSONDecodeError, OSError):
            from_at = None

    rows = _read_jsonl(state_root / "dataset" / EVENTS_NAME)
    fresh = []
    for row in rows:
        if row.get("kind") == "watch_completed":
            continue                       # heartbeat is never brief material
        observed = str(row.get("observed_at", ""))
        if from_at and observed <= from_at:
            continue
        fresh.append(row)

    if not fresh:
        return BriefResult(principal=principal, at=at, written=False,
                           events_consumed=0, dry_run=dry_run)

    candidates = _cluster(fresh)
    memory_context = _org_memory_context(state_root, candidates)
    items = _llm_items(principal, candidates, llm,
                       memory_context=memory_context)
    source = "llm"
    if items is None:
        items = _deterministic_items(candidates)
        source = "deterministic"

    result = BriefResult(principal=principal, at=at, written=True,
                         items=items, source=source,
                         events_consumed=len(fresh), dry_run=dry_run,
                         health=health_text)
    if dry_run:
        return result

    briefs_dir = state_root / BRIEFS_DIR
    briefs_dir.mkdir(parents=True, exist_ok=True)
    # **One file per day, appended** (LJ, 2026-10-16). Seventeen 1 KB files in a day
    # answer the wrong question: the reader asks what the watch said *today*, not what
    # it said at 14:09. Append-only, like every other ledger here — the day file only
    # ever grows, and a brief that has been written is never rewritten.
    path = briefs_dir / f"brief-{at[:10]}.md"
    fresh = not path.is_file()
    with path.open("a", encoding="utf-8") as handle:
        if fresh:
            handle.write(
                f"# Watch briefs — {principal} — {at[:10]}\n\n"
                f"_Appended hourly; one section per brief. A quiet hour writes "
                f"nothing, and that is the correct outcome. Deterministic watches, "
                f"honest states, no crawling, no sending on your behalf "
                f"(docs/35-the-watch.md). Replies feed reactions.jsonl._\n\n")
        handle.write(result.render_markdown() + "\n")
    result.path = path

    tmp = cursor_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "last_brief_at": at,
        "brief_count": _prior_count(cursor_path) + 1,
        "schema_version": SCHEMA_VERSION,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, cursor_path)
    return result


def _prior_count(cursor_path: Path) -> int:
    if not cursor_path.is_file():
        return 0
    try:
        return int(json.loads(cursor_path.read_text(encoding="utf-8"))
                   .get("brief_count", 0))
    except (json.JSONDecodeError, OSError, ValueError):
        return 0


def _send(path: Path, principal: str, log_file: Path | None = None) -> bool:
    """Deliver via notify.py --mode brief (receipted email).

    The sent message id is recorded as a brief receipt (W3) so
    ``reactions.py poll`` can find replies to this thread."""
    import re as _re
    import subprocess

    here = Path(__file__).resolve().parent
    cmd = [sys.executable, str(here / "notify.py"),
           "--report", str(path), "--mode", "brief"]
    if log_file:
        cmd += ["--log", str(log_file)]
    completed = subprocess.run(cmd, capture_output=True, text=True, cwd=here)
    if completed.returncode != 0:
        # Silence here is a defect: `--send` used to fail with no line of output,
        # which reads exactly like "not opted in". notify.py currently rejects
        # `--mode brief` (its argparse choices stop at reassess), so this fires
        # until the brief path is implemented end to end — see LEARNINGS.
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        logger.warning("brief not emailed — notify.py exited %s: %s",
                       completed.returncode, detail[-1] if detail else "(no output)")
        print(f"[brief] email NOT sent — notify.py exited {completed.returncode}"
              f"{': ' + detail[-1] if detail else ''}", file=sys.stderr)
        return False
    match = _re.search(r"message[_ ]id[=:}]?\s*\"?([0-9a-f]{8,})",
                       completed.stdout + completed.stderr)
    if match:
        state_root = path.parent.parent          # state/<p>/briefs/x.md
        subject = (path.read_text(encoding="utf-8")
                   .splitlines()[0].lstrip("# ").strip())
        from reactions import record_brief_receipt
        record_brief_receipt(
            state_root, at=datetime.now(timezone.utc)
            .isoformat(timespec="seconds"),
            path=str(path), message_id=match.group(1),
            subject=subject)
    return True


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The Shoulder W2 — the brief")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send", action="store_true",
                        help="email the brief via notify.py (opt-in)")
    parser.add_argument("--no-llm", action="store_true",
                        help="deterministic brief only (no model call)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    import config

    config.set_active_profile(args.profile)
    state_root = config.STATE_DIR / args.profile
    if not state_root.is_dir():
        print(f"no state dir: {state_root}", file=sys.stderr)
        return 66

    llm = None
    if not args.no_llm:
        try:
            from llm_client import LLMClient
            llm = LLMClient()
        except Exception as exc:                   # noqa: BLE001
            logger.warning("no LLM available, brief will be deterministic: %s",
                           exc)

    from writer_boundary import WriterBusy, standalone_writer_guard
    try:
        with standalone_writer_guard(
                args.profile, mode="brief", state_root=state_root,
                run_id=f"brief-{uuid.uuid4().hex[:8]}"):
            result = run_brief(state_root, dry_run=args.dry_run, llm=llm)
    except WriterBusy:
        print("[brief] deferred — writer busy")
        return 75

    if args.send and result.written and result.path and not args.dry_run:
        result.sent = _send(result.path, args.profile)

    if args.json:
        print(json.dumps({
            "principal": result.principal, "at": result.at,
            "written": result.written, "source": result.source,
            "events_consumed": result.events_consumed,
            "health": result.health,
            "sent": result.sent,
            "items": result.items,
            "path": str(result.path) if result.path else None}, indent=2))
    elif result.written:
        print(result.render_markdown())
        if result.path:
            print(f"[brief] written: {result.path}")
        if result.sent:
            print("[brief] emailed (receipted)")
    else:
        print(f"[brief] {args.profile}: nothing worth attention "
              f"(0 events since cursor) — quiet day, no brief written")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
