#!/usr/bin/env python3
"""decide.py — what is due, and what should happen next, as a machine answer.

The tick's decider (ch42 §3.2, ch43). **Pure read**: no writes, no model calls,
no network, no LLM. It composes reads that already exist rather than adding a
second watcher — the ten watches in `watch.py`, the clock authority `DUE.md`,
the pack queue, the AS0 selectors, the proposition ledger — and returns one
ranked list.

The rule that shapes everything here is ch27 §7: *choose by expected improvement
to a principal decision.* So every item that the loop may act on **must name the
principal decision it could change**. An item that cannot name one is not loop
work; it is reported in its own `maintenance` section, where it can never
outrank a dated clock.

Outcome vocabulary (ch42 §3.1): a tick that finds nothing due is a real result,
not a failure. `due: 0` is the common case and the cheapest one.

    decide.py --profile lj              # ranked text
    decide.py --profile lj --json       # the payload
    decide.py --profile lj --status     # one receipt line, for a cron tick
    decide.py --profile lj --all        # include maintenance

Exit codes: 0 read succeeded (whatever the answer), 1 the read failed, 2 usage.
The *answer* is in the payload, never in the exit code — a tick that treats
"something is due" as an error would retry it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

#: The read ladder rung each item needs (docs/27 §3): 0 is a local read with no
#: inference, 4 is a model call, 5 is network acquisition. The decider itself
#: stays on rung 0 — it only *names* the rung the next step would need.
RUNG_LOCAL, RUNG_REASON, RUNG_REPLAY, RUNG_INFERENCE, RUNG_ACQUIRE = 0, 1, 2, 4, 5

#: Priority classes, highest first. A dated clock outranks a standing
#: commitment; a principal's pending decision outranks agent work; maintenance
#: is last and never blocks.
CLASS_ORDER = ("clock", "principal", "health", "work", "maintenance")

#: The kinds with a **designed** precedence, read right after the class.
#:
#: LJ, 2026-10-19: *"we must create them asap, and make that a rule: all GO-s should
#: have application packs … LoveWork must think at all times: how do I convert this
#: lead into an opening for the principal?"* A GO is a promise that the work is done,
#: so writing the pack's material outranks re-reading a score (`review_go`) and
#: filling an empty scaffold (`pack_pending`).
#:
#: Before this the order held by accident: `complete_pack` came first only because
#: "complete_pack" < "review_go" as strings — a rename would have silently reversed
#: the queue, and nothing would have noticed.
#: **GO packs before MAYBE packs** (LJ, 2026-10-20). Within a principal the decider orders
#: these; across principals the slot's tier ordering does (a GO for lj outranks a MAYBE for
#: vj), so a scarce hour is always spent on the adverts closest to the principal's desk.
KIND_ORDER = ("go_pack", "maybe_pack", "pack_pending", "review_go")

#: The daily envelope (ch27 P3, ch42 §5 P1): a *per-day* bound, because N ticks
#: times a per-run cap is unbounded. Unset means "not yet ruled", and the
#: decider says so rather than inventing a number.
ENVELOPE_VARS = ("LOVEWORK_LOOP_DAILY_CALLS", "LOVEWORK_LOOP_DAILY_MINUTES")


def _envelope() -> dict:
    out = {}
    for name in ENVELOPE_VARS:
        raw = (os.getenv(name) or "").strip()
        out[name] = int(raw) if raw.isdigit() else None
    out["set"] = any(v is not None for v in out.values())
    return out


#: Owner tokens that are not a person: a clock owned by one of these is the
#: agent's or the scheduler's to carry. Anything else in the Owner cell means a
#: human must act, so the item is the principal's.
_NON_HUMAN_OWNERS = ("agent", "engine", "cron", "hermes", "host-a", "host-b")


def _short(text: str, limit: int = 110) -> str:
    """One readable line from a DUE.md cell: markdown off, first clause kept."""
    value = re.sub(r"[*`]", "", text or "")
    value = re.sub(r"\s+", " ", value).strip()
    for sep in (" — ", " – ", ": ", ". "):
        head = value.split(sep, 1)[0]
        if 20 <= len(head) <= limit:
            return head
    return value[:limit].rstrip(" ,;:") + ("…" if len(value) > limit else "")


def _authority_from_owner(owner: str) -> str:
    """Who must act, read from DUE.md's Owner cell rather than guessed."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", owner or "") if w]
    if not words:
        return "agent"
    human = [w for w in words if w.lower() not in _NON_HUMAN_OWNERS]
    machine = [w for w in words if w.lower() in _NON_HUMAN_OWNERS]
    if human and machine:
        # "LJ + agent": the loop may do its half, and the principal's half is
        # surfaced. Both is a real value — collapsing it to one loses the
        # distinction the loop's boundary is built on.
        return "both"
    return "principal" if human else "agent"


