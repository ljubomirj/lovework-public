#!/usr/bin/env python3
"""The Shoulder W4 — the reflection loop (docs/35; discipline docs/18).

The monthly close: reactions → grouped patterns → candidate propositions →
a reflect report the principal reads. The rules that make it honest:

* **No new independent evidence ⇒ no model call and no report.** The loop
  wakes only on reactions the previous run has not seen (docs/18: "No new
  independent evidence means no model reflection call"). A quiet month
  costs nothing.
* **Deterministic core, LLM drafting.** Grouping, corroboration counting,
  contradiction scanning and the disagreement query are deterministic; an
  optional LLM pass only words the claims. On any failure the report uses
  the mechanical wording — never invented.
* **Corroboration, not repetition.** A candidate needs ≥2 *distinct*
  reactions (different opportunities) to be proposed; a single expression
  becomes a hypothesis (docs/18's grain rule).
* **The disagreement query runs first** (docs/18's "first query"):
  recommendations the principal engaged with negatively — review
  candidates, never automatically errors.
* Bounded: at most ``MAX_CANDIDATES`` patterns per run, at most
  ``MAX_PROPOSITIONS`` new propositions, remainder counted.

Usage::

    ../venv/bin/python3 reflect.py --profile lj [--dry-run] [--no-llm] [--json]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from propositions import propose  # noqa: E402
from reactions import _read_jsonl  # noqa: E402

logger = logging.getLogger("reflect")

SCHEMA_VERSION = 1
CURSOR_NAME = "reflect-cursor.json"
REFLECT_DIR = "reflect"
MAX_CANDIDATES = 8
MAX_PROPOSITIONS = 4

_POSITIVE = {"excited", "interested"}
_NEGATIVE = {"bored", "repelled"}
_NEGATIVE_INTENTS = {"ignore", "defer"}

# The prompt lives in `prompts.py` — one place for all of them (2026-10-15).
from prompts import REFLECT_PROMPT as _PROMPT


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stimulus_org(stimulus: str | None) -> str:
    """Best-effort org name from a stimulus string ('brief item 2: Dex — …')."""
    if not stimulus:
        return "?"
    text = re.sub(r"^(brief|report|event)[^:]*:\s*", "", stimulus, flags=re.I)
    return (text.split("—")[0].strip() or "?")[:60]


def _orgs_of(reaction: dict) -> str:
    return _stimulus_org(reaction.get("stimulus"))


def group_reactions(rows: list[dict]) -> list[dict]:
    """Deterministic grouping: (reaction token, intent) → one pattern."""
    buckets: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    for row in rows:
        buckets[(row.get("reaction", "?"), row.get("action_intent"))]\
            .append(row)
    # negative and contested patterns first (they are the review candidates)
    def rank(item: tuple[tuple[str, str | None], list[dict]]) -> tuple:
        token, _ = item[0]
        return (0 if token in _NEGATIVE else
                1 if token == "unsure" else
                2 if token == "indifferent" else 3)
    ordered = sorted(buckets.items(), key=lambda kv: (rank(kv), -len(kv[1])))
    return [{"reaction": token, "intent": intent, "rows": rows_}
            for (token, intent), rows_ in ordered][:MAX_CANDIDATES]


def scan_contradictions(rows: list[dict], pattern: dict) -> list[dict]:
    """Reactions in the opposite valence touching the same orgs."""
    contradictions = []
    token = pattern["reaction"]
    if token not in _POSITIVE | _NEGATIVE:
        return contradictions
    opposite = _NEGATIVE if token in _POSITIVE else _POSITIVE
    orgs = {_orgs_of(r).lower() for r in pattern["rows"]} - {"?", ""}
    for row in rows:
        if row.get("reaction") in opposite and \
                _orgs_of(row).lower() in orgs:
            contradictions.append({
                "ref": row.get("reaction_id"),
                "note": f"{row.get('reaction')} about {_orgs_of(row)}",
            })
    return contradictions


def disagreement_candidates(rows: list[dict]) -> list[dict]:
    """docs/18's first query: negative reactions / ignore-intents to things
    the system surfaced — review candidates, never automatic errors."""
    out = []
    for row in rows:
        if (row.get("reaction") in _NEGATIVE or
                row.get("action_intent") in _NEGATIVE_INTENTS):
            out.append({
                "reaction_id": row.get("reaction_id"),
                "reaction": row.get("reaction"),
                "intent": row.get("action_intent"),
                "stimulus": row.get("stimulus"),
                "verbatim": row.get("verbatim"),
            })
    return out


def _mechanical_claim(pattern: dict, principal: str) -> str:
    orgs = sorted({_orgs_of(r) for r in pattern["rows"]})[:3]
    org_txt = ", ".join(orgs) if orgs else "unattributed stimuli"
    intent = (f" and intents to {pattern['intent']}"
              if pattern.get("intent") else "")
    return (f"{principal} expressed '{pattern['reaction']}' about "
            f"{len(pattern['rows'])} surfaced item(s) ({org_txt}){intent}.")


def _llm_claims(principal: str, patterns: list[dict], llm) -> list[dict] | None:
    if llm is None:
        return None
    lines = []
    for p in patterns:
        verbatims = " | ".join(
            (r.get("verbatim") or "").strip()[:90]
            for r in p["rows"][:4] if r.get("verbatim"))
        lines.append(f"- {p['reaction']} / {p['intent'] or '-'} × "
                     f"{len(p['rows'])}: {verbatims or '(no verbatim)'}")
    try:
        raw = llm.chat(
            [{"role": "user", "content": _PROMPT.format(
                principal=principal, groups="\n".join(lines),
                max_n=MAX_PROPOSITIONS)}],
            response_format={"type": "json_object"},
            temperature=0.2, context="watch-reflect")
    except Exception as exc:                        # noqa: BLE001
        logger.warning("reflect LLM pass failed: %s", exc)
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    items = payload.get("propositions")
    if not isinstance(items, list) or not items:
        return None
    clean = []
    for item in items[:MAX_PROPOSITIONS]:
        if (isinstance(item, dict) and item.get("claim")
                and item.get("category") and item.get("scope_domain")):
            clean.append({"claim": str(item["claim"])[:240],
                          "category": str(item["category"]),
                          "scope_domain": str(item["scope_domain"])[:120]})
    return clean or None


def run_reflect(state_root: Path, *, now: datetime | None = None,
                llm=None, dry_run: bool = False) -> dict:
    """One reflection pass: cursor → group → propose → report."""
    now = now or datetime.now(timezone.utc)
    principal = state_root.name
    at = now.isoformat(timespec="seconds")

    cursor_path = state_root / "cache" / CURSOR_NAME
    from_at = None
    if cursor_path.is_file():
        try:
            from_at = json.loads(
                cursor_path.read_text(encoding="utf-8")).get("last_reflect_at")
        except (json.JSONDecodeError, OSError):
            from_at = None

    all_rows = _read_jsonl(state_root / "dataset" / "reactions.jsonl")
    fresh = [r for r in all_rows
             if not from_at or r.get("recorded_at", "") > from_at]
    result = {"principal": principal, "at": at, "dry_run": dry_run,
              "new_reactions": len(fresh),
              "propositions_created": 0, "candidates_skipped": 0,
              "report_path": None, "quiet": False}

    if not fresh:
        result["quiet"] = True
        result["reason"] = ("no new reactions since the last reflect — "
                            "no model call, no report (docs/18)")
        return result

    patterns = group_reactions(fresh)
    disagreements = disagreement_candidates(fresh)

    # claims: LLM words if it can, mechanical wording otherwise
    claims = _llm_claims(principal, patterns, llm)
    source = "llm" if claims else "deterministic"
    if not claims:
        claims = [{"claim": _mechanical_claim(p, principal),
                   "category": "taste",
                   "scope_domain": "roles surfaced in briefs"} for p in patterns]

    created = []
    skipped = 0
    for pattern, claim_spec in zip(patterns[:MAX_PROPOSITIONS],
                                   claims[:MAX_PROPOSITIONS]):
        try:
            row = propose(
                state_root,
                claim=claim_spec["claim"],
                category=claim_spec["category"],
                scope={"domain": claim_spec["scope_domain"],
                       "condition": f"reaction pattern '{pattern['reaction']}'"},
                evidence_refs=[r["reaction_id"] for r in pattern["rows"]
                               if r.get("reaction_id")],
                contradictions=scan_contradictions(all_rows, pattern),
                confidence="low",
                proposed_by=f"reflect {at}" + (" (llm)" if source == "llm"
                                                else ""),
                now=now)
            created.append(row)
        except ValueError as exc:
            skipped += 1
            logger.info("candidate skipped: %s", exc)
    if len(patterns) > MAX_PROPOSITIONS:
        result["candidates_skipped"] += len(patterns) - MAX_PROPOSITIONS

    result["propositions_created"] = len(created)
    result["claim_source"] = source

    report_lines = [
        f"# Reflect — {principal} — {at[:10]}",
        "",
        f"_{len(fresh)} new reaction(s) since the last reflect · "
        f"{len(created)} proposition(s) created ({source} wording) · "
        f"{skipped} candidate(s) skipped._",
        "",
        "## Disagreement candidates (docs/18's first query)",
        "",
    ]
    if disagreements:
        for d in disagreements[:10]:
            report_lines.append(
                f"- {d['reaction']}"
                + (f" / intent {d['intent']}" if d.get("intent") else "")
                + f" — {d['stimulus'] or '?'}"
                + (f" · _\"{d['verbatim']}\"" if d.get("verbatim") else ""))
        report_lines.append(
            "\n_Review candidates, not errors: timing, energy or a different "
            "goal may explain each one (docs/18)._")
    else:
        report_lines.append("_None — no negative or ignore-intent reactions._")

    report_lines += ["", "## Propositions created", ""]
    for row in created:
        report_lines.append(
            f"- **{row['proposition_id']}** ({row['status']}, support "
            f"{row['support']}, contradictions {len(row['contradictions'])}) "
            f"{row['claim']} _[{row['scope'].get('domain', '?')}]_")
    if not created:
        report_lines.append("_None — patterns did not clear the guards._")

    report_lines += ["", "## Open questions for the principal", ""]
    for row in created:
        if row["status"] == "hypothesis":
            report_lines.append(
                f"- {row['proposition_id']}: one expression only — "
                "corroborate or let it stand as the principal's own "
                "statement (docs/18's single-statement rule).")
    if not [r for r in created if r["status"] == "hypothesis"]:
        report_lines.append("_None this run._")

    report = "\n".join(report_lines) + "\n"
    result["report"] = report
    if dry_run:
        return result

    reflect_dir = state_root / REFLECT_DIR
    reflect_dir.mkdir(parents=True, exist_ok=True)
    path = reflect_dir / f"reflect-{at.replace(':', '').replace('+', '_')}.md"
    path.write_text(report, encoding="utf-8")
    result["report_path"] = str(path)

    tmp = cursor_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "last_reflect_at": at, "schema_version": SCHEMA_VERSION},
        indent=2, sort_keys=True) + "\n", encoding="utf-8")
    import os
    os.replace(tmp, cursor_path)
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="The Shoulder W4 — the reflection loop")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
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
        except Exception as exc:                    # noqa: BLE001
            logger.warning("reflect will be deterministic: %s", exc)

    from writer_boundary import WriterBusy, standalone_writer_guard
    try:
        with standalone_writer_guard(
                args.profile, mode="reflect", state_root=state_root,
                run_id=f"reflect-{uuid.uuid4().hex[:8]}"):
            result = run_reflect(state_root, llm=llm,
                                 dry_run=args.dry_run)
    except WriterBusy:
        print("[reflect] deferred — writer busy")
        return 75

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result.get("quiet"):
        print(f"[reflect] {args.profile}: {result['reason']}")
    else:
        print(result.get("report", ""))
        if result.get("report_path"):
            print(f"[reflect] written: {result['report_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
