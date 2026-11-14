#!/usr/bin/env python3
"""The Shoulder W3 — reactions capture (docs/35, schema from docs/18).

The ch18 stream, finally given a writer. A **reaction** is the principal's
expressed response to a specific recommendation or event — the taste signal
the whole learning loop has been starved for. This module owns the ledger
and three capture paths:

1. ``record`` — the CLI/MCP primitive: one typed row per call, closed
   vocabularies enforced, the ch18 negative-space rules enforced
   (a rejection is not a reaction; silence is never ``indifferent``).
2. ``classify`` — map free text (an email reply, a chat line) onto the
   closed vocabularies: deterministic keyword/emoji rules first; an
   optional LLM pass for the rest; honest ``unsure`` when neither can.
3. ``poll`` — find replies to sent briefs in the principal's mailbox
   (query mode, ``mark_read`` NEVER set — the J&J precedent), classify,
   append one row per reply message, idempotent by message id.

Discipline (docs/18 §Stream 1, binding):

* **A scored reaction needs an unambiguous reference.** Rows carry either
  an ``assessment_id`` (the gold path) or an ``event_id``/``stimulus``
  reference (brief items — ch18's "conversational reference" case) with
  ``attribution: "event"``; never silently attach to the latest assessment.
* **``expression_kind`` separates explicit from inferred.** Behaviour-derived
  rows (e.g. an apply outcome) may be captured as ``inferred`` hypotheses —
  never counted as principal-expressed taste.
* **One message, one row, ever.** ``source_message_id`` is the idempotence
  key; poll re-runs are free.
* The ledger is append-only at ``state/<p>/dataset/reactions.jsonl``; the
  writer lease is taken for writes exactly like every other direct writer.

Usage::

    ../venv/bin/python3 reactions.py record --profile lj \\
        --event <event_id> --reaction interested --note "worth a look"
    ../venv/bin/python3 reactions.py poll --profile lj [--dry-run]
    ../venv/bin/python3 reactions.py stats --profile lj
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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = logging.getLogger("reactions")

SCHEMA_VERSION = 1
LEDGER_NAME = "reactions.jsonl"
RECEIPTS_NAME = "brief-receipts.jsonl"

# ── ch18 closed vocabularies ─────────────────────────────────────────────

REACTIONS = ("excited", "interested", "indifferent", "bored", "repelled",
             "unsure")
ACTION_INTENTS = ("apply", "research", "discuss", "defer", "ignore", "contact")
REASONS = ("work", "mission", "people", "trajectory", "money", "location",
           "reach", "timing", "culture", "ideology")
EXPRESSION_KINDS = ("explicit", "inferred")
ENERGIES = ("high", "medium", "low")

# Deterministic classification: keyword/emoji → (reaction, action_intent).
# Ordered: first match wins. Deliberately coarse — anything the table cannot
# resolve goes to the LLM (if available) or `unsure` (honest, ch18: a
# valuable signal, not a non-signal).
_CLASSIFY_RULES: tuple[tuple[str, str, str | None], ...] = (
    (r"🔥|💯|❤️|love it|exactly what i want|perfect|!!", "excited", None),
    (r"😀|😃|👍|😄|nice|good one|interesting|worth a look|👍🏽", "interested",
     None),
    (r"😐|shrug|\bmeh\b|fine|\bok(?:ay)?\b", "indifferent", None),
    (r"😴|🥱|boring|dull|meh+", "bored", None),
    (r"🤢|👎|no way|hard pass|never|awful|terrible|repell", "repelled", None),
    (r"\?|not sure|maybe|hm+|hmm", "unsure", None),
    (r"apply|applying|send it|do it|go for it", None, "apply"),
    (r"look in?to|research|check them out", None, "research"),
    (r"later|not now|another time", None, "defer"),
    (r"skip|ignore|pass( on)?\b", None, "ignore"),
)

_POLL_QUERY_WINDOW_DAYS = 7


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _append_ledger(state_root: Path, row: dict) -> Path:
    path = state_root / "dataset" / LEDGER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return path


# ── record ───────────────────────────────────────────────────────────────


def record_reaction(
    state_root: Path,
    *,
    reaction: str,
    assessment_id: str | None = None,
    event_id: str | None = None,
    stimulus: str | None = None,
    action_intent: str | None = None,
    primary_reason: str | None = None,
    secondary_reasons: list[str] | None = None,
    expression_kind: str = "explicit",
    energy: str | None = None,
    verbatim: str | None = None,
    source: str = "cli",
    source_message_id: str | None = None,
    recorded_by: str = "principal",
    attribution: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Validate against the ch18 vocabularies and append one row.

    Raises ``ValueError`` on any violation — the ledger never accepts an
    out-of-vocabulary value or a row with no reference at all.
    """
    now = now or datetime.now(timezone.utc)
    if reaction not in REACTIONS:
        raise ValueError(f"reaction must be one of {REACTIONS}, got {reaction!r}")
    if action_intent is not None and action_intent not in ACTION_INTENTS:
        raise ValueError(f"action_intent must be one of {ACTION_INTENTS}")
    for reason in filter(None, [primary_reason, *(secondary_reasons or [])]):
        if reason not in REASONS:
            raise ValueError(f"reason {reason!r} not in {REASONS}")
    if len(list(filter(None, [primary_reason, *(secondary_reasons or [])]))) > 3:
        raise ValueError("at most one primary + two secondary reasons (ch18)")
    if expression_kind not in EXPRESSION_KINDS:
        raise ValueError(f"expression_kind must be one of {EXPRESSION_KINDS}")
    if energy is not None and energy not in ENERGIES:
        raise ValueError(f"energy must be one of {ENERGIES}")

    if not assessment_id and not event_id and not stimulus:
        raise ValueError(
            "a reaction needs a reference: assessment_id, event_id or "
            "stimulus (ch18: never silently attach to the latest assessment)")
    if assessment_id and not attribution:
        attribution = "assessment"
    elif event_id and not attribution:
        attribution = "event"

    # Negative-space guard: an inferred apply (from behaviour) may never be
    # auto-upgraded to excited by the caller (ch18: applied ≠ excited).
    if expression_kind == "inferred" and reaction == "excited":
        raise ValueError(
            "an inferred reaction may not claim 'excited' (ch18: applied is "
            "not necessarily excited)")

    row = {
        "schema_version": SCHEMA_VERSION,
        "event_type": "reaction",
        "reaction_id": uuid.uuid4().hex,
        "profile_name": state_root.name,
        "assessment_id": assessment_id,
        "event_id": event_id,
        "stimulus": stimulus,
        "attribution": attribution,
        "reaction": reaction,
        "action_intent": action_intent,
        "primary_reason": primary_reason,
        "secondary_reasons": secondary_reasons or [],
        "expression_kind": expression_kind,
        "energy": energy,
        "verbatim": verbatim,
        "recorded_at": now.isoformat(timespec="seconds"),
        "source": source,
        "source_message_id": source_message_id,
        "recorded_by": recorded_by,
    }
    _append_ledger(state_root, row)
    return row