def _within_days(path: Path, days: int, *, today: date | None = None) -> bool:
    """Was this pack prepared inside the window? A pack directory carries its
    preparation date in its name (`YYYYMMDD-…-LoveWork`).

    **`today` is passed, not read** (2026-10-20). This used `date.today()` while its
    caller had already been handed a `today` — so the decider's answer depended on the
    wall clock even when a caller asked a question about a specific day (`decide.py
    --today`, a backfill, a test). Measured: a fixture with `today=2026-10-11` and packs
    dated 1009/1010 counted three packs on 2026-10-19 and one on 2026-10-20, because the
    "10-day window" was measured from the real day rather than from the one it was
    asked about. A passed-in clock that is not used is a clock the caller cannot control.
    """
    match = re.match(r"(\d{4})(\d{2})(\d{2})-", path.name)
    if not match:
        return False
    prepared = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return ((today or date.today()) - prepared).days <= days


def _needs_advert_link(pack: Path) -> bool:
    """No link, or only a re-post's link: the advert behind the pack is missing.

    A board link (`advert_link_is_board`) is not the employer's advert, so the
    pack still needs `resolve_advert` — otherwise the review guard would refuse
    to score it and nothing would ever replace the link (2026-11-03).
    """
    from application_packs import advert_link_is_board, pack_advert_url

    url = pack_advert_url(pack)
    return (not url) or advert_link_is_board(url)


def _item(kind: str, what: str, *, decision: str | None, authority: str,
          next_step: str, evidence: str, action: str, cls: str,
          due: str | None = None, rung: int = RUNG_LOCAL,
          target: str | None = None, order: int = 0) -> dict:
    """One row of the answer. `decision=None` means "maintenance, not loop work".

    `target` is the thing the action must work on. It travels with the item
    rather than being re-derived inside the action: on 2026-10-11 the action
    picked its own pack, so every receipt named a decision the tick had not
    served (measured: lj worked `z0.ai` while the receipt said
    "amiengineer at advancedmachineintelligence…"). One decision, one target,
    one receipt.

    `order` is the rank *inside* a kind whose order is not derivable from the
    generic key — `review_go` is ordered by the principal decision it could
    change (advert-less scores first, then the oldest review, then the fewest),
    which no combination of date and name can express. It is read only after
    `kind`, so setting it on one kind cannot reorder another.
    """
    return {"kind": kind, "what": what, "decision": decision,
            "authority": authority, "next_step": next_step,
            "evidence": evidence, "action": action, "class": cls,
            "due": due, "rung": rung, "target": target, "order": order}


#: Principal ids as they appear in DUE.md prose. A clock that names one of
#: these is that principal's; one that names none is repo-level and belongs to
#: every principal (the sync_skills duty, a cooldown expiry).
_PRINCIPAL_TOKENS = ("lj", "vj", "kj", "pk")


def _target_ordinal(target: str | None) -> int:
    """A pack's preparation date as an ordinal, from its directory name.

    `0` for an item with no dated target, so it sorts after every dated one.
    """
    match = re.match(r"(\d{4})(\d{2})(\d{2})-", Path(target or "").name)
    if not match:
        return 0
    return date(int(match.group(1)), int(match.group(2)),
                int(match.group(3))).toordinal()


def _names_principals(text: str) -> set:
    return {p for p in _PRINCIPAL_TOKENS
            if re.search(rf"\b{p}\b", text or "", re.I)}


def _clocks(repo_root: Path, today: date, profile: str) -> list[dict]:
    """DUE.md's open clocks — the authority for anything dated (docs/27 §4b)."""
    from situation import _clock_date, due_projection

    out = []
    for row in due_projection(repo_root, today=today).get("manual", []):
        when, what = row.get("when", ""), row.get("what", "")
        # A clock that names principals, none of them this one, is not this
        # principal's business — `Decide pr_0001` and the J&J letter are LJ's,
        # and showing them under vj/kj/pk would be a false claim about what is
        # due for them.
        named = _names_principals(" ".join(str(v) for v in row.values()))
        if named and profile not in named:
            continue
        label = _short(what)
        fired = _clock_date(when)
        authority = _authority_from_owner(row.get("owner", ""))
        out.append(_item(
            "due_clock", f"{_short(when, 30)} — {label}",
            # DUE.md already declares what the clock protects: "Consequence if
            # missed" *is* the principal decision at stake, so it is read, not
            # invented. `next_action` is the declared step; `owner` the
            # declared authority.
            decision=_short(row.get("consequence") or label, 130),
            authority=authority,
            next_step=_short(row.get("next_action") or
                             "act before the date, or record why the clock moved", 130),
            evidence=f"DUE.md · {when} · owner: {row.get('owner') or 'unstated'}",
            action=("external_effect" if authority == "principal"
                    else "local_projection"),
            cls="clock", due=fired.isoformat() if fired else None))
    return out


