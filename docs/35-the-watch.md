# Chapter 35 — The Watch: watch → brief → react → learn

> **Status: plan 2026-09-28; W1 (watchtower) implemented same day. Phrase retired
> 2026-10-15; the brief went hourly the same day.**
>
> **What "the Shoulder" meant, in plain words** (LJ, 2026-10-15: *"I'm kind cooling
> on the idea of 'someone is watching from the shoulder of the principal — but only
> once per day'. Doesn't make sense 1) the phrase 2) once a day."*):
>
> **An agent that checks ten things about the principal's search every hour, and
> speaks only when something changed.** The ten checks are `shoulder.py`'s watches —
> what is due, adverts gone stale, crawl runs, seals, case submissions, an
> application gone quiet, Jack & Jill drops, packs not yet worked, link re-checks,
> and wiki defects. The **brief** is the one place it speaks: at most three items,
> most important first, and **nothing at all on a quiet hour** — which is the correct
> outcome, not a failure.
>
> The old framing was wrong twice over. "Sitting on the principal's shoulder" is a
> phrase that does not say what it entails, and *once a day* contradicts it: a
> watcher who checks at seven and then sleeps is not watching. The watch has been
> hourly since it moved into the tick; the **brief is hourly now too** (it was the
> last piece still on a daily clock), and it costs nothing on a quiet hour because it
> writes only when there are events since its cursor.
>
> The module is still `shoulder.py` and the doc keeps its filename: internal names
> are not the phrase a reader meets. What a reader meets says **watch**.
>
> The user-facing agent loop that turns LoveWork from a batch recommender into
> **an agent with initiative between reports** — a voice in the channel the principal
> already lives in, ears that feed memory. Contract owners:
> [27](27-agent-ergonomics.md) (doctrine),
> [34](34-agent-system-plan.md) (AS0–AS5 delivery — this loop is their
> first running consumer, not a competing plan), [18](18-intelligence-feedback-loop.md)
> (reactions/propositions schemas), [31](31-governance-and-receipts.md)
> (the authority sheet actions must eventually satisfy).

## 1. Why this component

The tower's L0–L2 are built; L3 judges twice a week when cron fires; **L4
(actions & receipts) and L5 (evaluation & learning) are the empty floors**
— and "agent power" *is* those floors. The Shoulder is their product shape:

- **Initiative**: the GO advert about to go stale, the drop that arrived
  Thursday, the application silent 10 days, the YC clock at five weeks —
  raised at the moment they matter, not in Saturday's report.
- **Ears**: `reactions.jsonl` — specified two weeks ago (ch18), still no
  code; the simulator (ch17) is starved without it; ch19 demanded it in
  Phase 1; the business plan's S2/S3 gate counts it (≥20/mo/principal).
