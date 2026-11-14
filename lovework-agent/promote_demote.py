#!/usr/bin/env python3
"""Promotion/demotion policy for the market lists (docs/37 §4) — v1, conservative.

The question this answers, from the company-event cube
(``{date, principal, company, event}`` rows): **which companies do we start
watching, and which do we stop or slow down watching** — per principal.

Two directions, two instruments, one discipline:

* **PROMOTE** — a company that is *not* on any list keeps producing strong
  evidence (GO/MAYBE across distinct dates, recent, decently scored) → it
  joins ``company_pages.yaml`` (the cadence instrument; ``add_entry`` is
  idempotent and only ever adds). The hand-curated ``market_tracker.txt``
  is the *principal's* market view and is never machine-edited.
* **DEMOTE** — a company that *is* on a list has enough sightings to be
  judged (≥ ``DEMOTE_MIN_DATES`` distinct dates) and has produced **zero**
  strong evidence ever, with its last sighting older than
  ``DEMOTE_STALE_DAYS`` → *reported* for demotion. In ``--apply`` mode a
  ``company_pages.yaml`` entry's cadence is lengthened (to
  ``DEMOTE_CADENCE_DAYS``), never removed; tracker entries are report-only.

Conservative by construction (each rule costs a measured mistake):

1. **One lucky sweep cannot promote** — needs ≥2 strong events on ≥2
   distinct dates (the existing ``candidates()`` floor, kept).
2. **Stale evidence does not promote** — the last event must be within
   ``PROMOTE_RECENCY_DAYS`` (90); a company that was interesting in July
   and silent since waits.
3. **Weak MAYBEs do not promote alone** — best score must clear
   ``PROMOTE_MIN_BEST`` (6.0).
4. **Zero events is not demotion** — a listed company we have *never*
   seen events for is a broken-listing suspicion (the homepage-vs-board
   lesson, docs/37 §3), reported as ``silent``, never demoted. Only a
   company we have *seen often enough to judge* (≥4 dates) with zero
   strong evidence ever is a demotion candidate.
5. **Protections beat evidence** — any GO ever, any positive reaction
   ever, or any application in the outcome ledger protects a listed
   company from demotion outright.
6. **Bounded** — at most ``--max-promote`` promotions per run (crawl
   budget: every watched org costs a re-visit; docs/37 §3b), and every
   decision — applied or not — is appended to
   ``state/<p>/dataset/list_decisions.jsonl`` with its evidence.

Default action is **report-only**; ``--apply`` performs the writes.
No LLM, no network: the cube, the ledgers and the lists are enough.

Usage::

    ../venv/bin/python3 promote_demote.py --profile lj
    ../venv/bin/python3 promote_demote.py --profile all --apply --json
"""

from __future__ import annotations
import jsonl_io

import argparse
import json
import logging
import os
import re
import sys
import uuid
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from company_events import norm_key, tally  # noqa: E402 — the cube

logger = logging.getLogger("promote_demote")

SCHEMA_VERSION = 1
DECISIONS_NAME = "list_decisions.jsonl"

# ── policy constants (v1, conservative; every default cites its reason) ──

PROMOTE_MIN_STRONG = 2        # docs/37 §4: "one lucky sweep cannot promote"
PROMOTE_MIN_DATES = 2         #   ditto — strength must be spread over time
PROMOTE_MIN_BEST = 6.0        # weak MAYBEs alone are not a watch-list case
PROMOTE_RECENCY_DAYS = 90     # stale evidence waits, it does not promote
PROMOTE_TIER_A_STRONG = 4     # tier A: the clearly-hot companies …
PROMOTE_TIER_A_DATES = 4
PROMOTE_TIER_A_BEST = 7.5
PROMOTE_TIER_A_RECENCY = 42
PROMOTE_CADENCE_B = 30        # … get 21 days; everyone else gets the default
PROMOTE_CADENCE_A = 21
MAX_PROMOTE = 8               # crawl budget per run (docs/37 §3b cost table)

DEMOTE_MIN_DATES = 4          # enough sightings to judge (Poolside: 19)
DEMOTE_STALE_DAYS = 30        # and quiet recently, not just ever
DEMOTE_CADENCE_DAYS = 90      # lengthen, never remove (v1)

_RECENT_DAYS = 14             # last event newer than this → too soon to judge