def _run_health(state_root: Path, profile: str, today: date) -> list[dict]:
    """Is the principal's world view current? A stale sweep makes every other
    read untrustworthy, so this ranks above the work it gates."""
    out = []
    try:
        from principal_runtime import resolve_principal_runtime
        from strata import StrataEngine
        import status as status_mod

        runtime = resolve_principal_runtime(profile)
        if not runtime.state_root.is_dir():
            return out
        st = status_mod.status(runtime, StrataEngine(runtime.state_root))
    except Exception as exc:                       # never silently swallow
        return [_item("decider_error", f"status read failed: {exc}",
                      decision="whether the operational picture can be trusted",
                      authority="agent", next_step="fix the read, then re-decide",
                      evidence="status.py", action="read", cls="health")]

    latest = st.get("latest_run") or {}
    if latest and not latest.get("sealed"):
        out.append(_item(
            "unsealed_run", f"latest run {latest.get('run_id')} is not sealed",
            decision="whether the principal's findings rest on captured evidence",
            authority="agent", next_step="confirm the run sealed, or re-run it",
            evidence=f"status.py · latest_run {latest.get('run_id')}",
            action="read", cls="health"))
    newest = (st.get("seals") or {}).get("newest")
    if newest:
        from run_ledger import local_date_of

        sealed = local_date_of(str(newest))
        age = (today - sealed).days if sealed is not None else 0
        if age > 7:
            out.append(_item(
                "stale_world", f"newest result seal is {age} days old",
                decision="whether the feed the principal reads is still true",
                authority="agent", next_step="run or schedule a sweep",
                evidence=f"status.py · newest seal {newest}",
                action="acquire", cls="health", rung=RUNG_ACQUIRE))
    return out


def _principal_pending(state_root: Path, profile: str,
                      today: date | None = None) -> list[dict]:
    """Things that are *finished by the agent and waiting on the principal*.

    This is the loop's most valuable output and it must be a normal result, not
    a failure: a prepared pack, a written letter, a proposition awaiting a
    ruling. The decider cannot see a *send* (the engine never sends), so it says
    what it cannot know rather than guessing.
    """
    out = []
    apps = state_root / "applications"
    try:
        from application_packs import uninvestigated_packs
        # Bounded to the recent window, and aggregated: "submit a pack" is one
        # decision, not one decision per pack ever prepared. The whole corpus
        # here is ~400 packs and listing them drowned every clock (2026-10-11).
        # 10 days, the matcher's own stale-advert cap: a pack whose advert is
        # older than that is not something the principal should still be
        # deciding to send.
        recent = [p for p in sorted(apps.glob("*-LoveWork"))
                  if p.is_dir() and _within_days(p, 10, today=today)]
        # `uninvestigated_packs` yields dossier `.txt` paths, so comparing them
        # against pack *directories* matched nothing and every recent pack
        # counted as ready — including the uninvestigated ones (2026-10-11).
        not_yet = {txt.parent for txt in uninvestigated_packs(apps)}
        ready = [p for p in recent if p not in not_yet]
    except Exception as exc:
        # This is the principal's most valuable item; losing it silently is the
        # worst version of the swallow.
        out.append(_item("decider_error", f"pack corpus read failed: {exc}",
                         decision="whether the prepared packs can be trusted",
                         authority="agent", next_step="fix the read, then re-decide",
                         evidence="application_packs.py", action="read", cls="health"))
        ready = []
    if ready:
        newest = ready[-1]
        out.append(_item(
            "pack_awaiting_principal",
            f"{len(ready)} pack(s) ready to submit (newest {newest.name})",
            decision=f"which of the {len(ready)} recently prepared packs to pursue first",
            authority="principal",
            next_step="triage with pack_queue.py; the principal submits, never the agent",
            evidence=f"{newest} (and {len(ready) - 1} more prepared in the last 10 days)",
            action="external_effect", cls="principal"))

    letters = state_root / "letters"
    written = sorted(letters.glob("jackandjill-*.md")) if letters.is_dir() else []
    if written:
        newest = written[-1]
        when = date.fromtimestamp(newest.stat().st_mtime)
        if (date.today() - when).days <= 14:
            out.append(_item(
                "letter_awaiting_principal", f"letter written: {newest.name}",
                decision="whether to send this week's letter to Jack & Jill",
                authority="principal",
                next_step="read, edit and send it — the engine never sends",
                evidence=str(newest),
                action="external_effect", cls="principal"))

    try:
        import propositions
        folded = propositions.fold(state_root)
    except Exception as exc:
        out.append(_item("decider_error", f"proposition ledger read failed: {exc}",
                         decision="whether a pending ruling can be seen",
                         authority="agent", next_step="fix the read, then re-decide",
                         evidence="propositions.py", action="read", cls="health"))
        folded = []
    for row in folded:
        if row.get("status") in ("proposed", "hypothesis"):
            pid = row.get("proposition_id") or row.get("id") or "?"
            out.append(_item(
                "proposition_awaiting_principal",
                f"{pid} ({row.get('status')}): {_short(row.get('claim', ''), 70)}",
                decision="whether to accept this as the principal's own statement",
                authority="principal",
                next_step="accept it, or leave it a hypothesis for corroboration",
                evidence=f"dataset/propositions.jsonl · {pid}",
                action="mutate_principal_state", cls="principal"))
    return out


