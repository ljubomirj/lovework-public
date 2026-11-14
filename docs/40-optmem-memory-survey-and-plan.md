# Chapter 40 — OptMem: memory survey and weave plan

> **Audience**: agent (builder) + person. **Status**: **adopted and
> implemented** — D22 (2026-10-04), M0–M5 built per
> [`PLAN.md`](../PLAN.md), and the historic crawls curated into the memories
> on 2026-10-05 (§8). §1–§6 are the survey and rationale, kept as written;
> §7 records the rulings as decided.
> **Read next**: [ch26](26-asof-strata-history.md) (the evidence spine memory
> must not duplicate), [ch35](35-the-watch.md) (the loop where most
> write-hooks land), [ch37](37-market-players-and-market-watching.md) (the
> entity population: orgs, people, aggregators).

LJ's framing (2026-10-04): *"Let us use it for all our intelligence. We need
per-profile memory that will record what transpired. But also per other market
participants — the firms doing the hiring, but also other agents or job-board
aggregators too, as well as other people, participants in the ecosystem… Atm
our memories are non-existent. Memory is an orthogonal functionality of the
functions we perform — is my feeling and opinion. But let us see."*

This chapter answers with (1) a survey of where LoveWork would benefit from
better memory, (2) a plan for weaving [OptMem](https://github.com/VictorTaelin/OptMem)
into the functions, and (3) a verdict on the orthogonality hypothesis.

---

## 1. What OptMem is, in LoveWork terms

One file of Python (`memo`, 859 lines, zero dependencies; vendored at
`contrib/OptMem/`, upstream `~/LJ-RLM-memory/OptMem`; its test suite passes —
109,099 assertions, 0 failures, run 2026-10-04). A *memory* is a directory
(`MEMORY_DIR` selects which) holding:

| Piece | What it is | LoveWork correspondence |
|---|---|---|
| `LOG.txt` | every memory, one per line, **append-only, never edited**, fixed-width records (`date` + text ≤280 bytes) | exactly our JSONL ledger discipline |
| `TREE/` | one-line summaries of record blocks (`#0-1`, `#2-3`, `#0-3`… binary merge tree), agent-authored during `nap`, droppable via `forget` without touching the log | the derived-projection tier (like `MANUAL.md`/`STATE.md`) but **hierarchical and zoomable** — the thing our ledgers deliberately lack |
| `config` | size knobs, per memory | — |

Commands: `wake` (read the memory as a bounded document — the first act of
every session), `note` (append one line), `nap` (answer a merge prompt),
`recall <regex>` (search the whole log), `zoom <lo>-<hi>` (open a tree node),
`forget`, `config`, `import` (one-time bootstrap from dated lines).

Three design facts matter to us:

1. **Position is identity.** Fixed-width records make record #N a byte offset;
   every lookup is one seek. A million memories → `wake` in 0.03s.
2. **Detail decays with age, mechanically.** `wake` prints a tiling of the log
   under a line budget (`WAKE_LINES`, default 96 ≈ 8k tokens): recent records
   verbatim, older ones as tree summaries, ancient ones collapsed. Finest near
   the present — the same shape as our decay policy (30d quiet, P halving
   monthly), applied to narrative instead of applications.
3. **`wake [part] [T]` renders the memory as of record T.** Point-in-time is
   native. T is a record count, not a date, but records carry dates and the
   log is date-ordered, so a date→T wrapper is a binary search. This is the
   AsOf read, already built.

And one alignment that costs us nothing: the seal contract's include trees are
`cache, wiki, dataset, sources, logs, loop` under `state/<p>/`
(`strata_contract.INCLUDE_TREES`; `loop` joined 2026-10-11 so the agentic loop's
own residue — receipts, envelopes, drafts, transcripts — is point-in-time too). A memory placed at
`state/<p>/dataset/memory/…` is **captured by seals automatically** — frozen,
hashed, replayable at any generation. Evidence-grade PiT for memory needs zero
new machinery.

## 2. The authority boundary (read this before the survey)

LoveWork's core discipline is one authority per fact. OptMem does not compete
with it:

- **Ledgers stay the authority for facts.** The matcher's reapply-kill, the
  shoulder's chase window, the decay policy, promote/demote's keel — all keep
  reading `outcomes.jsonl`, the 4-cube, `list_decisions.jsonl`. No function
  ever *decides* from memory.
- **Memory is the narrative tier**: what transpired in agent-readable prose —
  the arc of a firm, the tone of a recipient, the lesson of a crawl rot. It is
  authored (by pipelines factually, by sessions judgmentally), append-only,
  compressible, and searchable. A memory note may *mirror* a ledger fact in
  narrative voice; the ledger row remains the single source.

This is the same split as wiki pages vs dataset, made temporal and zoomable.

## 3. Survey: where better memory pays, function by function

| # | Function | Memory today | What's missing | What an OptMem instance gives |
|---|----------|--------------|----------------|-------------------------------|
| 1 | Session start / consume (AS1 `STATE.md`, `CONTINUE.md`) | process memory only: due list, principal table, warm handoff | *who we are and what was learned* never wakes; `LEARNINGS.md` is project-global and must be read whole | `wake` of the principal memory: identity + standing decisions + lessons, bounded, freshest near present |
| 2 | Watch (crawl, matcher, reapply-kill) | per-run reports, ledgers, `logs/` | source-failure history is unmemorized: the 40–57 silent listings per principal are re-discovered by re-failing; "board X 403s since September" lives nowhere | per-org notes (`careers 403 since D`, `listing reposted 3rd time`) → the enrichment backlog becomes a memory query |
| 3 | Brief (Shoulder W2) | joins ledgers + wiki at render time | the *story* of a firm (3 reposts, 2 rejections, applied Aug, decayed) is re-derived from 3–4 files every morning | brief session wakes the org memories in scope; the arc is one read |
| 4 | React (W3) | `reactions.jsonl` (evidence) | tone and relationship state (who replied human, who auto-rejects) not distilled anywhere | org/people notes: reaction received, tone, counterpart name |
| 5 | Learn close (W4) | `JOURNAL.md` + `LEARNINGS.md`, project-level | per-principal operational lessons (calibration drift, crawl quirks) have no home scoped to the principal | principal memory notes + the natural slot to pay `nap` compressions |
| 6 | Applications / cases | outcomes ledger (authoritative), case READMEs (good local memory), `.txt` emails in case dirs | the human-side narrative (recruiter names, interview texture) is trapped in unindexed case files; cross-case per-firm history not surfaced at application time | org memory carries milestones in narrative voice; `recall` surfaces "we've been here before" before the next apply |
| 7 | Promote / demote | keel + `list_decisions.jsonl` rows (already one-liners) | the *why* is a row note read only in `--audit` | decision one-liners note into org memory verbatim — the keel gains a narrative shadow |
| 8 | Letters (J&J) | letter ledger + drop archive | per-recipient relationship memory absent: last-contact date, reply history, cooldown hygiene for cold outreach | `agents/<peer>` and `people/<slug>` memories: sent/replied/bounced/dropped with dates |
| 9 | People / introductions | `people_tracker.md` — a static list (51 people) | zero interaction memory; the N² serendipity surface has no ledger of touches | one memory per person, created on first noteworthy fact or first contact (contact only on LJ's say-so — unchanged) |
| 10 | Ecosystem agents / aggregators | scattered: source configs, transcripts, ATA notes | no memory of *the other agents* (boards, frontierlist, interview peers): what they expose, what changed, what failed | `agents/<name>` memories: capability + reliability notes over time |
| 11 | AsOf / seals (WP0–WP2) | full evidence PiT | — (not a gap; the constraint) | memory rides seals via `dataset/memory/`; runtime PiT read = `wake 1 T` |
| 12 | Simulator / experiments (ch13, ch17) | `assessment_cache`, `LEARNINGS.md` | "what did we already try, with what score" is recall-hostile | **deliberately out of scope** — LEARNINGS + JOURNAL serve the build side; a memory would duplicate them |

The pattern across the table: **facts are well kept; narrative is nowhere.**
Even our best-memory artefacts (case READMEs, wiki org pages) are either
local-scope or per-sighting flat appends with no zoom. The two recent
convulsions prove the cost: the 221+11 decay closures (2026-10-03) ended 232
relationships with no place to write what happened; the VJ yaml breakage was
diagnosed from shell archaeology rather than a source memory that would have
said `watch: vj company_pages.yaml unparseable since 2026-09-10`.

## 4. Verdict on the orthogonality hypothesis

LJ's hypothesis: *memory is orthogonal to the functions we perform*. Verdict:
**true at the mechanism level, false at the wiring level — and the second half
is the work.**

- **Orthogonal as infrastructure** — like the ledgers or git: any function
  reads/writes it, no function owns it, swapping it changes no ledger-derived
  behaviour. OptMem's `MEMORY_DIR` makes it a pure side-capability: the same
  tool, many stores, zero coupling to call sites.
- **Entangled in exactly two seams.** (a) *Write-hooks*: what is worth
  remembering is function-relative; a function with no note-hook contributes
  nothing, and unwired memory stays empty — which is precisely why ours is
  "non-existent" today despite four months of ledgers. (b) *Read-gates*: a
  brief composed after waking org memory is a different brief; memory changes
  function output without changing function logic.
- The clean restatement in LoveWork vocabulary: **ledgers are the orthogonal
  fact layer (already built); OptMem is the authored narrative layer — stored
  orthogonally, consumed functionally.** Memory is to the functions what the
  marginalia in a lab notebook is to the instrument readings.

## 5. The weave plan

### 5.1 Instance layout

One OptMem memory per entity, all under the sealed dataset tree, slugs reusing
`company_events.canonical_company` keys (one org-naming authority, ch37):

```
state/<p>/dataset/memory/
  principal/          this operator-agent's own memory for principal p
  orgs/<slug>/        one per tracked firm, created lazily on first note
  people/<slug>/      one per ecosystem person (people_tracker + contacts)
  agents/<name>/      one per external agent/aggregator/board/peer
```

Per-principal by design: the relationship with a firm is the principal's (VJ's
BBC is not LJ's BBC), matching how watchlists, outcomes and wiki already
scope. A principal-independent *fact* (a funding round) may appear in two
memories — acceptable duplication in the narrative tier; the evidence stays
per-principal anyway. Population at start: ~80 orgs (lj market tracker), 51
people (lj), a handful of agents (frontierlist, the boards, J&J peers) — but
all **lazily**: a memory exists only once something was noted.

Sizes: `WAKE_LINES` default 96 for `principal/`; orgs/people/agents run 48
(they are consulted scoped, not first thing every session).

### 5.2 Writers — the note-hooks, function by function

Machine-written notes carry a tag prefix (machine voice, grep-able); judgment
notes are untagged (session-authored).

| Function | Memory | Note (one line each) |
|----------|--------|----------------------|
| Watch | `orgs/<slug>` | deltas only: `watch: new listing <role>` / `gone` / `repost #3` / `careers 403 since <date>` / `source healthy again` |
| Brief | — (reader) | wakes the orgs in scope before composing |
| React | `orgs/<slug>` | `react: human reply from <name>, warm` / `auto-rejection` |
| Applications / cases | `orgs/<slug>` + `principal/` | milestones mirroring outcome rows in narrative voice: `case: applied <role> <date>` / `interview` / `offer` / `NO` |
| Promote / demote | `orgs/<slug>` | the keel decision line verbatim: `keel: promoted tier A (5 GO/6 dates, best 8.7)` |
| Letters (J&J) | `agents/<peer>`, `people/<slug>` | `letter: sent <topic>` / `replied` / `bounced` / `dropped (out of scope)` |
| Learn close (W4) | `principal/` | operational lessons; **and pays pending `nap` compressions** |
| Session end | `principal/` | what was decided and tried (the OptMem pitch verbatim) |
| People tracker | `people/<slug>` | first-sighting notes; contact events only via the letter loop on LJ's say-so |

Pipelines (`watch`, `keel`, `case` writers) call `memo note` as a subprocess
and **ignore nap prompts** if one prints (nothing runs in the background in
OptMem; an unanswered prompt is inert). Sessions author judgments and answer
naps immediately, per the OptMem protocol.

### 5.3 Readers

- **Session start** (alongside `STATE.md` + `DUE.md`): `wake` the active
  principal's `principal/` memory. STATE answers *where are we*; memory answers
  *what do we know from having been here*.
- **Brief composition**: wake each org memory mentioned in the brief (scoped,
  so the 48-line budget is generous).
- **Letters / intros**: wake the recipient's memory before composing —
  last-contact hygiene comes free.
- **Ad-hoc**: `recall` across one memory (per-entity stores keep regexes
  precise; no cross-entity tag discipline needed).

### 5.4 Bootstrap policy

Start nearly empty and grow forward; two seeding passes only:

1. `principal/`: ~10 hand-authored identity lines (who the principal is,
   standing decisions, the decay ruling, current market shape) via one
   `import` file. Hand-seeding beats bulk import — `JOURNAL.md` is a build log
   at the wrong granularity and voice.
2. `orgs/`: one baseline line per tracked org, generated from the 4-cube and
   outcomes (dated today): `baseline: 15 events since 2026-06, applied 3×,
   last event 2026-09-28, tier B`. Honest, one line, importable in one shot
   (`import` demands monotonic dates — sort before feeding).

No bulk import of reactions, letters history, or wiki pages: their facts are
already in ledgers; the memory tier starts narrating from adoption day, with
the baseline line carrying the compressed past.

### 5.5 Operational rules

- **Subagents never run `memo`** — OptMem's own subagent clause, extended to
  our reality (workflow subagents, ZCode subagents): they cannot judge what is
  already known. The memory block (OptMem's 426-token prompt, adjusted for
  `MEMORY_DIR` conventions) goes into the project `AGENTS.md` at M1.
- **No secrets in memory, ever.** Narrative only; the seal contract's
  credential-hint exclusions are the backstop, the note discipline the front.
- **Multi-machine writes.** Crons note on host-a; sessions note on host-b;
  both commit `state/`. Appends at the same file end from two machines → a git
  conflict. Records are whole fixed-width lines, so resolution is a safe
  concatenation in date order (a 10-line resolver, written at M2). Rare by
  construction (org memories are host-a-written; `principal/` is mostly
  session-written); accepted, not engineered around.
- **Nothing ever deleted.** Matches the house rule and the ledger discipline;
  `forget` only drops a derived summary for re-authoring.

### 5.6 Phases

| Phase | Delivers | Gate |
|-------|----------|------|
| **M0 adopt** | vendor `memo` at `lovework-agent/tools/optmem/memo` (pinned copy; `contrib/OptMem/` stays the upstream symlink); smoke tests | suite green in repo venv |
| **M1 principal** | `state/example/dataset/memory/principal/`: init, seed, wake wired into session protocol (`AGENTS.md` block + `STATE.md` pointer line) | one live session run off it; LJ reads a wake |
| **M2 orgs** | lazy org init; baseline import script; writers: watch deltas, case milestones, keel lines; brief wakes orgs; conflict resolver | next morning brief cites an org arc sourced from memory |
| **M3 people** | `people/<slug>` on the letter loop; first-contact notes | a letter composed after waking its recipient |
| **M4 agents** | `agents/<name>` for boards, frontierlist, J&J peers | source-reliability notes visible in enrichment triage |
| **M5 AsOf wrapper** | `tools/memwake --asof <date|run-id>`: date→T binary search over LOG, then `wake 1 T`; seal-level AsOf test that a sealed generation's memory wakes PiT-clean | replay of an old generation wakes the memory as it was |

Every phase is additive; no phase touches the matcher, reapply-kill, decay or
keel read paths (§2 authority boundary).

## 6. Alternatives, for the record

- **δ-mem** (`contrib/delta-Mem/`): memory *inside model weights* — delta-rule
  online state beside a frozen backbone; needs adapters/training per model.
  Solves a different problem (context-window economics), not an agent's
  auditable timeline. Rejected.
- **hindsight** (`contrib/hindsight/`): vectorize.io's cloud RAG memory
  platform — server, embeddings, dashboards. Heavy, external, non-append-only
  in spirit. Rejected.
- **Do nothing / lean on ledgers**: keeps us exactly where the survey says we
  are — facts perfect, narrative absent, every brief re-deriving arcs by hand.

OptMem wins on LJ's own criteria: simple enough to understand, implement and
debug; coarse or fine at will (the `wake` budget); compressible but
lossless (TREE is derived, LOG forever); a timeline, hence a clear AsOf view.

## 7. Decisions requested (nothing starts before these)

*Settled 2026-10-04 — LJ's go-ahead ("sounds like this could be useful to
us… let's make a plan… and implement them"). Adoption recorded as **D22** in
`DECISIONS.md`; the task-derivation plan lives in the repo root
[`PLAN.md`](../PLAN.md) (M0–M5, files, tests, gates, sequencing). The four
rulings below were decided as stated — 3–5 as defaults LJ can veto in
passing:*

1. **Adopt OptMem as the narrative tier** — yes; D22. **Vendor** a pinned
   copy at `lovework-agent/tools/optmem/` (we own the deltas);
   `contrib/OptMem/` stays the pristine upstream symlink.
2. **M1 scope** — lj first; vj/kj join at M2.
3. **Baseline import** for orgs — yes, one generated `baseline:` line per
   tracked org from the 4-cube + outcomes.
4. **Where naps get paid** — the W4 learn-close session ritual.

---

## 8. Historic curation (2026-10-05)

The M0–M5 hooks note *forward*; four months of ledger history was never
curated in — each org memory held a single generated `baseline:` line and the
principal memories held nothing. `lovework-agent/build_history_import.py`
closed that gap, once, per principal:

| Source | Note written | Memory |
|---|---|---|
| `outcomes.jsonl` (lj 1,335 rows; vj 24) | `case: <date> <kind> — <role> @ <org> [#<outcome_id> <source>]` | `orgs/<org>` |
| `list_decisions.jsonl` | `keel: promoted\|demoted <org> — <reason> [#<decision_id>]` | `orgs/<org>` |
| `letters/*.jsonl` | `letter: J&J <date> — <title> @ <company> [#<ref>]` | `agents/jack-and-jill` |
| case dirs with no ledger row | `case: <date> applied — … [case dir, pre-ledger]` | `orgs/<org>` |
| the same outcome rows, counted | `month: <YYYY-MM> — N outcome record(s): applied …, rejection …` | `principal/` |
| `IDENTITY` in the script | ~9 identity + standing-decision lines per principal | `principal/` |

**Origination, both directions.** Every machine line carries its ledger id
(`#<outcome_id>`, `#<decision_id>`, `#<referencenumber>`) or its case
directory name, so a memory line resolves back to the ledger row and the case
dir it came from; forward, a case directory is reachable from the org memory
that names it (`recall '<org>'`). `outcomes.jsonl`'s own `path` field is the
strong form of that link and is what the notes quote.

**Coarsening.** `--pay-naps` pays every pending compression with a generated
one-line summary built only from the children's dates, record counts and tag
vocabulary — months and kinds, never invented detail. This is a deliberate,
recorded deviation: PLAN.md §5.2 expects sessions to pay naps, but 1,400+
imported records imply ~2,800 base-level summaries that no session ritual
would absorb. Sessions keep paying naps for new notes; the historic tree is
built mechanically, and `zoom` still drills to the raw lines.

Idempotent: a record is skipped when its id is already in that memory's log
(`recall`), so re-running the import is a no-op.

**Naming (2026-10-05).** The curation wrote each org memory under the ledger's
`org_name` verbatim, and that field is machine-extracted and dirty: spaces,
commas, parentheses, accents, appended locations (`Jefferies New York, NY`,
`Voleon Group Berkeley, CA`), stray prefixes (`Le 001 Leerink Partners…`), the
same firm spelled several ways (`Hastings Insurance` / `Hastings Insurance
Services Ltd`), and rows that are not organisations at all (`Machine Learning
Engineer`, `Near you`, a test fixture). LJ's rule: *names on the file system
must be user- and filesystem-friendly, not only machine friendly*.

`mem.slug` is now the one naming authority (`mem.memory_dir` uses it): accents
folded in the slug (`Dassault Systèmes` → `Dassault-Systemes`), trailing
locations and descriptors dropped (`Jefferies New York, NY` → `Jefferies`,
`Owen Thomas | B Corp™` → `Owen-Thomas`), `&` spelled `and`
(`Legal-and-General`), hyphens for separators, `SLUG_OVERRIDES` for names the
rules cannot rescue, `NOT_AN_ORG` for the noise. `normalize_memory_slugs.py`
applied it to what existed — **163 moves** across the three principals:
renames, 8 merges (logs unioned in date order, the folded-away source kept in
`_attic/`, never deleted), noise atticked — leaving **0** awkward directory
names. The proper spelling lives on in the ledger, the memory's own text and
the wiki; the slug is only the convenient form of it. Rule and rationale:
`lovework-hydrate` §10 ("Names on the filesystem"), pinned by
`tests/test_mem_slugs.py`.


