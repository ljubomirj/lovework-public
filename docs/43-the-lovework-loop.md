# Chapter 43 — The LoveWork loop: tick, decide, act, receipt

> **Audience:** builders + LJ. **Status:** the decider (`decide.py`) and the
> runner (`tick.py`) are implemented, and the tick is **live on host-b** every
> hourly in `--mode act --apply` (one tick per principal, all three per run).
> §9 records the
> defects the first live run produced and how each class was closed.
> **Companions:** [42](42-the-lovework-agent.md) (the RLM charter this serves —
> §3.2 "the deterministic skeleton stays", §5 P1), [27](27-agent-ergonomics.md)
> (the doctrine the loop obeys: P3 cheap reads, P6 residue, P7 verification, P9
> authority), [34](34-agent-system-plan.md) (AS2, the action catalogue sketched
> in §5 below), [35](35-the-watch.md) (the loop the agent lives inside),
> [41](41-live-pack-revalidation.md) (the first mission).

---

## 1. What this chapter adds

[ch42](42-the-lovework-agent.md) rules that LoveWork grows an RLM agent and that
the deterministic skeleton stays on cron. This chapter is the concrete shape of
that skeleton-plus-agent boundary:

```text
tick (cheap, deterministic, no model call)
  → decide.py: what is due, ranked by the principal decision it could change
  → nothing due?          exit 0, one receipt line — the common case
  → something due?        ONE bounded loop, one tool, doctrine from the repo
  → close: receipts, the principal's half surfaced, honest exit
```

**The tick is not a new watcher.** `shoulder.py` already runs ten deterministic
watches with per-watch cursors, a `watch_completed` heartbeat, per-kind caps and
the writer lease, appending to `dataset/events.jsonl`. What was missing is the
*decider* — the thing that turns "here is what changed" into "here is what is
worth acting on, and why". A second watcher with its own cursors would be a
second authority for one fact (docs/27 §4b) — the defect class this project
spent a whole round removing from the wikis.

## 2. The rule that shapes the decider

**No decision, no loop work.** ch27 §7: choose work by expected improvement to a
principal decision, and stop when no observation can change it. So every item
the loop may act on names the decision it could change. An item that cannot name
one is not loop work; `decide.py` reports it in a separate `maintenance`
section, where it can never outrank a dated clock.

The decision text is **read, not invented**: DUE.md's table is
`When | What | Consequence if missed | Next action | Owner`, and
`situation.due_projection` now carries all five cells. The consequence *is* what
the clock protects; the next action is the declared step; the owner is the
declared authority.

```console
$ decide.py --profile lj --status
lj: due 24 (principal 6, earliest 2026-10-15) maintenance: 1
$ decide.py --profile all --status        # `all` cycles the principals with state
kj: due 15 (principal 1) maintenance: 2
lj: due 24 (principal 6, earliest 2026-10-15) maintenance: 1
vj: due 15 (principal 1) maintenance: 2
```

`--profile all` is a *selector*, not a principal name, and it was passed straight
through until 2026-10-11: `decide` answered for a principal called "all" (no
state, so only the repo-level clocks) and printed one line that looked like a
real answer. An unknown principal is now refused with exit 2 — absent is not
empty — and every status line names the principal it is about.

## 3. Authority: three values, not two

| Owner cell in DUE.md | Authority | Who acts |
|---|---|---|
| `LJ` | `principal` | the principal alone; the agent may prepare and must surface it |
| `agent`, `engine`, `host-a cron` | `agent` | the loop may act |
| `LJ + agent` | `both` | the loop does its half; the principal's half is surfaced |

The loop acts on `agent` and `both`, never on `principal`. That boundary lives
in the payload, so it is checkable by code and not by reading a prompt.

## 4. The outcome vocabulary

The loop's day ends in exactly one of these, and each is a receipt:

| Outcome | Meaning | What it leaves |
|---|---|---|
| `quiet` | nothing due that could change a decision | one line, no model call |
| `acted` | a bounded action completed and was verified | intent → progress → effect → verification |
| `needs_principal` | the honest next step is the principal's | an item in the brief, never silence |
| `blocked` | proven blocked with host-observable evidence | a ledger row or an exit code, never an assertion |
| `deferred` | a lease is held, or a dependency is absent | the reason, and a wake condition |

`needs_principal` is the loop's most valuable output, and the first version of
the decider got it wrong: it listed one item per prepared pack and returned 401
"due" items for LJ, 384 of them "needing the principal". The pack corpus is
~400 packs; the decision is "which of these to pursue first", so the item is
**aggregated and bounded** — one row per principal, inside the matcher's own
10-day staleness window.

## 5. The action catalogue (ch34 AS2, sketched)

Every action the loop may take is typed. The catalogue is the contract; the
decider emits the action *name*, and the loop refuses anything not in it.