def _pack_work(state_root: Path, profile: str, today: date) -> list[dict]:
    """Packs the agent can carry: prepare, investigate, or re-check."""
    out = []
    try:
        import pack_queue
        pending = pack_queue.queue(state_dir=state_root.parent, profile=profile,
                                   today=today)
    except Exception as exc:
        # Not `pending = []`: an unreadable queue is not the same answer as an
        # empty one, and swallowing it made a broken dependency look like "no
        # packs to do" (the class fixed in tick.py the same day).
        return [_item("decider_error", f"pack queue read failed: {exc}",
                      decision="whether the pack queue can be trusted",
                      authority="agent", next_step="fix the read, then re-decide",
                      evidence="pack_queue.py", action="read", cls="health")]
    # The whole queue, not the empty-block intersection (2026-10-19). The
    # intersection was the livelock fix for the *investigate-only* era
    # (2026-10-13: the tick filled the block, the queue still listed the pack
    # for the tailoring it lacked, the decider named it again, and the action
    # refused its own fresh draft). `complete_pack` finishes **all three
    # steps** — review, diligence, material — so a pack whose block is filled
    # but whose material is missing is exactly its work, and completing it
    # leaves the queue (the material is the queue's own notion of done), so
    # the livelock cannot recur. Mission day measured the gap the intersection
    # had become: LJ's *"application packs for all before Mon 19:00"* faced
    # queues of 30/19/100 packs (vj/kj/lj) **all** filled-block-but-incomplete
    # — the intersection named none of them, and the loop would have idled
    # through the deadline.
    from application_packs import pack_advert_url, pack_parts

    queued = {Path(row["pack"]): row for row in pending}
    # Forty, not ten: the runner filters these again by what it can actually do
    # (a fresh draft is finished work), so a ten-item window can be consumed
    # entirely by items it will skip and a run stops early with "nothing
    # actionable left" while doable packs sit just outside the window — measured
    # 2026-10-13, vj and kj did 3 of 6 in the 14:05 run and 6 of 6 when asked
    # directly.
    for pack in sorted(queued, reverse=True)[:40]:
        row = queued[pack]
        org, role = pack_parts(pack)
        if _needs_advert_link(pack):
            # A pack with no advert link cannot be completed, investigated or
            # reviewed: every one of those needs the advert. Resolving the link
            # is the *first* work the loop can do on it (2026-10-23) — and with
            # Tavily search it is ordinary work, not a permanent block.
            out.append(_item(
                "pack_pending",
                f"{row['principal']}: {org} — {role} (no advert link)",
                decision=f"whether {row['principal']} pursues {role} at {org}",
                authority="agent",
                next_step="search for the employer's own advert and record its link in the pack",
                evidence=str(pack), action="resolve_advert", cls="work",
                rung=RUNG_INFERENCE, target=str(pack)))
            continue
        out.append(_item(
            "pack_pending", f"{row['principal']}: {org} — {role}"
            " (steps 1-3 incomplete)",
            decision=f"whether {row['principal']} pursues {role} at {org}",
            authority="agent",
            next_step="complete the pack: advert review, diligence, tailored material",
            evidence=str(pack), action="complete_pack", cls="work",
            rung=RUNG_INFERENCE, target=str(pack)))
    return out