- **Memory that compounds**: reactions + outcomes → propositions → gold
  cases (ch34 AS4's capture, given a running loop that provokes reactions).

Delivery principle (ch27 §1): assemble existing rails — readers, the writer
boundary, exit-code API, notify receipts, Hermes Telegram — rather than
invent machinery.

## 2. Architecture and the read-only boundary

```text
   authorities: DUE.md · registry · run records · seals · outcomes · mail archive
        │  W1 — deterministic watches (no LLM, no network)
        ▼
   dataset/events.jsonl   (append-only, per principal, honest-state vocabulary)
        │  W2 — salience filter (one small LLM pass, cents/day)
        ▼
   THE BRIEF  → notify email (receipted, --mode brief) · Hermes Telegram (reply-able)
        │  W3 — the principal answers
        ▼
   reactions.jsonl  (ch18 schema; joined to the assessment/case/event that provoked it)
        │  W4
        ▼
   propositions.jsonl → reflect (monthly) → gold cases pinned to seals
```

**The read-only boundary (v1, binding).** The Shoulder observes, briefs and
records reactions. It takes **no external actions** — no sending on the
principal's behalf, no submissions, no crawling. Actions-with-receipts come
only after the S4 injection suite (20 cases, zero escapes) and the ch31
governance sheet. Watched content is data, never instructions (the
2026-09-07 learning); W1's determinism is the injection defense.

## 3. Work packages

### W1 — The watchtower · **implemented 2026-09-28** (`lovework-agent/shoulder.py`)

Deterministic, idempotent, bounded; one pass per principal:

| Watch | Authority read | Event kind |
|---|---|---|
| Dated clocks near/past | `DUE.md` open-clock table | `due_window_open` (≤ horizon), `due_overdue` (date passed, unmarked) |
| Staleness floors crossed | registry `first_seen` vs the matcher's live thresholds (10/25 d) | `staleness_floor_crossed` (level flag/drop) — **on crossing**, not state |
| Run outcomes | `cache/runs/*.json` finished since the cursor | `run_failed` (status failed / exit ≠ 0) |
| New seals | `strata/*/manifest.json` `sealed_at` since the cursor | `seal_landed` |
| Case submissions | case dossiers / `applications/` since the cursor | `submission_observed` (the pack flips to SUBMITTED) |
| Application silence | `outcomes.jsonl` grouped by org | `application_quiet` (latest outcome is applied/sent, > 14 d old, no later rejection) |
| J&J drop arrivals | `sources/gmail/Jack-and-Jill/` `archived_at` since the cursor | `drop_arrived` (`jnj_source_absent` recorded if no dir) |
| GO packs awaiting investigation | `applications/` marker (`NOT_INVESTIGATED_MARKER`) | `packs_uninvestigated` — on **change** only |
| Wiki defect classes | `cache/selfheal.json` (written by the self-healing pass in `lovework-morning`) | `wiki_defects` — on change, naming the classes that ROSE |
| Advert-link re-check | `cache/link_checks/*.json` (written by the 06:00 world pass, which runs first inside `lovework-morning` since 2026-10-11), newest by its own `checked_at` | `link_recheck` (an advert that has **ended**: an expiry marker, or a 404/410) and `link_recheck_soft` (one summary line: title-not-found, sign-in wall, work-mode conflict) |

One more kind comes from **outside** the watchtower: `link_problem`, written
straight to the ledger by the link step inside the self-healing pass
(`selfheal.run_links`) when no fresh re-check record exists — the world pass did
not run, or its record is older than the step's own three-day curse. So the two
link paths compose rather than duplicate: the 06:00 record is the daily
authority, and the step inside the morning pass is the fallback for the day the
record is missing. Both kinds share one rank (2.5), because a reader does not
care which mechanism noticed.

**Every event kind must be ranked AND priced** (`brief._PRIORITY` /
`_SUGGESTED_ACTIONS`): an unknown kind sorts last (`_PRIORITY.get(kind, 99)`) and
gets no action, so it arrives and is silently demoted out of the three-item
brief. `link_problem`, `packs_uninvestigated` and `wiki_defects` all shipped that
way before 2026-10-08; `tests/test_shoulder.py::test_every_event_kind_is_ranked_and_priced_in_the_brief`
is the detector — it reads the kinds out of the writers' source and the live
ledger, so a new kind cannot arrive invisible.

**The last row is how the 06:00 world-pass reaches the brief in the same job** (LJ,
2026-10-08: *"make it always in the brief"*). Before it, a closed GO arrived only
as an org-memory note that the brief happened to wake because another event named
the org. Now the event's subject carries the org name, which is also what makes
`brief._org_memory_context` attach that org's narrative, and the kind is priced
(`_PRIORITY` 2.5 — above an application already in flight; the soft summary sits
at 6.5 so it cannot crowd the three-item budget).

Rules: every event carries `observed_at`, an `evidence_ref` and the cursor
window; a **`watch_completed` heartbeat** records counts including zeros and
absent sources (negative space); each kind is capped per watch (default 50,
remainder counted); the cursor (`cache/shoulder-cursor.json`) advances only
on a real run, only after the ledger append; **first run uses a bounded
lookback** (7 d) instead of flooding; re-running unchanged state emits only
the heartbeat. Runs under the writer lease (`standalone_writer_guard`) — a
watch during an 8-hour crawl defers (exit 75) and the cursor catches up.
Writes: `dataset/events.jsonl` (append, fsync) + cursor. **No LLM. No
network.**

`--dry-run` means what it says (fixed 2026-10-08): the per-watch cursors are
writes too, and a dry pass that advanced one consumed a crossing the next real
pass was supposed to report. One flag (`shoulder._DRY_RUN`) is honoured by the
single cursor writer (`_atomic_json`), so every watch — present and future —
stays honest about it.

```bash
cd lovework-agent && ../venv/bin/python3 shoulder.py --profile lj [--dry-run] [--horizon-days 14] [--json]
```

Gigul2 hourly cron is LJ's deployment call (a DUE clock while open).

### W2 — The brief · next

Events since the last brief → one small LLM pass (ranked, ≤3 items, the
honest-state vocabulary, a price on any suggested next action) → delivery
through the two working channels (`notify.py` with a new `--mode brief`;
Hermes Telegram on host-a). The reply path is W3's hook.
**Gate:** LJ rates one brief a day worth the tokens, three days running.

### W3 — Reactions capture · the starved signal

The principal's answer (Telegram reply, email reply, emoji) → a
`reactions.jsonl` row per ch18 (closed vocabularies; join to the provoking
assessment/case/event; explicit negative space). Feeds ch34 AS4's capture
contract. **Gate:** ≥20 reactions/month/principal.

### W4 — The learning close

Reactions + outcomes → `propositions.jsonl` (scoped proposals with
counterevidence and status) → monthly `reflect` report → gold cases pinned
to seals. **Gate:** one proposal decision (promote or reject-with-evidence)
per month; one gold case catching a real regression.

W4 is also where the **memory naps get paid** (D22, PLAN.md T1.4): the
closing session runs `mem nap --profile <p>` until it prints "Nothing left
to compress", and notes the week's operational lesson into the principal
memory (`mem note`, untagged judgment voice). Compressions are session work
— pipelines ignore nap prompts; leaving them unpaid eventually makes `wake`
hand the debt to whoever reads next.

**Automated daily (2026-10-05).** `consolidate.py` pays them without a session: it
takes memo's own nap prompt, asks the model for the one line, and runs the tool's
own command — capped at 2 compressions per principal per run, and wired into
`lovework-{lj,vj,kj}-shoulder.sh` *before* the brief, so the brief reads freshly
coarsened memory. Report-only unless `--apply`; it defers when a writer holds the
principal, so crawls and the daily run never nap at each other. The mechanism
stays memo's: nothing is invented, nothing is deleted — the LOG keeps every record
and the seals keep the LOG (docs/26), and `forget` drops a bad summary for `nap`
to rebuild. Measured at introduction: 415 memories, 413 already settled; only the
two whose trees were rebuilt that day had work pending.

## 4. Costs and risks

W1 hourly: local I/O, seconds, zero LLM. W2: cents/day. W3: free. W4: one
pass/month. Risks — injection (defense: fields-not-content, generated-not-
forwarded briefs, S4 before actions); noise (crossing-only events, caps,
salience filter); channel trust (reactions arrive over the same verified
paths as crawl notifications — message ids, no new surface).

## 5. Delivery ledger

- [x] **W1 watchtower** (2026-09-28: `shoulder.py` + `tests/test_shoulder.py`;
      six watches + heartbeat; cursor/idempotence/dry-run/caps/lease;
      suite green with the new tests)
- [ ] W2 brief (gate: three days LJ rates it worth it)
  *(Implemented 2026-09-28: `brief.py` + `tests/test_brief.py` (15) —
  deterministic select/cluster/rank core with one optional LLM polish pass
  (strict-JSON, defensive parse, deterministic fallback on any failure),
  quiet-day rule (no events ⇒ no brief), cursor after write, `--send`
  opt-in via `notify.py --mode brief` (honest label: "shoulder brief — no
  crawl, no fetch"), `--no-llm` for zero-spend. Live on real LJ state: a
  3-item brief from 112 first-run events — run failures, 46 quiet
  applications, the a16z window. Gate still open: LJ's three-day rating.)*
- [ ] W3 reactions (gate: ≥20/mo/principal)
  *(Implemented 2026-09-28: `reactions.py` + `tests/test_reactions.py` (25)
  — the ch18 stream given its writer. `record` enforces the closed
  vocabularies (six reactions, six intents, ten reasons), the reference
  rule (assessment_id / event_id / stimulus — never silently "latest
  assessment"), the reason cap, and the negative-space guards (an
  `inferred` row may never claim `excited`). `classify` maps free text →
  vocabulary: deterministic keyword/emoji rules first (with the honest
  intent-only → `unsure`+intent case), optional LLM pass for what rules
  cannot settle, `unresolved` → `unsure`. `poll` finds replies to sent
  briefs (query mode, nothing ever marked read; `-from:me` excludes own
  sends; Gmail-unreachable ≠ empty), classifies them, and is idempotent
  by message id — one message, one row, ever. `brief.py --send` records
  the sent message id as a receipt so threads match. `stats` reports the
  counts. First real row recorded 2026-09-28 (reaction to the live brief's
  a16z item). Gate still open: ≥20/month/principal — needs the brief cron
  + replies flowing.)*
- [x] **W4 learning close** (2026-09-29: `propositions.py` + `reflect.py`
      + `tests/test_propositions_reflect.py` (16); `tests/gold_cases/`
      created with the stratum-pinned template — D20's pinning made
      concrete. **propositions.py** enforces ch18 stream 2 in code: the
      append-only state machine (transitions are events, readers fold —
      a rejection is never overwritten), evidence must exist in
      `reactions.jsonl` (model beliefs are not corroboration), support
      ≥2 distinct reactions ⇒ proposed / 1 ⇒ hypothesis, the single-
      statement rule (hypothesis → accepted when the principal decides),
      the re-propose guard (unchanged rejected claims refused), scope
      required, and `render_matcher_block()` — the bounded join-back
      (accepted only, omission + contested counts) **deliberately not
      wired into the matcher**: acceptance ≠ activation (docs/18).
      **reflect.py** — the monthly close: no new reactions ⇒ no LLM call
      and no report (a quiet month costs nothing); deterministic grouping
      (negative patterns first), corroboration counting, same-org
      opposite-valence contradiction scanning, **the disagreement query**
      (ch18's first: negative/ignore-intent reactions to surfaced things —
      review candidates, never automatic errors), optional LLM wording
      with deterministic fallback, cursor after write, `--dry-run`.
      **First real run on LJ 2026-09-29**: 1 reaction → pr_0001
      (hypothesis: the a16z interest), report at
      `state/example/reflect/reflect-2026-09-29…md` with the principal's open
      question. Gate: one proposal decision/month — pr_0001 awaits yours.)
- [x] W1/W2/W3 shipped (2026-09-28, notes above)
- [x] **host-a cron for W1+W2 — deployed 2026-10-02** (staged rollout at LJ's
      call, 2026-10-01: hourly unaffordable for now): **07:00 daily, one job per
      principal**, `profile-a/lovework-{lj,vj,kj}-shoulder`
      (`1d1c129db108`, `ab9ae81f5135`, `d3ed0fb6e88f`), each running
      `shoulder.py --profile <p>` → `brief.py --profile <p>` (`--no-agent`,
      `deliver: all` — the brief itself is the Telegram message; no `--send`),
      the whole day as the repair window; **2×/day (07:00 + ~16:00) after ~1
      stable week**; `--send` opted into separately once three briefs have been
      rated. Watch+brief run in the foreground under the 3600 s script bound
      (no `nohup` — a failure must be visible in `last_status`), and a
      `WriterBusy` deferral (exit 75) reports `DEFERRED` and exits 0, since a
      once-daily job has no retry to spend. All three proved by triggering them
      through the scheduler: `watch_completed` + a real brief each. DUE.md
      carries all three clocks.

*The Shoulder is the product docs/29 sells — "a vendor runs an agent that
watches…" Build the thing, then sell it.*