| Action | Class | Authority | Write set | Idempotency | Verification | Recovery |
|---|---|---|---|---|---|---|
| `read` | read | agent | none | trivially | the reader returns a value, not an exception | re-read |
| `local_projection` | local projection | agent | a regenerable projection (`STATE.md`, `MANUAL.md`, an index) | regenerate is idempotent | the generator's own exit code + the file's mtime | regenerate again |
| `infer` | inference | agent | a judgement row (assessment, investigation block) | keyed on the advert hash | the row exists and cites its evidence | re-run, or leave `unknown` |
| `acquire` | acquisition | agent | cached pages, registry rows | keyed on the URL/hash | the fetch reports its provider, or says `unreachable` | retry once, then record the failure |
| `investigate_pack` | inference | agent | the pack's investigation block | keyed on the pack + advert identity | the block is filled and cites fetched URLs | leave `uninvestigated` and say so |
| `complete_pack` (the **GO queue**) | inference + authored material | agent | the pack's review, investigation, checklist and tailored material | keyed on the pack: an artifact that already exists is left alone, so the workflow is idempotent by construction | `pack_readiness` says the pack carries `cover-letter.md` and `cv-tailored.md` **and** a filled investigation block | re-run; a pack that can never be prepared (no advert URL, no retained text) goes to `leads/` instead (`retire.retire_unpreparable`) |
| `review_go` | inference | agent | `loop/reviews.jsonl`, and — for a material move, with `--apply` — a `review.md` beside the pack's dossier | keyed on the pack **and the fetched advert's bytes**: an unchanged advert is never re-scored | the review row exists with the old score, the new score and the evidence hash; the note cites the page it fetched | record the row and write nothing into the pack (the next cycle re-reads it) |
| `mutate_principal_state` | principal-state mutation | **principal** | a proposition's status | transitions are append-only events | the folded state changed | never automatic |
| `external_effect` | external effect | **principal** | an application, an email, a letter | none — the principal holds it | a receipt the principal produces | never retried by the agent |

Rules that hold for all of them:

- **The loop never sends.** Mail, telegram, submissions and letters are the
  principal's act; the agent composes and surfaces (MANIFESTO, ch24, ch31).
- **Every effect takes the writer lease**, and holds it only around the effect
  phase — investigate outside it, revalidate, then publish (Codex's §3 in
  ch42 §7, adopted). Implemented 2026-10-11: `standalone_writer_guard` wraps the
  `--apply` write only, and contention is a `deferred` receipt with **exit 75**
  (the house vocabulary), not a failure and not a silent write.
- **Every effect carries a receipt** naming the principal, the action, the
  inputs' fingerprints and the verification. An artefact class without a
  detector is not admissible (ch42 §7 point 2).
- **`run_python` is bound to one principal by construction.** The tool takes a
  runtime, and a call that can reach another principal's state is a defect, not
  a bug — this class has already recurred once (LEARNINGS 2026-10-02,
  2026-10-10).

## 6. The daily envelope

A per-run cap is not a bound: N ticks times a per-run cap is unbounded. The
bound is **per day**, and it is unset today — `decide.py` says `UNSET` rather
than inventing a number:

| Variable | Meaning |
|---|---|
| `LOVEWORK_LOOP_DAILY_CALLS` | model calls the whole day may spend |
| `LOVEWORK_LOOP_DAILY_MINUTES` | wall clock the whole day may spend |

The envelope is reported in the tick's receipt, so the burn is visible without
reading a log.

One host, one envelope: `envelope-<date>.json` is git-tracked and named by date,
so two ticking hosts would each spend a full day's budget on the same day and the
merge would keep one of them. The host rule (§7 — the tick runs on host-b and
nowhere else) is what makes the file honest; if a second ticking host is ever
added, the envelope needs a host component in its name before that happens.

## 7. Wiring it — host-a, live (2026-10-11)

> **Moved 2026-10-11: the tick stopped on host-b and moved to host-a** (LJ:
> keeping two hosts in step cost more than the interference it avoided). The
> handover checklist is [ch44](44-tick-handover.md); this section is the
> standing record. host-b's launchd job is stopped — `bootout`, `disable`, and
> the installed plist moved aside — and the plist in `lovework-agent/` is kept
> as the record of how it ran there, not as a deployment step.

**Host rule.** The tick acts on **host-a** and nowhere else (`LOVEWORK_TICK_HOSTS`,
default `host-a`, substring-matched so `host-a.local` passes). It reads this
host's state, writes only `state/<p>/loop/` and — with `--apply` — the pack it
owns. `--profile all` cycles the principals that have state.

```console
$ tick.py --profile all --mode status     # decide only: no model call, no writes
kj: surfaced · due 15 · principal 1 · status mode: the decider answered, nothing was acted on
lj: surfaced · due 25 · principal 6 · status mode: the decider answered, nothing was acted on
vj: surfaced · due 16 · principal 1 · status mode: the decider answered, nothing was acted on
```

**The tick now shares a host with the schedule, so it has three guards — all in
code, in this order.**