#: How many GO reviews the decider names in one answer. Forty, the same bound as
#: the pack queue and for the same measured reason: the runner filters these again
#: by what it can actually do (a fresh review is finished work), so a short window
#: can be consumed entirely by items it will skip and a run stops early with
#: "nothing actionable left" while doable GOs sit just outside it (2026-10-13).
REVIEW_QUEUE = 40


#: How many unprepared GO packs one principal offers per decision. The runner takes
#: them one action at a time and re-decides on the next tick, so this is a window,
#: not a plan: a ten-item window was measured too small on 2026-10-13 (items it
#: would skip consumed it), and the drain re-reads anyway.
GO_PACK_QUEUE = 60


def _go_pack_work(state_root: Path, profile: str, today: date) -> list[dict]:
    """**The GO-to-pack rule**: a GO whose pack the principal cannot apply with.

    LJ, 2026-10-19: *"we must create them asap, and make that a rule: all GO-s should
    have application packs before the Monday 19:00 wiki state email … either the pack
    is created => it remains on GO; or if we fail, we retry; but if not possible (the
    advert is gone and we no longer have it) => then this should not be in GO, it
    should be in leads."*

    Ranked by the **score** of the opening — the best opportunity first — because
    that is the one whose pack the principal is most likely to use. `pack_readiness`
    is the authority; the candidate set is the index's own GO view, so a pack whose
    advert has since been re-judged is not prepared as if it were still a GO.
    """
    from application_packs import pack_advert_url, pack_for, pack_index, pack_readiness
    from wiki_store import WikiStore

    try:
        store = WikiStore(root=state_root / "wiki", create=False)
        _current, fresh = store.current_view(today=today)
        # **MAYBE packs too** (LJ, 2026-10-20: *"Atm the number of GO-s for vj is slim. Let's
        # make the MAYBE-s also prepared for application. So Add the requirement for preparing
        # application packs for MAYBE-s too."*). A MAYBE is a lead worth an hour of work and
        # not yet worth the principal's attention; preparing its pack is what turns it into a
        # decision he can make in one click. GOs stay first — `KIND_ORDER` and the slot's tier
        # ordering both see to that.
        gos = [e for e in fresh if e.decision in ("GO", "MAYBE")]
        packs = pack_index(profile, root=state_root / "applications")
    except Exception as exc:                       # never silently swallow
        return [_item("decider_error", f"GO pack set read failed: {exc}",
                      decision="whether the GOs' packs can be trusted",
                      authority="agent", next_step="fix the read, then re-decide",
                      evidence="wiki_store.current_view · application_packs.pack_readiness",
                      action="read", cls="health")]

    out: list[dict] = []
    rows = []
    for e in gos:
        pack = pack_for(packs, e.org_name, e.title, e.original_url or e.url or "")
        if pack is None:
            # No pack directory at all — `go_without_pack` is that class, and
            # `prepare_missing_packs` cuts the scaffold. Not pack-writing work.
            continue
        state = pack_readiness(pack)
        kind = "go_pack" if e.decision == "GO" else "maybe_pack"
        # **The board-link veto comes before "ready"** (2026-11-04). A ready pack
        # can still carry the one piece of work no reviewer will do: its link is a
        # re-post, and `review_go` refuses to score one. Nothing in this module
        # named such a pack, so it sat in `_go_reviews`' queue instead and blocked
        # on the same link every tick after paying for a fetch — measured: 25 of
        # lj's 26 GO reviews were board links (21 digitaljobs, 4 LinkedIn) and the
        # 8-minute slot was spent re-walking them, scoring nothing. Resolving the
        # link is the *first* work the loop can do on it (2026-10-23) and the only
        # action that can move it.
        if _needs_advert_link(pack):
            # A pack with no advert link cannot be completed, investigated or
            # reviewed: every one of those needs the advert. With Tavily search it
            # is ordinary work, not a permanent block.
            out.append(_item(
                kind,
                f"{e.org_name} — {e.title} — the {e.decision} pack has no employer advert link",
                decision=f"how {e.title} at {e.org_name} becomes an opening the principal "
                         f"can apply to",
                authority="agent",
                next_step="search for the employer's own advert and record its link in the pack",
                evidence=str(pack), action="resolve_advert", cls="work",
                target=str(pack)))
            continue
        if state["ready"]:
            continue
        rows.append((pack, e, state))
    # Best opening first, then the oldest case — a stable order the drain can resume.
    rows.sort(key=lambda r: (-(getattr(r[1], "score", 0) or 0), r[0].name))

    for pack, e, state in rows[:GO_PACK_QUEUE]:
        score = getattr(e, "score", 0) or 0
        kind = "go_pack" if e.decision == "GO" else "maybe_pack"
        out.append(_item(
            kind,
            f"{e.org_name} — {e.title} — the {e.decision} pack is not ready ({state['why']})",
            decision=f"how {e.title} at {e.org_name} becomes an opening the principal "
                     f"can apply to",
            authority="agent",
            next_step="write the pack's review, diligence and tailored material",
            evidence=f"{pack} · {score:.1f}/10 · {state['why']}",
            action="complete_pack", cls="work", target=str(pack),
            order=int(1_000 - score * 10)))
    return out