# ── classify ─────────────────────────────────────────────────────────────


def classify_text(text: str, *, llm=None) -> dict:
    """Free text → closed vocabulary. Deterministic rules first; an optional
    LLM pass for what the rules cannot settle; honest ``unsure`` otherwise.
    The verbatim is always preserved alongside — classification is an aid,
    never a replacement for the words."""
    text_l = (text or "").strip()
    if not text_l:
        return {"reaction": "unsure", "action_intent": None,
                "classified_by": "empty", "verbatim": text}

    reaction = intent = None
    for pattern, reaction_hit, intent_hit in _CLASSIFY_RULES:
        if re.search(pattern, text_l, re.IGNORECASE):
            reaction = reaction or reaction_hit
            intent = intent or intent_hit
            if reaction and intent:
                break
    if reaction is not None:
        return {"reaction": reaction, "action_intent": intent,
                "classified_by": "rules", "verbatim": text}
    if intent is not None:
        # an intent matched but no reaction word did: the honest reaction
        # is unsure; the intent is still signal (ch18: unsure is valuable)
        return {"reaction": "unsure", "action_intent": intent,
                "classified_by": "rules", "verbatim": text}

    if llm is not None:
        try:
            prompt = (
                "Classify this reply from a person to their agent's daily "
                "brief into exactly one reaction and optionally an intent.\n"
                f"reactions: {REACTIONS}\nintents: {ACTION_INTENTS}\n"
                'Output STRICT JSON: {"reaction": str, "action_intent": '
                "str|null}\nIf genuinely unclear, reaction is \"unsure\".\n"
                f"Reply text: {text_l[:500]}")
            raw = llm.chat([{"role": "user", "content": prompt}],
                           response_format={"type": "json_object"},
                           temperature=0.0, context="watch-reaction")
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            payload = json.loads(match.group(0)) if match else {}
            reaction = payload.get("reaction")
            intent = payload.get("action_intent")
            if reaction in REACTIONS:
                return {"reaction": reaction,
                        "action_intent": intent if intent in ACTION_INTENTS
                        else None,
                        "classified_by": "llm", "verbatim": text}
        except Exception as exc:                    # noqa: BLE001
            logger.warning("reaction LLM classify failed: %s", exc)
    return {"reaction": "unsure", "action_intent": None,
            "classified_by": "unresolved", "verbatim": text}