POSITIVE_REACTIONS = {"excited", "interested"}
OUTCOME_COOLDOWN_DAYS = 18 * 30   # the reapply rule (docs/24: poolside)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _days_since(value: str | None, today: date) -> int | None:
    if not value:
        return None
    try:
        return (today - date.fromisoformat(str(value)[:10])).days
    except ValueError:
        return None


# ── pure decisions (no I/O; every input is a cube cell or a guard flag) ──


def promotion_decision(cell: dict, *, today: date) -> dict | None:
    """Should this unlisted company join the watch list? None = no."""
    strong = (cell["by_event"].get("go", 0)
              + cell["by_event"].get("maybe", 0))
    dates = cell["distinct_dates"]
    best = cell["best_score"] or 0.0
    last_age = _days_since(cell.get("last"), today)
    if strong < PROMOTE_MIN_STRONG or dates < PROMOTE_MIN_DATES:
        return None
    if best < PROMOTE_MIN_BEST:
        return None
    if last_age is None or last_age > PROMOTE_RECENCY_DAYS:
        return None
    go = cell["by_event"].get("go", 0)
    hot = (strong >= PROMOTE_TIER_A_STRONG and dates >= PROMOTE_TIER_A_DATES
           and best >= PROMOTE_TIER_A_BEST and last_age <= PROMOTE_TIER_A_RECENCY)
    return {
        "tier": "A" if hot else "B",
        "cadence_days": PROMOTE_CADENCE_A if hot else PROMOTE_CADENCE_B,
        "strong": strong, "go": go, "dates": dates, "best": best,
        "last_age_days": last_age,
        "score": round(go * 20 + strong * 10 + dates + best, 1),
        "reason": (f"{go} GO + {strong - go} MAYBE across {dates} dates, "
                   f"best {best:.1f}, last seen {last_age}d ago"),
    }


def demotion_decision(cell: dict | None, *, today: date,
                      protected: dict) -> dict | None:
    """Should this listed company be slowed or reported? None = keep.

    ``protected`` carries the guardrails: any_go_ever, positive_reaction,
      applied_recently. A company with no cube cell at all is *silent*
      (possible broken listing — report only, never demote)."""
    for guard, why in (("any_go_ever", "has a GO on record"),
                       ("positive_reaction", "the principal reacted well"),
                       ("applied_recently", "an application is in cooldown")):
        if protected.get(guard):
            return None
    if cell is None or cell["events"] == 0:
        return {"action": "silent",
                "reason": "no cube events — listing may be broken "
                          "(homepage-vs-board lesson, docs/37 §3); report only"}
    dates = cell["distinct_dates"]
    strong = (cell["by_event"].get("go", 0)
              + cell["by_event"].get("maybe", 0))
    last_age = _days_since(cell.get("last"), today)
    if dates < DEMOTE_MIN_DATES:
        return None                            # not enough sightings to judge
    if last_age is not None and last_age < _RECENT_DAYS:
        return None                            # too soon — it may still produce
    if strong > 0:
        return None                            # has strong evidence: keep
    if last_age is None or last_age < DEMOTE_STALE_DAYS:
        return None
    return {
        "action": "demote",
        "dates": dates, "events": cell["events"], "strong": 0,
        "last_age_days": last_age,
        "new_cadence_days": DEMOTE_CADENCE_DAYS,
        "reason": (f"{dates} dates seen, {cell['events']} events, "
                   f"zero GO/MAYBE ever, last seen {last_age}d ago"),
    }


# ── readers: what is currently listed, and the protections ───────────────