def _go_reviews(state_root: Path, profile: str, today: date) -> list[dict]:
    """The GOs to re-read against their own advert (LJ, 2026-10-18).

    *"after that - to be reviewing existing GOs and refining them, WE want high
    quality, high SNR."* Every GO has a pack, so the loop's pack work is done;
    what is left is the **quality** of what is on file, and the local model is
    idle for it. Ranked by the principal decision a review could change, in the
    order LJ named:

    1. **GOs whose score never saw the advert's own text.** The highest value,
       because part of that reasoning was inferred rather than read — measured on
       PostHog: the title/list path gave 8.9 with an invented rationale, and the
       same advert read in full gave 8.2 with a grounded one. The fact comes from
       the pack's own dossier (`Primary evidence`), which is set by the matcher
       only when it judged the fetched advert page.
    2. **Then the oldest review**, so the corpus rotates instead of one GO being
       re-read forever.
    3. **Then the fewest reviews**, so a GO never re-read comes before one that
       has been re-read twice.

    The candidate set is the index's own GO view — `WikiStore.current_view` — so
    the queue is exactly the GOs the principal can still see, and a retired or
    re-judged advert is not reviewed as if it were current. A GO with no pack is
    left to `prepare_missing_packs` (`go_without_pack` is its defect class): a
    review has no dossier to be written beside.
    """
    # `tick` owns the loop's residue (`loop/`), so its review ledger is read
    # through its own reader rather than by parsing the file here — the same rule
    # as every other fact. The import is inside the function because `tick`
    # imports this module: the decider answers, the runner acts (ch43 §1).
    #
    # **Measured cost, so nobody has to re-derive it.** In the tick's own process
    # (`tick.py` *is* the process) this import is free and the queue costs ~1.7 s
    # for lj — `WikiStore.current_view` over 2,011 org pages (1.4 s), `pack_index`
    # (0.23 s), then ~0.04 s for the per-pack dossier reads. A **standalone**
    # `decide.py` pays 3.6 s more, because importing `tick` pulls `prompts` and
    # therefore `dspy`; nothing but the tick imports this module, so that is a
    # diagnostic's cost, not the loop's.
    from tick import pack_advert_url, read_reviews, review_is_fresh

    try:
        from application_packs import dossier_entry, pack_for, pack_index
        from wiki_store import WikiStore

        # `create=False`: this is a read, and a read must not write. `WikiStore`
        # makes its own `orgs/` and `reports/` on construction, so a plain
        # `WikiStore(root)` here created two directories in the principal's tree
        # once an hour — caught by `test_deciding_is_a_pure_read`.
        store = WikiStore(root=state_root / "wiki", create=False)
        _current, fresh = store.current_view(today=today)
        gos = [e for e in fresh if e.decision == "GO"]
        packs = pack_index(profile, root=state_root / "applications")
    except Exception as exc:                       # never silently swallow
        return [_item("decider_error", f"GO review set read failed: {exc}",
                      decision="whether the GOs on file can be re-read",
                      authority="agent", next_step="fix the read, then re-decide",
                      evidence="wiki_store.current_view · application_packs.pack_index",
                      action="read", cls="health")]

    reviews = read_reviews(state_root)
    candidates = []
    for e in gos:
        pack = pack_for(packs, e.org_name, e.title, e.original_url or e.url or "")
        if pack is None or review_is_fresh(state_root, pack, today, reviews=reviews):
            continue
        # A pack with no advert link cannot be re-read, and naming it would put a
        # `blocked` item at the head of the queue every tick (lj's top-ranked GO
        # on 2026-10-18 carried `Primary advert URL: not retained`). That is the
        # `no advert link` / `go_unpackable` class, which the wiki audit and
        # `prepare_missing_packs` own — not review work.
        advert = pack_advert_url(pack)
        if not advert:
            continue
        # …and neither can a pack whose only link is a **re-post**: `review_go`
        # refuses to score a board page (2026-11-03), so naming it here put a
        # permanently-blocked item in the queue and the slot paid a fetch to learn
        # what the URL already said. That pack's work is `resolve_advert`
        # (`_go_pack_work` names it), which is what can put the employer's advert
        # in its place. Measured 2026-11-04: 25 of lj's 26 reviews were board
        # links; the reviews themselves were the whole 8-minute slot.
        from application_packs import advert_link_is_board

        if advert_link_is_board(advert):
            continue
        rebuilt = dossier_entry(pack)
        if rebuilt is None:
            continue
        entry, _identity = rebuilt
        row = reviews.get(pack.name) or {}
        candidates.append({
            "pack": pack, "entry": entry, "org": e.org_name, "role": e.title,
            "advert": advert,
            "count": int(row.get("count", 0)),
            "last": str(row.get("at") or "")[:10],
            "no_evidence": not entry.primary_content_hash,
        })
    candidates.sort(key=lambda c: (0 if c["no_evidence"] else 1, c["last"],
                                   c["count"], c["pack"].name))

    out = []
    for rank, c in enumerate(candidates[:REVIEW_QUEUE]):
        pack, entry = c["pack"], c["entry"]
        seen = (f"{c['count']} review(s), last {c['last']}" if c["count"]
                else "never re-read")
        out.append(_item(
            "review_go",
            f"{c['org']} — {c['role']} — "
            f"{'scored without the advert' if c['no_evidence'] else 'scored with the advert'}"
            f"; {seen}",
            decision=f"whether the GO on {c['role']} at {c['org']} still holds when "
                     f"the advert itself is read",
            authority="agent",
            next_step="fetch the advert, re-score it against that, and note what moved",
            evidence=f"{pack} · {entry.score:.1f}/10 · evidence: "
                     f"{entry.primary_content_hash[:12] or 'not retained'}"
                     f" · advert: {c['advert'][:60]}",
            action="review_go", cls="work", rung=RUNG_INFERENCE,
            target=str(pack), order=rank))
    return out