# ── poll: replies to sent briefs ─────────────────────────────────────────


def _brief_receipts(state_root: Path) -> list[dict]:
    return _read_jsonl(state_root / "cache" / RECEIPTS_NAME)


def record_brief_receipt(state_root: Path, *, at: str, path: str,
                         message_id: str, subject: str) -> None:
    """brief.py --send calls this so poll can match replies to threads."""
    target = state_root / "cache" / RECEIPTS_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": at, "path": path,
                             "message_id": message_id, "subject": subject},
                            sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def poll_replies(state_root: Path, *, now: datetime | None = None,
                 llm=None, dry_run: bool = False,
                 search=None) -> dict:
    """Find replies to sent briefs and record them as reactions.

    ``search`` is injectable for tests (the gmail_accessor search callable);
    production resolves it lazily. Query mode only — nothing is ever marked
    read (the J&J precedent). Idempotent by the reply's message id.
    """
    now = now or datetime.now(timezone.utc)
    receipts = [r for r in _brief_receipts(state_root) if r.get("message_id")]
    result = {"receipts": len(receipts), "replies_seen": 0,
              "reactions_recorded": 0, "already_recorded": 0,
              "gmail": "unavailable", "dry_run": dry_run}
    if not receipts:
        result["gmail"] = "no-brief-receipts"
        return result

    known_ids = {row.get("source_message_id")
                 for row in _read_jsonl(state_root / "dataset" / LEDGER_NAME)}

    if search is None:
        def search(query, max_results):
            from gmail_accessor import run_gapi
            return run_gapi("gmail", "search", query, "--max",
                            str(max_results)) or []

    seen: dict[str, dict] = {}
    for receipt in receipts:
        subject = receipt.get("subject", "")
        # Replies usually share the subject with an Re: prefix; search the
        # thread by subject fragment (message-id threading is not exposed
        # by the search interface, so subject match is the pragmatic key).
        fragment = re.sub(r"^(Re|Fwd):\s*", "", subject)[:60].strip()
        if not fragment:
            continue
        query = (f'subject:"({fragment})" newer_than:{_POLL_QUERY_WINDOW_DAYS}d '
                 f'-from:me')
        try:
            messages = search(query, 20)
        except Exception as exc:                    # noqa: BLE001
            logger.warning("reaction poll search failed: %s", exc)
            continue
        if messages == []:
            result["gmail"] = "ok"
            continue
        if not messages:
            continue                                # Gmail unreachable
        result["gmail"] = "ok"
        for msg in messages if isinstance(messages, list) else []:
            mid = str(msg.get("id", "")) or str(msg.get("message_id", ""))
            if not mid:
                body = msg.get("body") or msg.get("snippet", "")
                mid = "hash:" + uuid.uuid5(uuid.NAMESPACE_URL,
                                           str(body)).hex
            if mid in known_ids or mid in seen:
                continue
            seen[mid] = msg
    result["replies_seen"] = len(seen)

    for mid, msg in sorted(seen.items()):
        body = (msg.get("body") or msg.get("snippet")
                or msg.get("text") or "").strip()
        if not body:
            continue
        verdict = classify_text(body, llm=llm)
        result["reactions_recorded"] += 1
        if dry_run:
            continue
        record_reaction(
            state_root,
            reaction=verdict["reaction"],
            event_id=None,
            stimulus=f"brief reply: {str(msg.get('subject', ''))[:80]}",
            action_intent=verdict.get("action_intent"),
            expression_kind="explicit",
            verbatim=verdict.get("verbatim"),
            source="email-reply",
            source_message_id=mid,
            recorded_by="principal",
            attribution="stimulus",
            now=now,
        )
    return result