def _load_yaml_list(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return [e for e in (data or []) if isinstance(e, dict) and e.get("name")]
    except Exception as exc:                       # noqa: BLE001
        logger.warning("could not read %s: %s", path, exc)
        return []


def _tracker_names(path: Path) -> list[str]:
    """Company names from a market_tracker.txt — real entries only.

    A tracker entry is `• Name — URL — note`; the header/prose sections
    also use bullets, so a bullet whose second segment is not an http(s)
    URL is prose (provenance notes, eligibility text), not a company —
    measured on the live lists, where the naive parse produced silent
    'companies' like "UK (primary base)" and "Live URL liveness checks…"."""
    if not path.is_file():
        return []
    names = []
    for line in jsonl_io.lines(path):
        line = line.strip()
        if not line.startswith("•"):
            continue
        parts = line[1:].split("—")
        if len(parts) < 2:
            continue
        name, url = parts[0].strip(), parts[1].strip()
        if name and url.startswith("http"):
            names.append(name)
    return names


def _protections(principal: str, state_root: Path) -> dict[str, dict]:
    """per-canonical-name guardrails from outcomes + reactions."""
    from company_events import canonical_company

    guards: dict[str, dict] = {}

    def _guard(name: str) -> dict:
        key = canonical_company(name) or name.casefold()
        if key not in guards:
            guards[key] = {"any_go_ever": False, "positive_reaction": False,
                           "applied_recently": False}
        return guards[key]

    for line in (state_root / "dataset" / "outcomes.jsonl").read_text(
            encoding="utf-8").splitlines() if (
            state_root / "dataset" / "outcomes.jsonl").is_file() else []:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        org = row.get("org_name") or row.get("org")
        if not org:
            continue
        g = _guard(org)
        if row.get("kind") == "applied":
            applied_age = _days_since(row.get("date"), date.today())
            if applied_age is None or applied_age <= OUTCOME_COOLDOWN_DAYS:
                g["applied_recently"] = True

    reactions_path = state_root / "dataset" / "reactions.jsonl"
    if reactions_path.is_file():
        for line in jsonl_io.lines(reactions_path):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("reaction") in POSITIVE_REACTIONS:
                for field in ("stimulus", "verbatim"):
                    text = str(row.get(field) or "")
                    for guard_name, g in list(guards.items()):
                        if guard_name and guard_name[:8].lower() in text.lower():
                            g["positive_reaction"] = True
    return guards


# ── the pass ─────────────────────────────────────────────────────────────


def run_policy(state_root: Path, repo_root: Path, *, today: date | None = None,
               max_promote: int = MAX_PROMOTE, apply: bool = False) -> dict:
    """One policy pass over one principal. Report-only unless ``apply``."""
    today = today or date.today()
    principal = state_root.name
    at = _utc_now_iso()

    from company_events import canonical_company

    cube = tally(principal=principal)
    yaml_path = repo_root / "profiles" / principal / "company_pages.yaml"
    import logging as _logging
    yaml_list, list_load_errors = _load_yaml_list(yaml_path) \
        if False else (_load_yaml_list(yaml_path), [])
    # a zero from a BROKEN file must be distinguishable from an empty one
    if yaml_path.is_file():
        try:
            import yaml as _yaml
            _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except Exception as exc:                     # noqa: BLE001
            list_load_errors.append(f"{yaml_path.name}: "
                                    f"{type(exc).__name__}: {str(exc)[:90]}")
            _logging.getLogger("promote_demote").warning(
                "could not read %s: %s", yaml_path, exc)
    tracker_names = _tracker_names(repo_root / "profiles" / principal
                                   / "market_tracker.txt")
    listed_keys = {canonical_company(e["name"]) or e["name"].casefold()
                   for e in yaml_list}
    listed_keys |= {canonical_company(n) or n.casefold()
                    for n in tracker_names}

    promotions, skips = [], {"already_listed": 0, "no_evidence": 0}
    for (company, name), cell in cube.items():
        if name != principal:
            continue
        if (canonical_company(company) or company.casefold()) in listed_keys:
            skips["already_listed"] += 1
            continue
        decision = promotion_decision(cell, today=today)
        if decision is None:
            skips["no_evidence"] += 1
            continue
        decision["company"] = company
        promotions.append(decision)
    promotions.sort(key=lambda d: -d["score"])
    promotions = promotions[:max_promote]

    # demotions across both instruments
    protections = _protections(principal, state_root)
    cube_by_key = {(canonical_company(c) or c.casefold()): cell
                   for (c, n), cell in cube.items() if n == principal}

    demotions, silenced, protected_count = [], [], 0
    seen_keys = set()
    for entry in yaml_list:
        key = canonical_company(entry["name"]) or entry["name"].casefold()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        cell = cube_by_key.get(key)
        guard = protections.get(key, {})
        if any((cell and cell["by_event"].get("go", 0) > 0,
                guard.get("any_go_ever"),
                guard.get("positive_reaction"),
                guard.get("applied_recently"))):
            protected_count += 1
            continue
        decision = demotion_decision(cell, today=today, protected=guard)
        if decision and decision["action"] == "silent":
            silenced.append({"company": entry["name"], "instrument":
                             "company_pages.yaml", **{
                                 k: v for k, v in decision.items()
                                 if k != "action"}})
        elif decision:
            demotions.append({"company": entry["name"],
                              "instrument": "company_pages.yaml",
                              "current_cadence": entry.get("cadence_days"),
                              **{k: v for k, v in decision.items()
                                 if k != "action"}})
    for name in tracker_names:
        key = canonical_company(name) or name.casefold()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        cell = cube_by_key.get(key)
        guard = protections.get(key, {})
        if any((cell and cell["by_event"].get("go", 0) > 0,
                guard.get("any_go_ever"),
                guard.get("positive_reaction"),
                guard.get("applied_recently"))):
            protected_count += 1
            continue
        decision = demotion_decision(cell, today=today, protected=guard)
        if decision and decision["action"] == "silent":
            silenced.append({"company": name, "instrument": "market_tracker",
                             **{k: v for k, v in decision.items()
                                if k != "action"}})
        elif decision:
            # tracker entries are the principal's list: report only, always
            demotions.append({"company": name, "instrument": "market_tracker",
                              "report_only": True,
                              **{k: v for k, v in decision.items()
                                 if k != "action"}})

    result = {"principal": principal, "at": at, "applied": apply,
              "promotions": promotions, "demotions": demotions,
              "silent": silenced, "protected": protected_count,
              "skips": skips,
              "listed": {"company_pages": len(yaml_list),
                         "tracker": len(tracker_names)},
              "list_load_errors": list_load_errors,
              "cube_companies": len(
                  {c for (c, n) in cube if n == principal})}

    if apply:
        applied = []
        skipped: list[dict] = []
        from sources.company_pages import add_entry
        for promo in promotions:
            board = _best_url(cube, promo["company"], principal, repo_root=repo_root)
            if not board:
                # No board means the entry could never find work — the exact
                # failure the tracker enrichment fixed for market lists. Report
                # it rather than write a URL we know does not work.
                skipped.append({"company": promo["company"],
                                "why": "no board url (advert-only or mail-derived evidence)"})
                logger.warning(
                    "%s: promotion skipped, no board url for %s", principal, promo["company"])
                continue
            added = add_entry(
                principal, name=promo["company"],
                careers_url=board,
                reason=f"cube-promotion tier {promo['tier']} "
                       f"({promo['reason']})",
                cadence_days=promo["cadence_days"])
            applied.append({"company": promo["company"], "added": added, "board": board})
        result["applied_promotions"] = applied
        result["skipped_promotions"] = skipped
        # demotion writes on company_pages.yaml (cadence lengthen) are a
        # later step: v1 applies promotions only; demotions stay proposals
        # until a principal has reviewed one round of reports.

        # narrative mirror (D22, PLAN.md T2.4): every landed decision gets
        # its one-liner in the org's memory — the keel's shadow, note-if-exists
        try:
            import mem as _mem
            by_company = {p["company"]: p for p in promotions}
            pairs = [(a["company"],
                      "keel: promoted tier %s (%s)" % (
                          by_company.get(a["company"], {}).get("tier", "?"),
                          by_company.get(a["company"], {}).get("reason", "")))
                     for a in applied]
            _mem.note_case_lines(state_root / "dataset", pairs)
        except Exception:
            pass  # decisions ledger is the authority; the mirror is best-effort

    _log_decisions(state_root, result)
    return result


# ── boards, not adverts (2026-10-01) ─────────────────────────────────────────
# The rule and its helpers now live in `sources/company_pages.py` (`resolve_board_url`)
# alongside `add_entry`, because both entry-creating paths need it: promotions here
# and harvested leads in `lead_resolution`. This module used to carry its own copy —
# a second implementation of the same rule, which is how rules drift apart.
from sources.company_pages import (  # noqa: E402
    board_from_advert as _board_from_advert,
    is_mail_url as _is_mail_url,
    lookup_board as _lookup_board,
    verified_boards as _verified_boards_central,
)


def _verified_boards(repo_root: str) -> dict:
    """Board index for a *repo* root (the central helper wants the profiles dir)."""
    return _verified_boards_central(str(Path(repo_root) / "profiles"))


def _best_url(cube: dict, company: str, principal: str,
              *, repo_root: Path | None = None) -> str | None:
    """A board to watch — never an advert, never a mail link. None when unknown."""
    if repo_root is not None:
        known = _lookup_board(_verified_boards(str(repo_root)), company)
        if known:
            return known
    for line in _company_rows(cube, company, principal):
        url = line.get("url") or ""
        if url.startswith("http") and not _is_mail_url(url):
            board = _board_from_advert(url)
            if board:
                return board
    return None


def _company_rows(cube: dict, company: str, principal: str) -> list[dict]:
    """The cube aggregates; the raw rows live in the jsonl — read them."""
    path = Path(_dataset_dir_hint(principal)) / "company_events.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in jsonl_io.lines(path):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("principal") == principal and row.get("company") == company:
            rows.append(row)
    return rows


def _dataset_dir_hint(principal: str) -> str:
    import config
    return str(config.STATE_DIR / principal / "dataset")


def _log_decisions(state_root: Path, result: dict) -> None:
    path = state_root / "dataset" / DECISIONS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "decision_id": uuid.uuid4().hex,
            "at": result["at"], "principal": result["principal"],
            "applied": result["applied"],
            "promotions": result["promotions"],
            "demotions": result["demotions"],
            "silent": result["silent"],
        }, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def run_audit(state_root: Path, repo_root: Path, *,
               fresh: dict | None = None) -> dict:
    """One principal's applied-vs-pending reconciliation.

    Answers the questions that took an eight-step investigation on
    2026-10-02: what did previous --apply runs actually write, what does a
    fresh computation propose now, are the lists loadable, and is this
    principal on the same keel as the others? Read-only apart from the
    standard decision-log row ``run_policy`` writes.
    """
    principal = state_root.name
    fresh = fresh if fresh is not None else run_policy(
        state_root, repo_root)

    # run history from the decision log
    history = []
    log_path = state_root / "dataset" / DECISIONS_NAME
    if log_path.is_file():
        for line in jsonl_io.lines(log_path):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            history.append({
                "at": row.get("at", ""), "applied": row.get("applied"),
                "promotions": len(row.get("promotions") or []),
                "demotions": len(row.get("demotions") or []),
                "silent": len(row.get("silent") or []),
            })
    history.sort(key=lambda r: r["at"])

    # what --apply actually wrote: the cube-promotion entries in the yaml
    yaml_path = repo_root / "profiles" / principal / "company_pages.yaml"
    applied_names = []
    if yaml_path.is_file():
        for entry in _load_yaml_list(yaml_path):
            reason = str(entry.get("reason", ""))
            if reason.startswith("cube-promotion"):
                applied_names.append(entry.get("name", "?"))

    # reconciliation: fresh proposals vs already-applied. A caught-up
    # company never appears in fresh proposals (run_policy skips listed
    # companies), so "already" is applied-minus-pending, not the
    # intersection — the intersection is always empty by construction.
    pending = [p["company"] for p in fresh.get("promotions", [])]
    still_pending = [c for c in pending if c not in applied_names]
    already = [c for c in applied_names if c not in pending]

    return {
        "principal": principal,
        "audit_at": _utc_now_iso(),
        "run_history": history,
        "runs_total": len(history),
        "applied_runs": sum(1 for r in history if r["applied"]),
        "applied_entries": applied_names,
        "pending_now": still_pending,
        "already_caught_up": already,
        "listed": fresh.get("listed", {}),
        "list_load_errors": fresh.get("list_load_errors", []),
        "keel": {
            "watched": fresh.get("listed", {}).get("company_pages", 0),
            "tracked": fresh.get("listed", {}).get("tracker", 0),
            "protected": fresh.get("protected", 0),
            "pending": len(still_pending),
        },
    }


