# Chapter 14 — Operational Meta-Loop

> **Audience:** LoveWork operators and agents maintaining the system.
> **Purpose:** make scheduled work observable, testable, and repairable rather
> than requiring LJ to remember to chase a missing outcome.

## The invariant

For each scheduled crawl, LoveWork holds an explicit operational contract:

> A **full** crawl is expected every Sunday at 09:00 local time.  It must end
> with either a Telegram-visible failure or a Gmail completion notification
> whose Gmail API message ID is recorded.

The analogous contract can be configured for incrementals and future jobs.
The launcher's ``nohup`` acknowledgement is only evidence that a worker was
started; it is never evidence that the crawl completed.

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

## Current implementation: full-crawl contract

`lovework-crawl.sh` is the worker-owned source of truth.  Before it starts the
pipeline, it writes `run_ledger.py start`; after the crawl and manual succeed,
it writes `finish` with the report path. `notify.py` uses the active Hermes
profile's Gmail API with `--body` and accepts success only when Gmail returns a
sent-message ID. That ID is persisted with the run record.

HermeL runs `lovework-full-watchdog.sh` at 13:30 and 15:00 each Sunday.  The
first check catches a completed crawl with a missing notification promptly;
the second catches a crawl that was still running at the first check but later
failed or exceeded its 5.5-hour bound. Empty output is intentional silence;
any output is a Telegram-worthy operational variance.

The watchdog is deterministic and `no_agent`: it keeps the invariant cheap,
reliable, and testable. Five minutes after each watchdog check, the
`lovework-full-meta-investigator` cron runs its pre-run gate. It wakes an agent
only when it can atomically claim a new incident; a repeated observation of
the same incident cannot spend another model session. The agent receives the
evidence packet and is authorised only for the bounded repair class above.

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
`~/.hermes-gigul2/profiles/hermel/cron/jobs.json`; change it through the Hermes
CLI, never by hand-editing its active JSON file.