def reaction_stats(state_root: Path) -> dict:
    rows = _read_jsonl(state_root / "dataset" / LEDGER_NAME)
    by_reaction: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in rows:
        by_reaction[row.get("reaction", "?")] = \
            by_reaction.get(row.get("reaction", "?"), 0) + 1
        by_source[row.get("source", "?")] = \
            by_source.get(row.get("source", "?"), 0) + 1
    return {"total": len(rows), "by_reaction": dict(sorted(by_reaction.items())),
            "by_source": dict(sorted(by_source.items())),
            "explicit": sum(1 for r in rows
                            if r.get("expression_kind") == "explicit")}


# ── CLI ──────────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The Shoulder W3 — reactions")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record")
    p.add_argument("--profile", required=True)
    p.add_argument("--reaction", required=True, choices=REACTIONS)
    p.add_argument("--assessment-id")
    p.add_argument("--event-id")
    p.add_argument("--stimulus")
    p.add_argument("--intent", choices=ACTION_INTENTS)
    p.add_argument("--reason", help="primary reason token")
    p.add_argument("--secondary", nargs="*", choices=REASONS)
    p.add_argument("--energy", choices=ENERGIES)
    p.add_argument("--note", help="verbatim text, preserved raw")
    p.add_argument("--inferred", action="store_true")

    p = sub.add_parser("poll")
    p.add_argument("--profile", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-llm", action="store_true")

    p = sub.add_parser("stats")
    p.add_argument("--profile", required=True)

    args = parser.parse_args(argv)
    import config

    config.set_active_profile(args.profile)
    state_root = config.STATE_DIR / args.profile
    if not state_root.is_dir():
        print(f"no state dir: {state_root}", file=sys.stderr)
        return 66

    if args.command == "record":
        from writer_boundary import WriterBusy, standalone_writer_guard
        try:
            with standalone_writer_guard(
                    args.profile, mode="reaction", state_root=state_root,
                    run_id=f"reaction-{uuid.uuid4().hex[:8]}"):
                row = record_reaction(
                    state_root, reaction=args.reaction,
                    assessment_id=args.assessment_id,
                    event_id=args.event_id, stimulus=args.stimulus,
                    action_intent=args.intent,
                    primary_reason=args.reason,
                    secondary_reasons=args.secondary,
                    expression_kind="inferred" if args.inferred else "explicit",
                    energy=args.energy, verbatim=args.note)
        except WriterBusy:
            print("[reaction] deferred — writer busy")
            return 75
        print(json.dumps(row, indent=2, sort_keys=True))
        return 0

    if args.command == "poll":
        llm = None
        if not args.no_llm:
            try:
                from llm_client import LLMClient
                llm = LLMClient()
            except Exception as exc:                # noqa: BLE001
                logger.warning("classify will be rules-only: %s", exc)
        from writer_boundary import WriterBusy, standalone_writer_guard
        try:
            with standalone_writer_guard(
                    args.profile, mode="reaction-poll", state_root=state_root,
                    run_id=f"reaction-poll-{uuid.uuid4().hex[:8]}"):
                result = poll_replies(state_root, llm=llm,
                                      dry_run=args.dry_run)
        except WriterBusy:
            print("[reaction-poll] deferred — writer busy")
            return 75
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "stats":
        print(json.dumps(reaction_stats(state_root), indent=2,
                         sort_keys=True))
        return 0
    return 64


if __name__ == "__main__":
    raise SystemExit(_main())