| Guard | What it asks | Why it exists |
|---|---|---|
| **The writer lease**, *probed* | Does `state/<p>/strata/writer.lock` answer to another `flock`? | A crawl, a seal, a consume or the brief holds it while it writes. Probing takes nothing and creates nothing — no `O_CREAT`, so a principal with no lock file is simply free and nothing is written into `strata/` (ch42 §3.3). |
| **Running work** (`/proc`) | Is any LoveWork job running *now* — `crawl_operation.py`, `main.py`, `consume.py`, `brief.py`, `shoulder.py`, `selfheal.py`, `link_check.py`, `run_watchdog.py`, …? | **This is the guard a time window cannot give**: it sees a job that overran its hours. Read from `/proc` rather than shelling out to `ps` — one authority for one fact, and cheap enough hourly. The tick excludes itself — one cron run ticks three principals, and a scan that saw its own process would defer after the first. |
| **The skip windows** | Is it an hour the schedule occupies? | The coarse guard, read from the Hermes cron store: `sat:07-23` (LJ's sweep starts 08:00 and runs 1.8–8.8 h, plus the 22:30 watchdog and meta-investigator), `sun:06-20` (the consumes at 06:30/08:30 and the VJ/KJ sweeps 10:00–17:00 with their watchdogs), `daily:05-09` (the 06:00 chain and the pack job it triggers). |

A deferral is a *result*, not a failure: `deferred · busy: a writer holds lj`,
`deferred · busy: crawl_operation.py is running (pid 1234)`,
`deferred · crawl window sat:07-23: the GPU is the crawl's`, exit 0. `--force`
bypasses both locks, for the case where a human wants it now.

**One job, hourly** — `lovework-tick.sh` under Hermes cron on host-a (no-agent:
the script's stdout is delivered verbatim, and empty stdout is silent, so a quiet
day costs nothing). **One run ticks every principal** — the script loops over
`lj vj kj`, so a run is up to three actions, three lines, one moment to look at.
Hourly rather than every 30 minutes (LJ, 2026-10-12: *"if 3 principals/tick then
we should tick every 1 hour instead of 30 mins — that's easier to keep track
of"*): the cadence is a human interface as much as a cost knob, and 24 runs a day
that each cover all three principals is easier to reason about than 48 that
cover them in rotation.

| Setting | Value | Why |
|---|---|---|
| mode | `act` | a tick that never acts is a status line |
| `--apply` | **present since 2026-10-11** | LJ: *"go ahead apply all the drafts yourself"* — the tick fills the pack's investigation block as well as drafting it. `LOVEWORK_TICK_APPLY=0` returns it to drafts-only |
| `LOVEWORK_LOOP_DAILY_CALLS` | 24 | per principal per day (set in the launcher — `hermes cron create` has no env flag). 24 runs a day minus the skip windows leaves ~17 ticks a day per principal, so 24 is a bound that can actually bind. LJ: *"12/day is NOT too much … we should use it more — rather than let it sit idle"* |
| `LOVEWORK_LOOP_DAILY_MINUTES` | 60 | per principal per day — ~17 actions × ~70 s is comfortably inside it |
| `LOVEWORK_TICK_SKIP_WINDOWS` | `sat:07-23,sun:06-20,daily:05-09` | the hours the Hermes store says the schedule occupies |
| delivery | `all` | a tick that acted, needs the principal, is blocked or failed prints one line; everything else is silent |

Rollback: `hermes cron pause lovework-tick` (a paused job cannot fire).
**The `hermes` invocation matters**: the live gateway runs with
`HERMES_HOME=~/.hermes-host-a/profiles/profile-a` — the *profile* directory, not the
home root. With the home root the CLI creates jobs in a store nothing reads and
reports a "STALLED" ticker that is running perfectly well (both measured
2026-10-11).

**The inference is the crawl's, over the relay** (changed 2026-10-21 — §14). The tick
uses the OpenCode Go `opencode-go` subscription exactly as the crawl does:
`deepseek-v4.1-flash`, `reasoning_effort="max"`, key from
`~/.lovework/credentials/opencode-go.key`. Until 2026-10-21 it used the LAN
endpoint (`192.0.2.10:8081`, Strata serving `qwen3.8-flash-next-iq3_xxs`, 256K
context, no key) so a tick spent no relay tokens; measured then, a full investigation
was ~3,000 completion tokens and 48 s at default effort. That history is kept because
the numbers explain the old cost model — the relay now bills both paths.
Measured end to end on host-a's first live run: two acted principals, **2 m 13 s**
for `--profile all`.

**Fetching uses Firecrawl** (`LOVEWORK_FETCH_PROVIDERS` default `firecrawl,http`
since 2026-10-26, when Tavily's monthly quota ran out; Exa is blacklisted since
its credits ran out, 2026-10-22. Tavily stays wired and returns by naming it), at
most two URLs per action: the advert, then the company's front page — and no
second page at all when the advert is hosted by an ATS or a board
(`_site_root`), whose root is the board. The key resolves from `FIRECRAWL_API_KEY`
in `.env` *or* `~/.lovework/credentials/search/firecrawl_api_key`: cron does not
source `~/.bashrc`, so the credential file is what a scheduled job uses (verified
present on host-a, 2026-10-11).

**The tick does not write the sealed tree.** `cache/` *and* `dataset/` are
captured by a seal, so a tick artifact landing in either would be captured as if
it were crawl evidence. The action fetches with `use_cache=False`; its residue
stays in `state/<p>/loop/`: `receipts.jsonl` (acted ticks only — a read-only
tick writes nothing, ch27 P6), `envelope-<date>.json`, `drafts/`, and
`transcripts/model_calls.jsonl`. The transcripts are bound with
`model_transcripts.active_dataset_dir` because the unbound fallback is
`config.DATASET_DIR` — **lj's** — so vj's and kj's tick calls were being written
into LJ's ledger as crawl evidence (12 rows, measured 2026-10-11; see §9). Each
tick stamps `LOVEWORK_OUTER_RUN_ID=tick-<profile>-<timestamp>` so its calls are
attributable rather than `unattributed`.

**The loop's product has a reader.** `loop/` is a published dashboard area, so
`state/<p>/loop/drafts/<pack>.md` and `receipts.jsonl` are openable at
`/principals/<p>/loop/…`. Before 2026-10-11 nothing served them: a draft nobody
can open is work nobody can use.

```console
$ bash ~/.hermes-host-a/profiles/profile-a/scripts/lovework-tick.sh   # the job, by hand
lj: acted · due 25 · principal 6 · investigate_pack · draft only
kj: acted · due 16 · principal 1 · investigate_pack · draft only
vj: blocked · due 16 · principal 1 · investigate_pack · nothing could be fetched from _not
$ tail -1 state/example/loop/receipts.jsonl
{"action": "investigate_pack", "calls": 1, "effect": "draft only", "seconds": 81.15, …}
```

That first run on host-a is also where two smaller defects surfaced: a pack whose
`Link` cell says `_not retained_` was read as the URL `_not` (so the tick asked
the fetcher for "_not" and the library's error line reached the delivered
output), and the launcher passed the whole captured output through instead of
the tick's own line. Both fixed — a link needs a scheme to be a link, and the
launcher greps for `^<principal>: ` before delivering.

**The loop is point-in-time too (LJ's ruling, 2026-10-11).** `loop` joined
`strata_contract.INCLUDE_TREES`, so every seal now carries the tick's
`receipts.jsonl`, `envelope-<date>.json`, `drafts/` and `transcripts/` — "what
did the tick do as of date D" is answerable from a bundle like every other
point-in-time question. Two deliberate details:

- **The transcripts are sealed with the rest**, on the same standard the crawl's
  own model calls are held to: they are what makes the loop's calls replayable
  from a bundle (~23 KB per call; at 40 calls a day that is under a megabyte a
  day beside the 126 MB ledgers a seal already copies).
- **The seal records the loop's *claims*, not its effects.** A filled pack lives
  in `applications/`, which stays out of the bundle: the bundle says what the
  tick concluded and wrote, and the pack remains a living artifact.
- `loop/ledger-scan.json` is excluded — its content is a file size and an mtime,
  host-local runtime that would make two identical states differ.

**Open, deliberately.** The windows are *read from* the cron store, not derived
from it at runtime (ch44 §7.2): the locks are the precise guard, so a new job
does not need a window edit to be respected.

**The tick and the 06:00 pack job do not collide.** The job follows
`tasks/2026-10-03-pack-completion-workflow.md` for **at most two packs a run**
(review → investigation → tailored material); the tick fills the *investigation
block* of the packs the job has not reached. A pack the tick has filled loses its
`NOT YET INVESTIGATED` marker, so the job's monitor stops calling it
unreviewed and still asks for `cover-letter.md` / `cv-tailored.md` — the two are
complementary by construction, and the split is why the queue moves at all: 362
packs pending, two a day by the job.

## 8. What would change this ruling

- **The decider proves uninformative** — if it cannot name a decision for most
  items, the loop has nothing to serve and the maintenance pass is the honest
  answer.
- **The envelope cannot tame the cost** (ch42 §6, condition (b)).
- **A principal's boundary needs a different shape** (docs/15): per-principal
  shims and markers, still one tool.


## 9. The first live run, and what it cost (2026-10-11)

The tick ran live on host-b for the first time — 8 acted ticks across three
principals — and the residue was examined the way ch42 §7 point 2 requires: an
artefact class without a detector is not admissible. Every defect below was
found on disk, fixed at the writer, and is now covered by a detector
(`tick.py --audit`) and a regression test with a known-positive and a
known-negative.

| # | What the run did | Class | Fix |
|---|---|---|---|
| 1 | **Every receipt named a decision the tick had not served.** lj worked `z0.ai` while the receipt said "amiengineer at advancedmachineintelligence…"; vj worked `nology` while it said "pricingdataanalyst at abfgroup"; kj worked `deepmirror` while it said "computationalchemist at aqemia". | Two authorities chose the work: the decider picked the item, the action picked its own pack. The receipt — the verification record — was false for every tick. | The item carries `target`; the action works *that* target; the receipt reports the pack it worked. |
| 2 | **The same pack was investigated again and again** — lj three times in 65 minutes, each a full model call — while the corpus stood still. | The progress marker (the dossier's `NOT YET INVESTIGATED` block) only moves with `--apply`, which is off. A draft-only tick therefore repeated itself until the envelope ran out. | The **draft is the progress marker**: a pack drafted inside `LOVEWORK_TICK_DRAFT_FRESH_DAYS` (14) is not work, and the honest outcome becomes `needs_principal` — the drafts are the principal's to read. |
| 3 | **The tick's transcripts went into the crawl's ledger**, inside the sealed tree — and vj's and kj's rows went into **LJ's** dataset. | `model_transcripts` falls back to `config.DATASET_DIR`, a module-level default that is lj's; and `dataset/` is captured by a seal, the very class §7 avoids for `cache/`. | `active_dataset_dir(loop/transcripts)` around the model call; the 12 stray rows were moved into each principal's `loop/transcripts/`, with the original ledger archived to `~/.lovework/ledger-backups/` (outside the repo — a 126 MB untracked copy in the state tree is one `git add -A` from the history). |
| 4 | **An "INVESTIGATED" draft with nothing behind it** (vj's iwoca: no fetch, twelve fields, all `unknown`). | A model asked with no evidence answers from its priors, and the artefact claimed verification it did not have. | No evidence (no advert link, or nothing fetched) → `blocked`, no model call, no draft. The two existing drafts are marked `UNVERIFIED — no page could be fetched`. |
| 5 | **The second fetch was a marketplace index**, not the company: `apply.workable.com/` returned 1 char, `linkedin.com/` 5,001. | `_site_root` assumed an advert's host is the employer's; for an ATS or a board the root is the board. | `_site_root` returns `""` for a known ATS/aggregator host, so the budget is not spent on someone else's index. |
| 6 | **An unreadable pack queue silently widened the work**: `except Exception: queued = set()` plus an `or pending` fallback licensed every pending pack. | A swallowed exception changed behaviour rather than reporting it. | `PackQueueUnavailable` propagates; the tick defers with the reason. |
| 7 | **The writer lease was doctrine only** — §5 said every effect takes it, and no code did; exit 75 was documented and unreachable. | Prose that the code does not implement is worse than absent prose: it is trusted. | `standalone_writer_guard` around the `--apply` write; contention → `deferred` + exit 75; the plist's claim about the skip windows was corrected too. |
| 8 | **The loop's product was invisible** — no dashboard area, no brief mention. | An artefact class with no reader. | `loop/` published as a principal area. |

**The tenth was the wiring itself.** `com.lj.lovework-tick.plist` was not
well-formed XML: the header comment wrote the flags literally, and `--` is
illegal inside an XML comment. `launchctl` cannot load such a file, so the loop
was never actually scheduled — the eight ticks on disk were runs someone
started by hand. The comment now spells the flags in words, `tests/test_tick.py`
parses the plist and asserts its bounds, and the host rule is declared there
(`LOVEWORK_TICK_HOSTS=host-b`). **A shipped artefact that cannot load is an
artefact class**, and it is exactly the kind that a detector catches and a
human reading the file does not.

**The ninth defect was mine, and it is the one worth remembering.** Repairing #3
rewrote a 126 MB `model_calls.jsonl` with `read_text().splitlines()` and
`"\n".join(...)`. `str.splitlines()` also breaks on the Unicode separators a JSON
string may legally contain (`\u2028`, `\u2029`, `\x0b`, `\x1c`…), and the ledger
is written with `ensure_ascii=False`, so those characters are in it literally:
**7,012 valid rows became 6,963 valid + 50 fragments**, with raw newlines inside
strings. The detector caught it (a parse check), the original bytes were restored
from the backup the repair itself had written, and the repair now reads physical
lines and **refuses to rewrite a file it cannot account for line by line** —
the same gate a model file gets before any work is done on it.

Two rules came out of that:

- **A line-delimited ledger is rewritten on `\n`, never on `splitlines()`.**
- **A repair that cannot reproduce what it promised puts the original bytes back
  and says so.** `repair_loop_residue` verifies the row count it expects after
  writing and restores the backup if the file disagrees.

### What the run got right

Not everything was a defect. The evidence discipline held: the Xelix draft cites
only the advert it fetched, says "exact legal entity details are not stated in
the evidence", and puts ten items in `unknown`; `confidence` came back
`moderate`/`low` rather than inflated. The skip window proved itself live (a tick
inside Sunday's sweep deferred with `crawl window sun:09-18: the GPU is the
crawl's`), the envelope counted what it spent, and the whole action cost ~50 s
and one local call.

### Running the checks

```console
$ tick.py --profile all --audit     # receipts vs what they claim; exit 1 on a defect
lj: 4 receipt(s), 0 defect(s), 4 corrected
vj: 2 receipt(s), 0 defect(s), 2 corrected
kj: 2 receipt(s), 0 defect(s), 2 corrected
$ tick.py --profile all --repair --dry-run   # what the idempotent repair would change
```


## 10. The controls the charter asks for, and where they now stand

[ch42 §7](42-the-lovework-agent.md) records Codex's three weaknesses to fix
before P1, and the first live run reproduced two of them in the concrete:

| Weakness | What the live run showed | Now | Still open |
|---|---|---|---|
| **1. The Python shim is not an enforceable authority boundary** — "cross-principal reads and writes denied outside the model's control" | vj's and kj's tick calls were written into **LJ's** `dataset/model_calls.jsonl`; the sandbox tool resolved its registry from the module default. Both are the same class, and both were live. | The tick binds its transcripts to the principal's `loop/transcripts/`; the action works a target that carries the principal; the sandbox tool takes a runtime and resolves the profile by name when it is absent; receipts store paths relative to the repo root so the other host can check them. | The isolated workspace and the trusted action service (Codex's fix) are **not built**: the tick is still Python with host permissions, and the action catalogue is a dict in `tick.py`, not a validating service. |
| **2. RLM without a specified recursive mechanism** | The investigation is one model call with two fetches — no recursion, no child calls. | — | The 20-case comparison (plain agent vs recursive) has not been run. |
| **3. Unattended execution before the controls to survive it** — "interrupt before, during and after publication; resume without duplicate effects; cap the whole session" | The envelope worked, but a draft-only tick repeated the same pack until the envelope was gone, and the write into the dossier was a truncate-then-write. | One action per tick; a per-day envelope that says which bound applied; the draft is the progress marker, so an interrupted tick's next run skips the pack instead of repeating it; the `--apply` write takes the writer lease and holds it only around the effect; the dossier and draft writes are atomic (`os.replace`); a torn envelope is **spent, not reset**; a draft with no receipt is a reported defect (`draft_without_receipt`). | The seven-morning canary, and the interrupt-during-publication rehearsal, are LJ's to run. |

**The measured cost of the first live run** (8 acted ticks, three principals, 8
model calls, ~7 minutes of wall clock in total): the loop spent its whole budget
re-investigating two packs, wrote twelve rows into the wrong ledger, and
produced two drafts that claimed an investigation nothing backed. Every one of
those is now either impossible or reported by `--audit` — which is the standard
the charter set: **an artefact class without a detector is not admissible.**

## 11. The second mission: reviewing the GOs (2026-10-18)

LJ, 2026-10-18: *"after that - to be reviewing existing GOs and refining them, WE
want high quality, high SNR."* Every GO has a pack (lj 49/49, vj 22/22, kj 4/4),
so the loop's pack work is done and the loop had gone quiet — nothing due, every
tick a status line. What is left is the **quality** of what is already on file,
and the model has room for it.

`review_go` is one GO, re-read: fetch the advert live, re-score it with the
advert as the evidence (the matcher's own chain, minus the wrappers that write
into the sealed tree), and record what moved.

**Which GO, and why that one** — ranked by the principal decision it could
change:

1. **GOs whose score never saw the advert's own text.** The highest value,
   because part of that reasoning was inferred rather than read. Measured on
   PostHog: the title/list path gave 8.9 with an invented rationale, and the same
   advert read in full gave 8.2 with a grounded one. The fact comes from the
   pack's own dossier (`Primary evidence`), which the matcher sets only when it
   judged the fetched advert page.
2. **Then the oldest review**, so the corpus rotates instead of one GO being
   re-read forever.
3. **Then the fewest reviews.**

The candidate set is the index's own GO view (`WikiStore.current_view`), so the
queue is exactly the GOs the principal can still see; a retired or re-judged
advert is not reviewed as if it were current, and a GO with no pack stays with
`prepare_missing_packs` (its `go_without_pack` defect class).

**A review is not a re-verdict.** The pack's dossier keeps the judgement the
principal was told; the review is a note beside it (`review.md`), and it says so.
A verdict is a prohibition (ch45 §3.2) — the loop does not get to move a GO by
re-scoring it.

**What it costs, and what bounds it.** One fetch and one local model call
(no relay tokens), against the same daily envelope as every other action; the
`loop/reviews.jsonl` ledger is the progress marker, so a GO reviewed inside
`LOVEWORK_REVIEW_FRESH_DAYS` (14) is not work, and an advert whose bytes are
unchanged since the last review is not re-scored at all — the loop has nothing
new to say, and what it said is the principal's to read.

**Material means material.** A note is written only when the move clears
`LOVEWORK_REVIEW_MATERIAL_DELTA` (2.0) or the *tier* changes. The threshold is
the measured cross-model per-advert spread (`calibration.py`: ±1.9 between two
relay models on the same corpus, decision agreement 0.65–0.72) — a smaller move
is not distinguishable from the model's own noise. The note names the model and
its calibration offset, because the review runs on the **local** model, which has
no measured offset yet: the honest thing is to say so beside the two numbers
rather than to imply they are on one scale.

---

## 12. The slot doctrine: drain, then yield (2026-10-18)

LJ, 2026-10-18: *"'Tick hourly' is fine, '1 or X action per tick' where we allot
fixed amount of work (that may take 1 second even!) and then we go back to sleep
do nothing with the time is not. We should have aim: 1. keep processing the queue
of X actions, until the queue is empty, or until we are close to 1h slot
expiring; if latter, stop and yield the time to the next tick. So this is like
scheduling the cpu by the os kernel scheduler."*

The tick's run form changed that day from a fixed quantum to a **slot**. The
default CLI run (`--max-actions 0`, the new default) is the draining form:

- **Keep processing the queue** — the same decide → one action → receipt loop,
  back-to-back — **until the queue is empty** (the principal goes quiet and
  leaves the rotation) **or the slot is about to expire** (the next hourly
  boundary at `:05`, less `LOVEWORK_TICK_SLOT_MARGIN_MINUTES` = 3 minutes, so
  the next tick starts with the run lock free and the receipts written). Then
  stop and yield.
- **Round-robin across principals** — one action per principal per cycle, so a
  principal with a deep queue (lj's 172-pack pile) cannot starve the others,
  and a quiet principal costs nothing. This is the kernel-scheduler fairness
  LJ named: processes, quanta, preemption at the slot boundary.
- **Every guard still guards per action**: the writer lease, the busy-process
  markers, the skip windows, the daily envelope. The envelope changes *role*,
  not size semantics — with the slot as the working bound, the envelope is the
  runaway net. The wrapper's env on host-a should carry numbers the slot can
  actually use (`LOVEWORK_LOOP_DAILY_CALLS=1200`, `LOVEWORK_LOOP_DAILY_MINUTES=1200`
  are the recommendation; the module's 40/30 defaults are the unwrapped
  fallback only).
- **`--max-actions N` (N ≥ 1) is still the single-shot form** — the tests, the
  manual runs, and anything that wants exactly one action keep it.

**The post-crawl pass** (`--post-crawl`) is the same engine with a longer clock
(`LOVEWORK_TICK_POST_CRAWL_MINUTES` = 300) and one guard relaxed: the crawl
wrapper launches it *after* a sweep has sealed (LJ: *"process all GO-s after the
full crawl and prepare the application packs after the full crawl"*), so the
skip windows no longer apply — the crawl is demonstrably done and the GPU is
free. The lock pair still guards every action, and where an hourly slot *yields*
a busy host to the next tick, a post-crawl pass **waits it out** (bounded:
`LOVEWORK_TICK_BUSY_WAIT_MINUTES` = 10), because nothing comes behind it to
resume the work — the Saturday-night watchdog (22:30) must not end a five-hour
pass over thirty seconds of checking. On the **relay** since 2026-10-21 (the LAN
model at 8081 before): the pass now spends relay tokens, at `reasoning_effort="max"`
— LJ reversed the earlier *"we are less token constrained than a sub"* when he moved
the loop onto the subscription (§14).

**The invitation is a reachable relay.** Before the first action a draining run probes
the model once (`GET /v1/models` with the credential, no model call): an unreachable
relay — network, expired key, quota — defers the whole run with one honest line instead
of a queue's worth of failed actions, each spending envelope and writing a failed
receipt. (Until 2026-10-21 the question was whether the LAN endpoint was *idle*, because
it served the same GPU as the crawls; a subscription relay has no idle state, only
reachability. The probe must send a real `User-Agent` — the WAF 403s
`Python-urllib/3.x` while answering curl, measured 2026-10-21.)

**What the principal reads: the board.** The refinement this loop produces
lived in scattered review notes while the ranking stayed the crawl's — LJ:
*"The initial GO list I saw on Sat after the principal lj crawl bears no
resemblance to the list as it is now."* Every draining run regenerates
`state/<p>/loop/hone-board.md` (`hone_board.py`): the live GOs joined with the
review ledger — moved rows first (crawl said → re-read says, Δ, the note link),
then the waiting rotation in the decider's own order (scored-without-the-advert
first). A projection, never a verdict: the wiki index keeps the decisions, the
packs hold the notes, `reviews.jsonl` is the ledger; the board is their
readable join, idempotent to regenerate, on demand via
`tick.py --profile <p> --board`.

**Enacting the DROPs (2026-10-18, same day).** LJ ruled that a review's tier
change must be *acted on*: *"So DROP should be acted on and that lead moved
from applications/ to leads/ and the index updated. No information should be
lost … that's a plus, not a minus."* `retire.retire_review_ruled` (riding the
hourly retire pass, docs/45 §5) moves the whole pack directory to `leads/`,
records the scores in the pile's ledger, rebuilds the index, and the board's
*Enacted* section shows what left and why. A review note is still not a
re-verdict on the page — the enactment is the move plus the ledger row, both
reversible, nothing deleted. Only DROP moves; MAYBE/FLAG stay apply-side.

## 13. First-level monitoring (2026-10-20)

LJ, after reading one more correct-but-raw line among dozens: *"I'm doing the monitoring.
Maybe we can employ some LLM based solution to do the 1st level of monitoring?"* So
`monitor.py` (job `lovework-monitor`, hourly at :50) reads the loop's own record and
prints **only what the principal needs to read**; a quiet hour prints nothing at all.
The division is the project's doctrine: **the facts are collected by code** (heartbeats,
receipts, envelope, self-heal classes, the scheduler's `executions.db` — exact, free,
testable) and **the judgement is the model's** (the same relay and model as every other
call — is this normal, does LJ need to act?). Three verdicts, default silence: `quiet`,
`informational`, `needs_principal`, `broken`. Every triage is appended to
`state/<p>/monitor/triage.jsonl`
so "why did the monitor stay quiet?" is answerable.

**A failure is a fact; broken is a state** (the 2026-10-20 regression). The first version
counted non-completed runs inside a 24 h window, so a syntax error in `lovework-tick.sh`
that was fixed at 15:16 kept the condition "true" all evening, and the job was reported
broken long after it had recovered. Rows are now rolled up **one per job**: only a job
whose **newest run did not complete** is broken; a job that failed and then completed is
reported as recovered, and the prompt says so in words.

**The alarm is keyed on the condition, never on the prose.** The first version suppressed
a repeat when the model's *headline* matched the previous one — but a model at temperature
0 still rewords, so one condition produced eight Telegram messages between 17:24 and 21:51
("A scheduled LoveWork job failed repeatedly in the last 24 hours", each a fresh
sentence). `monitor.signature()` now hashes the **state**: which heartbeats are stale,
which jobs are still failing, which self-heal classes rose. The last *spoken* signature
and its time live in `state/monitor/dedupe.json` (loop-wide, because the verdict may name
a different principal each hour); an unchanged condition is silent for
`LOVEWORK_MONITOR_REALARM_HOURS` (6 h) and then said once again — a genuinely stuck loop
is not forgotten, and a reworded sentence cannot re-arm the alarm.

**Read the live store, not the first path that exists.** `HERMES_HOME` names the Hermes
home root in the gateway's environment and the profile directory in the cron runner's;
`~/.hermes-host-a/cron/executions.db` is a 0-row decoy while
`profiles/profile-a/cron/executions.db` holds the record. `monitor._executions_db()` collects
every candidate and takes the one the scheduler is writing (newest mtime); when no store
is readable the facts say **`store UNAVAILABLE`** — blindness is reported, never mistaken
for a passing grade.

**In flight is not a failure** (the same day's second fault, 22:50). The scheduler writes
a `running` row when a job starts and rewrites it when the job ends — so the monitor,
running at :50, reads the row for **itself** and called job `f9515d76dd25` (the monitor)
"still failing" while it was mid-thought. `_is_failure()` now treats `running` /
`claimed` / `pending` as activity, not failure, until it outlives
`LOVEWORK_MONITOR_INFLIGHT_MINUTES` (90) — past that it is a hang, and a hang is a
failure. The facts print three buckets: **STILL failing**, **runs in flight right now
(not a break)**, and **recovered**; only the first can move the signature.

**The sent message is readable after the fact.** Hermes captures each job's stdout as
the Telegram body in `~/.hermes-host-a/profiles/profile-a/cron/output/<job_id>/<ts>.md`, so
"what did the monitor actually tell LJ?" is answered by a file, not a screenshot — and
`executions.delivery_outcome` says whether it was delivered.

## 14. The loop's model moved to the relay (2026-10-21)

LJ: *"For the Lovework tick-ng and all work from now on, for the llm calls, instead of
localllm on host-a:8081, use OpenCode Go opencode-go subscription, use key … Use
model deepseek-v4.1-flash with thinking or reasoning setting to max."* The LAN model
(`192.0.2.10:8081`, `qwen3.8-flash-next-iq3_xxs`) is retired for LoveWork; the tick,
the monitor and everything added later now use the same relay, key, model and effort as
the crawl. One authority, so they cannot drift apart again.

**Per principal, from the same day.** The *choices* live in
`profiles/<p>/llm.yaml` — one tracked file per principal, no secrets — so a principal
running their own copy on their own computer points it at their own provider, model
and key without touching code (the how-to, the precedence and the recipes:
[chapter 49](49-llm-settings.md)). The table below names the code that reads them and
the global fallbacks; the values are what shipped on 2026-10-21.

**Where each setting lives** (all of it in `lovework-agent/`; nothing secret in git):

| setting | value | where |
|---|---|---|
| **the choices (per principal)** | provider, `base_url`, `model`, `reasoning_effort`, `temperature`, `max_tokens`, `key_file` / `api_key_env` | `profiles/<p>/llm.yaml`, read by `config.llm_settings()` (env `LOVEWORK_LLM_*` overrides it) |
| credential | `~/.lovework/credentials/opencode-go.key` (mode 600, one line) | `config.LLM_KEY_FILE`, read by `config.secret_from_file` |
| credential overrides | `LOVEWORK_LLM_API_KEY` (explicit) → the file → `OPENCODE_GO_EQUAL_API_KEY` / `OPENCODE_GO_EQUAL_LJ_API_KEY` / `OPENCODE_GO_LJ_API_KEY` | `config.LLM_API_KEY` |
| base URL | `https://opencode.ai/zen/go/v1` (`LLM_BASE_URL` in `lovework-agent/.env`) | `config.LLM_BASE_URL` |
| model | `deepseek-v4.1-flash` (`LLM_MODEL` in `.env`) | `config.LLM_MODEL` |
| provider label | `opencode-go` (`LLM_PROVIDER` in `.env`) — labels the call log and the dataset ledger | `llm_client.LLMClient`, explicit provider wins |
| effort | `max`, passed to the relay as `reasoning_effort: "max"` — the relay's own vocabulary, no mapping | `config.LLM_REASONING_EFFORT` (default `max`, env-overridable) |
| session header | `x-opencode-session` on every `opencode.ai` call | `llm_client.LLMClient.__init__` |
| loop read timeout | 600 s (`LOVEWORK_TICK_LLM_TIMEOUT`) | `tick.TICK_LLM_TIMEOUT_SECONDS` |
| reachability probe | `GET {base}/models` with the credential and a real User-Agent | `tick.model_reachable()` |
| the client itself | `LLMClient()` with no overrides — key/base/model/effort all from `config` | `tick.model_client()` |

**Why the credential is a file, not `.env`:** `lovework-agent/.env` is tracked in git
and carries `LLM_API_KEY`; a rotation must not need a commit, and `.env` beating a shell
export is the 2026-09-10 trap. The file wins over `.env`; the tracked fallback stays for
older deployments, and deleting the file reverts the crawl to the old key without a code
change.

**What changed in behaviour:** the loop now spends relay tokens (the old *"we are less
token constrained than a sub"* no longer applies — LJ reversed it), reasoning is `max`
on every call including the crawl's matches, and the pre-action gate asks "does the
relay answer?" rather than "is the GPU idle?". The skip windows (`LOVEWORK_TICK_SKIP_WINDOWS`,
`daily:05-09`) are unchanged — they now guard disk/CPU contention with a running crawl,
not the shared GPU.

**A provider outage is a deferral, not a failure** (2026-10-21, the 09:20 tick).
The relay answered `503 Upstream request failed: Endpoint is unavailable` to all
three attempts in three seconds; the tick recorded lj `failed` and the wrapper sent a
*job failed* alarm — while vj and kj acted in the same run, and the relay answered
normally minutes later (quota 48 % monthly, 11 % weekly). `tick._is_provider_outage`
now classifies 5xx/429 and connection/timeout errors as **deferred** with
`why: provider unavailable: …` (the work is still due next hour, nothing lost),
and a 4xx or a bug still fails — the next hour will not fix those. Exit 1, and the
Telegram alarm, are reserved for real failures.

**Whose ledger the calls land in.** `model_transcripts` falls back to `config.DATASET_DIR`
(lj's), so vj's and kj's loop calls were filed under lj with `run_id: unattributed`.
Since 2026-10-21 `tick._main` pins the recorder to the principal before any work
(`model_transcripts.set_active_dataset`, `--profile <p>` only — the wrapper runs one
process per principal), so each call appears in `state/<p>/dataset/model_calls.jsonl`.
`--profile all` (one round-robin process) keeps the fallback.

**Verified (2026-10-21):** relay probe with the new key → `HTTP 200`,
`completion_tokens_details.reasoning_tokens` 14 on a one-word answer; `tick.model_reachable()`
→ `True`; a real `tick.model_client().structured()` call → `ok`; the monitor's live triage
on the relay returned `quiet` in **13 s** (the LAN model took 159 s for the same verdict).