def _render_audit(audits: list[dict]) -> str:
    """The consolidated cross-principal audit view."""
    lines = ["# promote/demote — AUDIT — "
             + (audits[0]["audit_at"][:10] if audits else "?"), "",
             "Applied-vs-pending reconciliation across runs, lists and a "
             "fresh computation. Read-only.", ""]
    lines += ["## Keel comparison", "",
              "| P | watched | tracked | protected | pending | applied "
              "entries | runs (applied/total) |", "|---|---|---|---|---|"
              "---|---|"]
    for a in audits:
        lines.append(
            f"| {a['principal']} | {a['keel']['watched']} | "
            f"{a['keel']['tracked']} | {a['keel']['protected']} | "
            f"{a['keel']['pending']} | {len(a['applied_entries'])} | "
            f"{a['applied_runs']}/{a['runs_total']} |")
    lines += ["", "## Per principal", ""]
    for a in audits:
        lines += [f"### {a['principal']}", ""]
        if a.get("list_load_errors"):
            lines.append("**⚠ LIST LOAD ERRORS — counts UNTRUSTWORTHY**")
            for err in a["list_load_errors"]:
                lines.append(f"- {err}")
            lines.append("")
        if a["applied_entries"]:
            lines.append(f"**Applied so far ({len(a['applied_entries'])}):** "
                         + ", ".join(a["applied_entries"][:12])
                         + ("…" if len(a["applied_entries"]) > 12 else ""))
        else:
            lines.append("**Applied so far:** none — every round has been "
                         "report-only")
        lines.append("")
        if a["pending_now"]:
            lines.append(f"**Pending now ({len(a['pending_now'])}) — a "
                         "fresh --apply would write:** "
                         + ", ".join(a["pending_now"][:12])
                         + ("…" if len(a["pending_now"]) > 12 else ""))
        else:
            lines.append("**Pending now:** none — caught up with the "
                         "evidence")
        lines.append("")
        for run in a["run_history"][-3:]:
            lines.append(f"- {run['at'][:16]} "
                         f"{'APPLY' if run['applied'] else 'report'}: "
                         f"{run['promotions']} promos, "
                         f"{run['demotions']} demos, "
                         f"{run['silent']} silent")
        lines.append("")
    return "\n".join(lines) + "\n"


