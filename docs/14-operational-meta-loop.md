# Chapter 14 — Operational Meta-Loop

> **Audience:** LoveWork operators and agents maintaining the system.
> **Purpose:** make scheduled work observable, testable, and repairable rather
> than requiring LJ to remember to chase a missing outcome.

## The invariant

For each scheduled crawl, LoveWork holds an explicit operational contract:

> A principal's **full** crawl is expected at its own scheduled time, and must
> end with either a Telegram-visible failure or a Gmail completion notification
> whose Gmail API message ID is recorded.

The schedule (HermeL, host-a; re-timed 2026-09-12, LJ shifted +1 h the same day
after its sweep overran the assumption; LJ moved 10:00 → 08:00 on 2026-10-11
after a 14 h 00 m 01 s sweep, and its check moved with it to 22:30):

| Principal | Full sweep | Check + heal |
|---|---|---|
| LJ | Saturday 08:00 | Saturday 22:30 (+14.5 h) |
| VJ | Sunday 10:00 | Sunday 12:00 (+2 h) |
| KJ | Sunday 14:00 | Sunday 16:00 (+2 h) |

One check per principal, placed at roughly twice the expected run length
(observed: LJ median 1.8 h / longest success **14 h** as of 2026-10-10; VJ
**4 h 32 m** as of 2026-10-11 — the `market_players` tracker runs last and has
widened VJ's tail, so a median-era bound sits *below* the principal's own
maximum and fires on healthy runs; KJ longest 1.8 h), and the watchdog's
`--max-runtime-minutes` sits just inside that offset (**900** for LJ, 105 for
VJ and KJ) so "still running when we look" is itself the variance.

That bound is hand-set in each principal's watchdog script and is therefore
also the easiest thing to get wrong. Since 2026-10-18 the watchdog does not
trust it alone (VJ fired `overdue` on three healthy sweeps — 2026-10-04, 10-11,
10-18 — and every packet ended in the same conclusion, "the bound is wrong"):
`run_watchdog.effective_runtime_minutes` floors the configured bound at the
principal's **own observed successful tail × 1.25**, and a run still in flight
inside that applied bound, with a live pid and a log written within the last
15 minutes, is `running_long` — a printed one-liner, no incident packet, no
model session. Two things still alarm exactly as before: a worker whose log has
stopped moving (process-alive plus a silent log is the shape of a hang, whatever
the bound says) and a run that outgrows the applied bound. Widening the
configured bound is still worth doing for a principal whose runs legitimately
take longer — it keeps the *printed* check honest — but a mis-calibrated bound
no longer spends an investigation.

The analogous contract can be configured for incrementals and future jobs.
The launcher's ``nohup`` acknowledgement is only evidence that a worker was
started; it is never evidence that the crawl completed.

#### Work location — the allow-list the exclude-list could not express (2026-10-09)

The Gradium canary: three 2026-10-03 listings at `- **Location**: Paris` scored
**GO 9.7** for LJ with **six packs prepared**, while LJ's own `work_auth.md` said
"EU-located roles — not a green signal on their own" (no EU free movement) and the
matcher's own reasoning said *"the Paris location is a material gate"*. The model
read the rule and scored it anyway: **an eligibility condition expressed as a
score penalty is not a gate**.

The cause is structural, not a missed regex. The location machinery was an
**exclude-list** (`lovework:work-auth-exclude-countries`, which VJ and KJ use),
and its eligible-region escape hatches name France and Germany on purpose (those
are the principals' remote markets). An exclude-list cannot say "UK and MK
only" — unless you enumerate the whole world.

The fix is the dual: an **allow-list** marker.

```
<!-- lovework:work-location-allowed: UK, MK -->     (profiles/example/work_auth.md)
```

`matcher._check_work_location_kill` reads it and hard-DROPs an advert whose
workplace sits outside the allowed places, with no remote signal and no allowed
place named — permissive by construction like its siblings: "Paris (remote
possible)", "Remote (worldwide)" and a mixed "Paris; London" are all left to the
model, an unrecognised place is never killed, and a profile without the marker
(vj, kj, pk) is untouched. The place vocabulary lives in
`lead_identity.classify_work_location`, shared by the kill, the wiki audit's
`ineligible_location` class and the link re-check — because a second spelling of
"where is this job" is how this ran for three weeks.