def _maintenance(state_root: Path, profile: str, repo_root: Path) -> list[dict]:
    """Hygiene with no principal decision behind it. Reported, never ranked."""
    out = []
    attic = state_root / "wiki" / "_attic"
    if attic.is_dir():
        n = len(list(attic.glob("*.md")))
        if n:
            out.append(_item(
                "attic_pages", f"{n} pages in the attic",
                decision=None, authority="none",
                next_step="triage into a merge or a retirement, in a maintenance pass",
                evidence=str(attic), action="read", cls="maintenance"))
    for name, path in (("MANUAL.md", state_root.parent / "MANUAL.md"),
                       ("state/<p>/STATE.md", state_root / "STATE.md")):
        if path.is_file():
            age = (date.today() - date.fromtimestamp(path.stat().st_mtime)).days
            if age > 3:
                out.append(_item(
                    "projection_lag", f"{name} is {age} days old",
                    decision=None, authority="none",
                    next_step="regenerate it (it is a projection, never hand-edited)",
                    evidence=str(path), action="local_projection",
                    cls="maintenance"))
    return out


def decide(profile: str = "lj", *, today: date | None = None,
           state_dir: Path | None = None, repo_root: Path | None = None) -> dict:
    """The whole answer: ranked items, the maintenance tail, and the envelope."""
    today = today or date.today()
    state_dir = state_dir or config.STATE_DIR
    repo_root = repo_root or config.LOVEWORK_ROOT
    state_root = state_dir / profile

    items: list[dict] = []
    if state_root.is_dir():
        items += _run_health(state_root, profile, today)
        items += _principal_pending(state_root, profile, today)
        items += _pack_work(state_root, profile, today)
        items += _go_pack_work(state_root, profile, today)
        items += _go_reviews(state_root, profile, today)
    items += _clocks(repo_root, today, profile)

    maintenance = _maintenance(state_root, profile, repo_root) if state_root.is_dir() else []

    def key(row: dict) -> tuple:
        dated = row["due"] is not None
        # Ties inside a kind are broken by the *target's* recency, newest first —
        # a pack item has no clock of its own, and sorting those by their display
        # text made the loop work the alphabet instead of the newest GO
        # (measured 2026-10-13). `-ordinal` because the tuple sorts ascending.
        kind = row["kind"]
        return (CLASS_ORDER.index(row["class"]), not dated,
                row["due"] or "9999-99-99",
                # A designed precedence, then the rest by the generic key.
                (KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)),
                kind,
                # `order` before the target's recency: a kind that ranks its own
                # items (review_go) must not have that rank broken by the generic
                # rule. It is 0 everywhere else, so nothing moves.
                row.get("order", 0),
                -_target_ordinal(row.get("target")), row["what"])

    items.sort(key=key)
    due_items = [i for i in items if i["decision"]]
    dated = [i for i in due_items if i["due"]]
    return {
        "profile": profile,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "due": due_items,
        "maintenance": maintenance,
        "counts": {"due": len(due_items), "maintenance": len(maintenance),
                   "needs_principal": sum(1 for i in due_items
                                          if i["authority"] == "principal"),
                   "dated": len(dated)},
        "earliest": dated[0]["due"] if dated else None,
        "envelope": _envelope(),
        "note": ("nothing is due: the common and cheapest case"
                 if not due_items else ""),
    }