def _render(result: dict) -> str:
    lines = [f"# promote/demote — {result['principal']} — {result['at'][:10]} "
             f"({'APPLY' if result['applied'] else 'REPORT-ONLY'})", ""]
    lines.append(f"listed: {result['listed']['company_pages']} watched · "
                 f"{result['listed']['tracker']} tracked · "
                 f"{result['cube_companies']} cube companies · "
                 f"{result['protected']} protected")
    lines += ["", f"## Promotions ({len(result['promotions'])}) — cube "
              "evidence, not yet listed", ""]
    for p in result["promotions"]:
        lines.append(f"- **{p['company']}** (tier {p['tier']}, cadence "
                     f"{p['cadence_days']}d): {p['reason']}")
    if not result["promotions"]:
        lines.append("_None clear the conservative bar._")
    if result.get("list_load_errors"):
        lines += ["", "## ⚠ LIST LOAD ERRORS — counts above are UNTRUSTWORTHY",
                  ""]
        for err in result["list_load_errors"]:
            lines.append(f"- {err}")
    lines += ["", f"## Demotion candidates ({len(result['demotions'])}) — "
              "listed, enough sightings, zero strong evidence", ""]
    for d in result["demotions"]:
        tag = " _[report-only: tracker]_" if d.get("report_only") else ""
        lines.append(f"- **{d['company']}** ({d['instrument']}{tag}): "
                     f"{d['reason']}")
    if not result["demotions"]:
        lines.append("_None._")
    lines += ["", f"## Silent listings ({len(result['silent'])}) — listed, "
              "no cube events at all (broken-listing suspicion)", ""]
    for s in result["silent"][:12]:
        lines.append(f"- {s['company']} ({s['instrument']})")
    if len(result["silent"]) > 12:
        lines.append(f"- … +{len(result['silent']) - 12} more")
    if not result["silent"]:
        lines.append("_None._")
    return "\n".join(lines) + "\n"


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Market-list promotion/demotion policy (docs/37 §4, v1)")
    parser.add_argument("--profile", default="all")
    parser.add_argument("--apply", action="store_true",
                        help="perform promotion writes (default: report-only)")
    parser.add_argument("--audit", action="store_true",
                        help="consolidated audit: run history, applied-vs-"
                             "pending reconciliation, keel comparison")
    parser.add_argument("--max-promote", type=int, default=MAX_PROMOTE)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    import config

    repo_root = Path(config.LOVEWORK_ROOT)
    principals = (
        sorted(p.name for p in config.STATE_DIR.iterdir()
               if p.is_dir() and (p / "dataset").is_dir()
               and re.fullmatch(r"[a-z][a-z0-9-]*", p.name))
        if args.profile == "all" else [args.profile])
    results = []
    for principal in principals:
        state_root = config.STATE_DIR / principal
        if not state_root.is_dir():
            print(f"no state dir: {state_root}", file=sys.stderr)
            return 66
        from writer_boundary import WriterBusy, standalone_writer_guard
        try:
            with standalone_writer_guard(
                    principal, mode="promote-demote", state_root=state_root,
                    run_id=f"pd-{uuid.uuid4().hex[:8]}"):
                fresh = run_policy(
                    state_root, repo_root, max_promote=args.max_promote,
                    apply=args.apply)
                results.append(run_audit(state_root, repo_root,
                                         fresh=fresh) if args.audit
                               else fresh)
        except WriterBusy:
            print(f"[promote-demote] {principal}: deferred — writer busy")
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    elif args.audit:
        print(_render_audit(results))
        return 0
    else:
        for r in results:
            print(_render(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