Measured backlog on 2026-10-09 (GO/MAYBE rows whose location sits outside the
rule): **lj 236 · vj 0 · kj 0**, pinned by
`tests/test_work_location_rule.py::test_the_real_lj_wiki_reports_the_class`.
Tomorrow's sweep is the first to be scored under the rule.

## The working day: world first, then the document (2026-10-08)

Between sweeps, the day has two halves, in this order — and the order is the
point (LJ: *"it makes sense to do any updates, any new fetches first, then
re-check the state of the principal wiki and correct everything there"*):

| When | Job | What |
|---|---|---|
| **06:00 daily** | `lovework-morning` (`lovework-morning.sh`) — **one job since 2026-10-11** (the 06:00 world pass and the 07:00 morning pass merged at LJ's word; `lovework-recheck` is paused, its pass runs first inside this script) | **the world, then the document**: per principal `link_check.py --sections GO --max-age-hours 168` (open the actionable links, record reachable / still-live / same-role; ~2–90 HTTP requests, no LLM) **then** case ingest → **self-heal** (duplicates, block shapes, page names, moved-page links, entities, placeholders) → watch → memory care → brief (its stdout) → trigger the pack job. The order is load-bearing: the self-heal's link step and the watch read the record the first step just wrote. |
| Tue + Thu | `lovework-<p>-link-recheck` | the **full** re-check (GO, MAYBE, FLAG, New Listings), placed ~3 h after the principal's sweep window |

So by ~07:30 the principal has an updated, checked, self-consistent wiki to work
from: what changed in the world is already in the record, and the 07:00 pass then
cleans a tree that knows about it. Both passes are idempotent (`link_check.py`
refuses a second verdict for the same report on the same day; the self-heal
re-run is a no-op), and both defer honestly when another operation holds the
principal's writer lease (exit 75).

Why the daily pass is GO-only: a closed **GO** is the expensive mistake — an
evening spent on a pack for an advert that has ended — while a dead FLAG is
noise. The twice-weekly pass covers everything. `--max-age-hours 168` (a week)
keeps the daily pass running between sweeps; the twice-weekly jobs keep 96 h
because they are placed just after a sweep.

Rollback for the 06:00 job: `hermes cron pause 729e2bab6b87` (or `remove`).

**The two link paths compose.** The 06:00 pass writes the machine record; the
watchtower reads it into the brief (hard problems first, then one summary line).
The link step inside the self-healing pass (`selfheal.run_links`) is the fallback
for the day that record is missing — it skips while a record younger than three
days exists, so on a normal day it does nothing and the two never both speak. Its
kind is `link_problem`, ranked with `link_recheck` (2.5).

**A rise must be PERSISTED to alarm.** `selfheal` computes which classes rose and
writes them into `cache/selfheal.json`; the watchtower reads that file and is what
puts `rising: <classes>` in the brief. Until 2026-10-08 the key was computed and
then dropped on save, so the alarm reached stdout and never the brief.

## The loop

```text
Expectation ──> Observe durable evidence ──> Compare
      ^                                         |
      |                                         v
Remember <── Verify <── Repair <── Investigate variance
```

1. **Expectation.** A small schedule contract names job type, weekday/time,
   grace period, maximum runtime, and required terminal evidence.
2. **Observation.** The worker writes an atomic record under
   `lovework-agent/cache/runs/`: start time, Hermes profile, log, terminal
   status, report path, and Gmail API message ID (or notification error).
3. **Comparison.** `run_watchdog.py` compares the most recent expected window
   with those records.  A completed report without a message ID is a variance,
   not a successful run.
4. **No variance.** The no-agent Hermes watchdog exits silently: no model call
   and no unnecessary Telegram message.
5. **Variance.** It atomically writes a JSON evidence packet and a readable
   incident under `cache/incidents/`, then prints a concise alert for Hermes to
   deliver on Telegram.
6. **Investigation.** An agent starts from that packet and asks the questions
   LJ would ask: Did the worker start? Is it still alive? What is the terminal
   state in its log? Is a report present? Did Gmail accept a notification? Is
   the configured HermeL profile/token the one actually used?
7. **Repair plan.** The agent identifies the broken contract, proposes the
   smallest root-cause repair, and adds a regression test or deterministic
   check that would catch the same problem next time.
8. **Repair and verify.** The gated HermeL investigator may implement and test
   a narrow runtime/notification/observability repair. Credentials, personal
   profiles, sources, match scoring, Git operations, and cron schedules are
   outside its authority and remain for LJ to approve.
9. **Remember.** Record the incident outcome in `JOURNAL.md`, the reusable
   rule in `LEARNINGS.md`, and keep the incident/evidence plus regression test.

This is self-maintenance at the **harness** level: the loop is fixed and
inspectable; its knowledge, tests, and safe repair playbook improve over time.
It is deliberately not an unrestricted self-modifying agent.

## Current implementation: principal-owned full-crawl contracts

`lovework-crawl.sh` is the worker-owned source of truth.  Before it starts the
pipeline, it writes `run_ledger.py start`; after the crawl and manual succeed,
it writes `finish` with the report path. Since 2026-10-18 the run sends no
post-crawl email (`notify.py --record-only`): the record's notification state is
`suppressed` plus the reason, and the state reaches readers through the Monday
state email instead. Runs that predate the change still carry a Gmail
sent-message ID, and `notify.py` remains the sender for the paths that do email
(the brief, the state email).

HermeL runs one check per principal, each an offset after that principal's own
sweep — `lovework-full-watchdog.sh` for LJ, `lovework-vj-full-watchdog.sh` for VJ
and `lovework-kj-full-watchdog.sh` for KJ (see the table above). The check covers
everything the earlier two-tick arrangement did: a crawl that finished without a
resolved notification state (`sent`, or `suppressed` with its reason), a crawl
that failed, and a crawl still running past its bound.
Empty output is intentional silence; any output is a Telegram-worthy variance.

The watchdog is deterministic and `no_agent`: it keeps the invariant cheap,
reliable, and testable. Five minutes after each watchdog check, that principal's
`lovework-*-meta-investigator` cron runs its pre-run gate. It wakes an agent
only when it can atomically claim a new incident; a repeated observation of
the same incident cannot spend another model session. The agent receives the
evidence packet and is authorised only for the bounded repair class above.

A run still in flight is reported in three ways, and the difference matters to
the gate: `running` (inside the configured bound — silence), `running_long`
(inside the bound derived from the principal's own tail, worker alive, log
fresh — one printed line, `wakeAgent: false`, no packet) and `overdue` (log
stalled, worker gone, or outgrown the applied bound — packet plus wake). The
packet now carries `Applied bound: N min (configured M min)` beside the
observed tail, so an investigator reads the derivation instead of repeating it.

## Operational interface

From `lovework-agent/`:

```bash
# Inspect the durable worker evidence, newest first.
ls -lt cache/runs/

# Check the Sunday full-crawl expectation now (silent = contract holds).
../venv/bin/python3 run_watchdog.py \
  --run-type full --weekday 6 --time 09:00 \
  --grace-minutes 15 --max-runtime-minutes 330

# Read an incident packet if the watcher reported a variance.
ls -lt cache/incidents/
```

The live scheduler configuration is HermeL's
`~/.hermes-host-a/profiles/profile-a/cron/jobs.json`; change it through the Hermes
CLI, never by hand-editing its active JSON file.

## Planned extension: evidence durability and coverage (2026-09-13)

Keep the current delivery contract, but expose independent execution,
coverage, capture-integrity, report-QA and delivery outcomes. A sent email
cannot prove successful source observation or durable history. One run lineage
must join the wrapper, pipeline, source attempts, sealed evidence and late
notification receipts; existing wrapper/pipeline IDs currently differ.

The planned [history protocol](26-asof-strata-history.md) seals failed and
interrupted attempts with honest partial coverage. Follow-up repairs/rechecks
create child records rather than altering sealed evidence. The
[agent cockpit](27-agent-ergonomics.md) reads these facts without network or
LLM cost. Wake the existing bounded investigator for a new actionable variance;
do not expand repair authority or resend on an ambiguous receipt.


## Self-healing: the agent immune system, deployed (2026-10-08)

The meta-loop above watches *scheduled work*. This section is the other half:
the wiki the work produces **heals itself**, daily, whether or not anyone
notices. LJ's framing, and the shape of the loop:

> Self-healing is like the immune system in the body, keeping the body's decay
> in check, fixing things internally — ramp it up, make it continuous, never
> stopping.

The six steps, and where each one lives in code:

1. **Assume faults exist; search for them.** `wiki_audit.py --profile <p>`
   reports defect classes, not instances, and separates a **defect** (we
   produced it wrongly and can put it right) from a **gap** (a limit of the
   evidence: a redirect URL, a listing whose source never captured a link).
2. **Drill into a fault: how, and why did nothing prevent it.** The forensics
   are kept as artefacts (`tasks/defect-forensics-*.md`,
   `tasks/defect-investigation-*.md`) rather than as memory: a cause chain is
   evidence, and the next session must be able to re-read it.
3. **Look down from the cause: the maximum blast radius.** Every class entry in
   `LEARNINGS.md` states it (`5,706 redundant blocks`, `16,024 dead links`,
   `14,060 links across a principal boundary`) — the size is what decides
   whether a class fix is worth a migration.
4. **Establish the wider class.** The recurring shape is *one authority per
   fact, one writer per document kind*: two fold keys for a page name, two block
   schemas for one document, two repair tools on two hosts, a default argument
   that names one principal.
5. **Fix the class, then prove the example is fixed for good.** Prevention at
   the writer, a one-time idempotent repair, and a test that fails without the
   fix (`tests/test_wiki_defect_classes.py`, `test_selfheal.py`).
6. **Detect it tomorrow, and raise the alarm.** `selfheal.py` measures on every
   pass and compares with the last one; a class that **rises** is the alarm even
   when the total falls. `shoulder.watch_wiki_defects` reads that measurement
   (one authority — the watch does not run a second audit) and puts the rising
   classes in the daily brief.

### The pass

```bash
cd lovework-agent
../venv/bin/python3 selfheal.py --profile lj            # measure and plan
../venv/bin/python3 selfheal.py --profile lj --apply    # heal
../venv/bin/python3 selfheal.py --all --apply           # every principal
```

Exit codes: `0` clean, `1` defects remain, `2` a class rose since the last pass,
`75` another operation holds the principal's writer lease (retry later). The pass
holds that lease itself, so it cannot race a crawl or a seal.

**Order is part of the contract**, which is why one door runs both repair tools:

| step | tool | why here |
|---|---|---|
| names, urls | `wiki_org_repair.py` | moves pages (rename / merge / fold / retitle) |
| rename map | `wiki_org_repair.py --rebuild-map` | every move must be recorded, or the sealed reports' links die |
| shapes | `wiki_repair.py --all` | report-block and note normalisation, entity decode, duplicate collapse |

Run the other way round and a fold re-creates the duplicate identity the dedupe
had just collapsed: `duplicate_org_blocks` went 0 → 9 within the hour on
2026-10-08.

### Continuity

`lovework-morning.sh` (the 07:00 job, all principals) runs the pass with
`--apply` **before** the watch, so the brief reports the healed state and the
`wiki_defects` event carries the post-repair count. A contended pass prints
`DEFERRED` and is not a failure; an alarm prints `SELF-HEAL ALARM`.

The same pass must be run **after any merge or import** of another host's state.
A repair is host-local until it is re-applied: host-b and host-a each repaired
their own copy of the same pages, and the merge unioned them, so the duplicates
host-a had collapsed came back (2026-10-08). Idempotent repairs make this cheap —
re-running is a no-op when there is nothing to do.

### What is deliberately NOT repaired

- **Sealed reports are never rewritten.** The seal hashes `outputs/`; their
  links are resolved at the serving layer (`doc_serve.resolve_wiki_org` + the
  per-principal `org-renames.json`) and the residue is counted, not edited.
- **Historical findings keep their own words.** A block whose title is
  `Full-time` cannot be repaired — nobody knows the real title — so the class is
  gated forward and the count stands.
- **Long prose locations are reported, never stripped.** Removing the text takes
  "US" out of the work-authorisation hard-kill's view.
- **`wiki/_attic/` is quarantined, and reported as a gap.** Its blocks were
  migrated into the pages that *are* audited, so auditing the residue would
  double-count; `--include-attic` asks for the full scope.

### The detector's own hygiene

A wrong detector is worse than none. Each class ships a fixture with a
known-positive **and** a canonical page that must produce zero classes, and the
link checks ask the same functions the dashboard serves with rather than a
mirror of them. Two false classes came out of the first pass of this loop alone
(a regex that reported 13,247 phantom defects; a rule that counted markdown's
closing parenthesis as URL punctuation), and a "100% repaired" claim that was
true only in the shape its unit test used.