def _render(result: dict, *, show_maintenance: bool) -> str:
    c = result["counts"]
    lines = [f"{result['profile']}: {c['due']} due "
             f"({c['needs_principal']} need the principal"
             + (f", earliest {result['earliest']}" if result['earliest'] else "")
             + f") · {c['maintenance']} maintenance"]
    if not result["envelope"]["set"]:
        lines.append("  envelope: UNSET — no daily bound is configured "
                     "(LOVEWORK_LOOP_DAILY_CALLS / _MINUTES)")
    for row in result["due"]:
        head = f"  [{row['class']}] {row['what']}"
        lines.append(head)
        lines.append(f"      decision : {row['decision']}")
        lines.append(f"      authority: {row['authority']}  action: {row['action']}"
                     f"  rung: {row['rung']}" + (f"  due: {row['due']}" if row['due'] else ""))
        lines.append(f"      next     : {row['next_step']}")
        lines.append(f"      evidence : {row['evidence']}")
    if show_maintenance and result["maintenance"]:
        lines.append("  maintenance (no principal decision behind it):")
        for row in result["maintenance"]:
            lines.append(f"    - {row['what']}")
    return "\n".join(lines)


def _status_line(result: dict) -> str:
    """One receipt line, and it names the principal: a cron log with three
    principals in it needs to know which one a line is about."""
    c = result["counts"]
    earliest = f", earliest {result['earliest']}" if result["earliest"] else ""
    return (f"{result['profile']}: due {c['due']} "
            f"(principal {c['needs_principal']}{earliest}) "
            f"maintenance: {c['maintenance']}")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default="lj")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--status", action="store_true",
                        help="one receipt line, for a cron tick")
    parser.add_argument("--all", action="store_true",
                        help="include the maintenance tail")
    parser.add_argument("--today", default=None, help="YYYY-MM-DD (testing)")
    args = parser.parse_args(argv)

    today = date.fromisoformat(args.today) if args.today else None

    if args.profile == "all":
        # `all` is a selector, not a principal name. Passing it through answered
        # for a principal called "all" — no state, so only the repo-level clocks
        # — and printed one line that looked like a real answer (2026-10-11).
        from situation import _principal_profiles

        profiles = [p for p in _principal_profiles(config.LOVEWORK_ROOT)
                    if (config.STATE_DIR / p).is_dir()]
    else:
        profiles = [args.profile]
        if not (config.STATE_DIR / args.profile).is_dir():
            from situation import _principal_profiles

            if args.profile not in _principal_profiles(config.LOVEWORK_ROOT):
                # A typo must not read as "nothing is due": absent is not empty.
                print(f"unknown principal: {args.profile!r} — no "
                      f"state/{args.profile}/ and no profiles/{args.profile}/",
                      file=sys.stderr)
                return 2

    results = []
    for profile in profiles:
        try:
            results.append(decide(profile, today=today))
        except Exception as exc:
            print(f"decide failed for {profile}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 1

    if args.json:
        payload = results if args.profile == "all" else results[0]
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif args.status:
        for result in results:
            print(_status_line(result))
    else:
        for result in results:
            print(_render(result, show_maintenance=args.all))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
