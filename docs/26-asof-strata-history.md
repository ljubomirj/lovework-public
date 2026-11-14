# Chapter 26 — Point-in-time history: strata and AsOf

> **Status: design under implementation (updated 2026-09-20).** D20 exists;
> WP0 contracts/inventory, the standalone WP1a seal engine and WP2a
> seal-level AsOf CLI are built and fixture-tested. The **wrapper crawl path is
> wired through `run_operation`** (`lovework-crawl.sh` → `crawl_operation.py`,
> committed 2026-09-17) while other entry points (direct `main.py`, `consume.py`,
> `reassess.py`) are not; no seal, attempt record or staging is observable on
> host-b's copied state, so **live-seal status is adjudicated on host-a** —
> check `state/<p>/strata/` and the checkout revision there. `STATE.md` remains
> planned. Unified GLM/Codex design, 2026-09-14; implementation findings below retain
> their 2026-09-13 audit date. This specification qualifies D20's original
> claims. Implementation gates are in §8. Source reporting is the separate
> [26-source-report.md](26-source-report.md); retain both paths for compatibility.
> Companion: [27-agent-ergonomics.md](27-agent-ergonomics.md).

## 1. What we are preserving

LoveWork is a principal's evolving understanding of opportunities, supported
by observations and tested through decisions and outcomes. A crawl is one way
of acquiring evidence. A report is one expression of a judgment. Neither is
the whole state of the system.

The durable unit is an **observation with provenance**, connected to the
decision that used it. A snapshot preserves a collection of those units and
the files needed to interpret them. Time belongs to each observation; a
four-hour crawl is not a simultaneous observation of the world.

Four questions require different answers:

| Question | Contract |
|---|---|
| What did we actually produce then? | Retrieve the original sealed output and its identity. Later corrections are separate revisions. |
| What did principal P know by D? | Select evidence available to P by the cutoff, retaining gaps and uncertainty. |
| What would the then-current system have produced? | Reconstruct from frozen inputs and the historical execution specification; disclose missing dependencies and stochastic limits. |
| What would policy Q say about that old evidence? | Run a labelled counterfactual, changing only declared inputs; save it separately with a parent reference. |

An existing report can answer the first question partly even when faithful
reconstruction is impossible. Missing evidence is an answer, not permission
to retrieve today's website and call it yesterday's.

### Existing foundations and limits

Audited against code, rather than historic documentation:

| Existing surface | What it preserves | What it does not establish |
|---|---|---|
| `job_registry.py` | `first_seen`, `last_seen`, current lifecycle; inherited owner discovery dates | Day-level processing dates are not verified online liveness. Upsert can refresh dates from cached/replayed data. |
| `snapshot.py: snapshot_cache()` | Pre-crawl copies of `jobs.csv` / `jobs.db` | No consistent full evidence or policy snapshot. |
| Dataset run/assessment/outcome JSONL | Judgments, hashes, some primary-page metadata, passive outcomes | Full dependency closure, immutable original model calls, unified wrapper/pipeline identity. |
| Crawler and enrichment caches | Some retained page text | Cache hits are not fresh web observations; not every response or expiry is retained. |
| Gmail archive | Saved bodies, message IDs, archive dates | Message date is not when LoveWork knew the content; backfill is later acquisition. |
| Wrapper run records | Start/finish, report and notification receipts | Records are mutable; wrapper ID differs from pipeline UUID. |
| `consume.py` | Owner declaration and ingestion history | Its owner run ID currently does not pin an immutable owner corpus. |

Current `pipeline.py` can finalize lifecycle for attempted sources even after
errors. Source-level success also cannot prove every org/page was enumerated.
Do not use historical `disappeared` rows as confirmed closure labels.

## 2. One system, explicit contracts between layers

```text
principal intent / authority                     external world
           |                                          |
           +------ captured evidence and events -------+
                              L0
                 sealed run bundles / strata
                              |
                  L1 identities and timeline
                              |
                  L2 temporal views and queries
                              |
                  L3 judgments and alternatives
                              |
                  L4 actions and their receipts
                              |
                  L5 evaluation and learning
                              |
                  versioned policy proposals
                              +----> next run's captured inputs
```

L0 preserves **what was observed or asserted**, not guaranteed world truth.
An advert can lie; a principal statement, parser inference and HTTP response
are different evidence kinds. Upper layers may generate new observations
(a reaction, experiment result or action receipt); those must enter L0 before
being used as durable knowledge. Derived views can be rebuilt. Original
judgments and action receipts cannot be regenerated by asking a model again.

Storage, temporal semantics and the operator interface are separate contracts.
The first version needs ordinary files and small deterministic readers, not a
new distributed database or agent framework.

## 3. L0: hard-linked strata, with a consistency protocol

LJ's `~/bin/README-wdbackup` supplies the basic mechanism: build a **new**
directory, copying changed files and linking unchanged files to an older
snapshot. All generations remain ordinary, simultaneously accessible trees.

### Why filesystem-first

The storage should remain inspectable even if the current application and
agent harness disappear. Ordinary directories support `rg`, `cat`, `stat`
and `diff` without a running service or database migration. Repeated careers
pages and unchanged archive files are natural candidates for whole-file
reuse. The writer and manifest still have dependencies and a schema; the
captured bytes remain readable independently of them.

Use three consistent storage terms: **strata** preserve evidence and run
state, the **change journal** explains differences, and **AsOf** queries the
history. These are components within the shared L0–L5 tower, not a competing
layer-numbering scheme. Simple storage makes early capture useful before
semantic readers are complete; its limits are made explicit below.

Canonical target layout. WP1a implements the strata root, `inputs/`, `state/`,
`outputs/`, `current`, and `strata/history-index/`; typed `evidence/` and
`events/` capture plus isolated replay remain later work. Live run directories
remain absent until WP1b:

```text
state/<p>/
  strata/<run_id>/
    manifest.json             # schema, lineage, cutoffs, coverage, digests
    inputs/                   # effective profile, policy/config, history used
    evidence/                 # captured source responses and model inputs
    events/                   # fetches, coverage, decisions, state transitions
    outputs/                  # original reports and judgments
    state/                    # allowlisted compatibility state at seal time
  strata/current -> <run_id>  # latest sealed generation, not latest success
  strata/history-index/       # disposable indexes, manifest watermark
  replays/<replay_id>/        # derived results, never live production state
```

Start with `state/` copies of the allowlisted cache/wiki/dataset/sources/logs
and actual input profile files. Exclude strata themselves, recursive legacy
archives, credentials, `.env`, all `.git*`, sockets, locks and temporary files.
Record every omitted class and its replay consequence. Do not copy the whole
worktree or follow arbitrary symlinks. Private case/history information needed
by a decision is captured explicitly as consumed inputs; a symlink to the
live external applications directory is not frozen evidence. Share only the
declared public-advert subset across principals, never whole private strata.

### Capture scope at a glance

| Input tree or artifact | Treatment | Reason |
|---|---|---|
| Page/enrichment caches and registry | Include the allowlisted run-state copy; capture consumed versions separately | State restoration and decision provenance are different requirements. |
| Wiki, reports and source/incident logs | Preserve the run's versions | Later report healing must not erase the original output. |
| Dataset ledgers | Freeze consistent copies initially; record row counts and digests | Detect truncation and support joins, while measuring growing-file cost. |
| Gmail archive | Include authorized message records and their acquisition metadata | Stable message files reuse well; old message dates do not prove old knowledge. |
| Profile and effective policy/history | Pin in `inputs/` and execute against them | A run-end profile copy may differ from what was consumed. |
| `cache/archive/` | Inventory separately as legacy evidence; exclude from recurring recursive capture | Avoid repeatedly nesting copies of old snapshots. |
| Applications and other external paths | No recursive capture; retain authorized facts actually consumed | Preserve the principal boundary and avoid live symlinks in replay. |
| Credentials, `.env`, Git internals, locks and staging | Exclude | No secrets, recursive history or transient process state in the bundle. |

### Acquisition and sealing

1. Allocate one run ID before any work, with host, principal, mode and optional
   parent ID. Every entry point uses the same run context. Persist the attempt
   before credential checks; a preflight failure is useful operational history.
2. Pin the effective inputs at run start and make execution read those bytes.
   Include the assembled profile, selected role, consulted bio, source policy,
   prompts, non-secret effective settings, code revision **and dirty content**,
   dependency versions, model/provider/parameters and effective decision clock.
   A Git SHA and a profile hash alone cannot reproduce an uncommitted run.
3. Capture each external/tool response and the exact normalized input used
   downstream when it is consumed. Record cache reuse against its original
   observation. Retain negative responses as well as positive ones. If only
   rendered markdown or truncated text is available, label that representation;
   do not claim raw HTTP/HTML was preserved. Persist model requests, responses,
   parsed decisions and prompt versions without credentials.
4. Use a shared writer protocol for all mutable-state writers: crawl, direct
   CLI/MCP, consume, reassessment, link recheck, report heal and notification
   updates. Begin with an atomic per-principal lock and a quiescent seal boundary.
   A wrapper-only check-then-create lock is insufficient. Copy SQLite through a
   consistent backup API or after closing writers; copying an active DB/WAL
   independently is not a transactional snapshot. Pin a sealed owner generation
   for consume, avoiding a second principal's mutable registry/cache pair.
5. Populate a fresh `.partial-<run_id>-<attempt>/` beside the final directory.
   The first generation copies live files; later generations may hard-link
   **only to prior sealed generations**, never to live files. The rsync shape
   is `rsync -a --checksum --link-dest=<absolute previous tree> <source>/ <fresh destination>/`.
   Use a reviewed allowlist. Never use `--inplace`, `--append`, or destination
   reuse. No `--delete` is needed for an empty destination. The previous and
   new snapshot must share a filesystem for hard-link reuse; otherwise use an
   explicitly reported full-copy fallback or return unsupported.
6. Verify manifest paths, file digests, required artifacts, input bindings and
   coverage. Flush files and directories as appropriate, then atomically rename
   on the same filesystem; replace `current` through a temporary symlink and
   atomic rename. Readers discover final directories only. A failed crawl may
   have a valid sealed bundle marked `partial`; a failed seal never advances
   `current`. On restart preserve abandoned staging for inspection. Recovery
   validates its already durable events and seals a separate interrupted-run
   bundle, retaining original recording times plus recovery/seal timestamps.
   Missing dependencies remain gaps; never promote staging merely because
   files exist. Normal readers continue to see only sealed bundles.
7. Seal the crawl output before delivery. Later notification receipts, healed
   reports or rechecks are separately sealed child events/revisions referring
   to that original. Do not reopen a stratum to update its notification flag.

Hard links share an inode: editing any linked file edits every generation
using it. Thus this is immutable **by writer contract**, with digest checking,
not immutability magically provided by hard links. Permission/mtime changes
also affect shared inodes; do not recursively chmod/touch a sealed tree to
"protect" one generation. Enforce write boundaries; treat integrity failures
as incidents. A checksum manifest detects accidents, not malicious rewriting
of both bytes and manifest. These snapshots on one disk are not a backup.

Rsync links only when preserved attributes also match; identical content with
changed mtimes can still allocate a copy. The relevant capacity unit is the
**whole changed file**, not its byte delta. See the
[rsync manual](https://download.samba.org/pub/rsync/rsync.1#opt--link-dest).

### Minimum manifest contract

Schema-versioned, principal-scoped JSON; IDs are opaque and timestamps UTC:

- `run_id`, `parent_run_id`, `host`, `principal`, `role`, `mode`, legacy ID aliases;
- `started_at`, `finished_at`, `sealed_at`, observation window and input cutoff;
- separate `execution_status`, `capture_status`, `coverage_status`;
- previous manifest digest and any owner manifest/evidence references;
- effective input and output references, SHA-256, byte sizes, representation;
- per-source/scope attempt results, coverage limits and missing dependencies;
- file inventory plus exclusions; schema/reader versions and replay capability;
- counts by artifact kind and source scope, ledger row counts, and separate
  logical bytes, newly allocated/copied bytes and linked-file counts.

Counts and coverage let an agent decide which stratum to inspect without
opening the whole corpus. File hashes remain the integrity check; equal row
counts alone do not establish equal data. A rebuildable
`strata/history-index/strata.jsonl` lists sealed manifests with run ID, seal time,
mode, status, predecessor and digest. Publish the seal before indexing it;
recovery can rediscover a sealed directory missing from the index. The index
and `current` pointer are conveniences, never additional authorities.
Distinguish the previous generation used for deduplication from a causal
`parent_run_id` (such as a replay's parent): they need not be the same run.

Manifest paths are relative and confined to the bundle or explicit validated
immutable dependencies. A missing dependency produces `incomplete`, never a
live fallback. Readers validate schemas; an unknown version is an actionable
error. Legacy records remain raw and acquire versioned interpretation adapters.

## 4. Time and identity: the vocabulary every layer shares

Use distinct fields, not one overloaded `date`:

| Field | Meaning |
|---|---|
| `observed_at` | When the response/assertion was acquired; cache replay retains its original time. |
| `recorded_at` | When this event entered LoveWork's durable history. |
| `available_at` | When evidence became available to this principal; owner observation and consumer ingestion differ. |
| `source_asserted_at` / validity interval | Publication/expiry/event time asserted by the source, with precision and confidence. |
| `assessed_at` and `effective_at` | When calculation ran, and which clock its cooldown/time-sensitive policy used. |
| `sealed_at` | When this durable bundle became queryable. |

The bitemporal core separates **event/valid time** from **knowledge time**.
Use the conservative knowledge cutoff: require acquisition, recording and
principal availability no later than D. A later backfill may describe an old
event but was not known then. Optional retrospective queries may use later
knowledge about earlier events, with both cutoffs shown explicitly. Missing
times remain unknown; dates retain day precision and known timezone, not
invented midnight accuracy. Same-time ordering uses recorded sequence/IDs.

For a run sealed after D, run-level queries use the latest eligible seal at
or before D and display the lag. Event-level queries may include earlier
durably recorded events from that run only if their recording times are
trustworthy. Never label a later complete run as the exact state at D.

Separate identifiers:

- `opportunity_id`: provider namespace + stable posting ID when available;
  otherwise a versioned provisional identity, with uncertainty.
- `evidence_id`: content digest plus acquisition event reference; two fetches
  of identical bytes share content but are two observations.
- `assessment_id`, `run_id`, `case_id`, `action_id`: separate causal objects.
- Identity aliases/merges/splits are timestamped assertions, not destructive
  rewrites. Org/title similarity proposes a link; it does not prove identity.

Keep today's `advert_hash` as an identity alias: new assessment rows use the
registry hash helper, but historical variants and application identities need
explicit reconciliation. These are not hashes of advert content. Changed text is a
new evidence version, not automatically a new opportunity. A repost may be a
new opportunity despite the same title. Historical identity resolution must
also respect its knowledge cutoff; later merges cannot silently alter old views.

## 5. Observation history, coverage and liveness

Record source attempts and actual observation events at acquisition time;
derive registry/journal views from them. A filesystem diff alone cannot tell
whether an unchanged page was revalidated, skipped, or read from cache.

Every attempt names its scope (provider query/org/page/mailbox category),
source configuration digest, pagination/cursor, start/end, cache use, parser
version, retrieval and parse outcomes, budgets and completeness. Distinguish
`complete`, `partial`, `empty`, `failed`, `skipped`, `authwall`, `rate_limited`
and `not_due`; report zero as an observed count only when justified. "Complete"
means complete for the declared scope, never the entire job market.

| Event | Valid inference |
|---|---|
| Fresh primary advert retrieved and recognized as open | Advance `last_verified_open_at` for that posting. |
| Role appears in a fresh feed | Advance `last_listed_at`; provider listing is weaker than primary verification. |
| Email alert about a role | Record a mention at the alert/observation times; no claim of current openness. |
| Cached text reused or archived mail replayed | Processing event only; preserve observation age. |
| Qualified HTTP 304 | Revalidation of the selected representation; retain validator, prior bytes and endpoint semantics. |
| Explicit closure/expiry on primary page | Record closure evidence and observed time, preserving contradictory evidence. |
| 404/410 | Record endpoint disappearance; do not infer company vacancy filled or closed everywhere. |
| Absent from complete comparable enumeration | `not_listed_in_scope`; closure remains a policy inference. |
| Timeout, partial pagination, cap, cadence skip, parse failure | Observation gap; no negative lifecycle transition. |

Expose `first_observed_at`, `last_mentioned_at`, `last_listed_at`,
`last_verified_open_at`, last check result and age independently. Do not
force multiple sources' conflicting claims into one unqualified boolean.

If an advert was verified open on A and explicitly closed on B, an inferred
closing transition is bounded by `(A, B]` under a same-episode assumption with
no unobserved reopening. Preserve the actual observations independently of
that assumption; they do not establish a unique full vacancy history. With no later qualified closure,
the observation is right-censored; gaps do not prove survival through them.
First discovery is not publication, so time-to-closure since discovery is
not total vacancy lifetime. Reappearances create new episodes or explicit
identity questions. Late corrections append events and leave prior views
recoverable. Outcome labels must retain these distinctions (chapter 19).

### The change journal: explain adjacent generations

Generate a deterministic, rebuildable journal under
`strata/history-index/journal/<from-run>--<to-run>.jsonl`. Each row includes both
manifest digests and the reducer version. Raw acquisition/coverage events
remain sealed in their run bundles; the journal interprets them alongside
file and registry changes. It must never invent a missing observation event.

Illustrative row shapes (IDs abbreviated; metadata common to all rows omitted):

```json
{"kind":"file_change","path":"state/cache/page_example.md","change":"modified","same_inode":false,"bytes_delta":-184,"before_evidence":"e1","after_evidence":"e2"}
{"kind":"registry_transition","opportunity_id":"job-1","from":"new","to":"disappeared","meaning":"legacy_registry_state_change","evidence_refs":["registry-before","registry-after"]}
{"kind":"observation_gap","source":"gmail_lj_jobs","scope":"label:LJ-jobs","reason":"credential_unavailable","attempt_ref":"attempt-1"}
```

File rows distinguish added/removed/modified/metadata-only changes. The same
`(device, inode)` is a cheap reuse signal under the sealed-writer contract;
manifest hashes establish content equality across different inodes. Recheck
bytes for integrity verification; inode equality alone cannot detect a shared
inode that was accidentally modified. A linked file cannot simultaneously be
reported as having different before/after content.

Registry rows report what the frozen registry says, with legacy semantics
labelled. Qualified liveness comes from the observations in this section,
not from promoting a CSV transition into a verified closure. Gap rows come
from scoped attempts, including credential failures and cadence skips.
Together these rows distinguish a changed market, a changed policy and a
broken or narrowed observation process.

### Survival and outcome projections

Expose a rebuildable survival view with opportunity/episode IDs, first
observation, last qualified open observation, closure bounds and assumptions,
coverage gaps, censoring type, source scope and evidence references. Join
assessments by their own time rather than using today's score or claiming an
exact "score at death" where the closure date is unknown.

Use this view to investigate crawl cadence and urgency: how often does an
opportunity become unavailable before the next check, and which observations
would reduce that uncertainty? Account for selection and observation gaps
before changing cadence. Listing disappearance is evidence about availability,
not proof of a hire or rejection, and cannot alone supervise hiring success.

Passive lifecycle projections may join the principal's action/outcome timeline
through stable IDs. If materialized into `outcomes.jsonl`, namespace them as
inferred availability events, retain reducer/evidence references, and make
emission idempotent. Corrections append superseding events; rebuilding an index
must not duplicate outcomes or overwrite original application/rejection facts.

## 6. AsOf: inexpensive reads before experiments

The WP2a seal-level CLI is runnable against fixture/sealed bundles, but normal
LoveWork operations have produced no live bundles yet. All commands require an
explicit principal. The built surface is `runs`, `state`, `when`, `diff`, `cat`,
`verify`, and `journal`; `explain` and replay modes remain later work. Role is
required when evaluating a future policy. One implementation must back CLI,
MCP and human renderers; no parallel definitions of temporal truth.

```text
asof runs   --profile lj --before <UTC-time> --limit 20 --json
asof state  --profile lj --at <UTC-time> --json
asof when   --profile lj --org <name> --json
asof diff   --profile lj --from <run-id> --to <run-id> --json
asof cat    --profile lj --run-id <id> --path <relative-path>
asof verify --profile lj [--run-id <id>] [--deep] --json
asof journal --profile lj --from <run-id> --to <run-id> [--write]

# Planned after qualified capture/replay isolation:
asof explain --profile lj --assessment <id> --json
asof replay --profile lj --role general --run <id> --mode recorded
asof replay --profile lj --role general --run <id> --mode reconstruct --dry-run
asof replay --profile lj --role general --run <id> --mode counterfactual --policy <digest> --dry-run
```

`state` returns a knowledge view, with requested/effective cutoffs, selected
seals, lag, unavailable scopes and references. `diff` separates changes in
world evidence, coverage, identity, profile, policy and renderer. A changed
GO count alone cannot attribute cause. `when` offers org search to resolve an
ID, never silently joins similarly named roles. `cat` resolves only frozen,
authorized evidence. `verify` distinguishes bytes intact, references complete
and historical reconstruction possible; these are different checks. `explain`
traces a recorded assessment to its profile/policy, evidence, clock and prior
history without generating a new rationale. A missing link is reported as a gap.

`recorded` returns stored decisions and outputs with no model calls.
`reconstruct` uses the frozen execution specification and clock. Stored model
responses permit deterministic playback of known calls; missing calls or
changed request hashes fail closed. A newly queried hosted model is a new
experiment even with the old model name and seed. Bitwise reproducibility is
claimed only for paths tested to provide it.

`counterfactual` pins the old evidence and explicitly declares changed
policy/profile/model/clock. Previews calculate missing dependencies, proposed
write set and budget without LLM calls; explicit execution uses isolated
`replays/` state. No Gmail, live retrieval, case creation, notification, registry
updates or production assessment-cache writes. A fresh enrichment request is
a separately authorized acquisition with today's observation time. Existing
`--replay-archive` is an operational reprocessing path, **not** this contract.

All reads return a bounded summary, evidence references, warnings with codes,
pagination and a suggested next read. Indexes (initially DuckDB/SQLite if
measurement warrants) store manifest digests and reducer versions and are
rebuildable; stale indexes cannot quietly answer newer questions.

## 7. Git and other useful storage tricks

**Git can expose many snapshots simultaneously.** `git show <commit>:<path>`
reads any committed version without switching checkout. `git cat-file --batch`
reads many objects efficiently; enumerate commit/path pairs when a complete
timeline is needed. For filesystem tools, detached worktrees can materialize
several revisions at once; exports via `git archive` are another option.
Worktrees share Git's object store but their checked-out files still use disk.
See [Git worktree](https://git-scm.com/docs/git-worktree) and
[Git cat-file](https://git-scm.com/docs/git-cat-file).

Use Git for reviewed code, policy and documentation history, with the exact
repository identity recorded (LoveWork has several Git directories). Git
commit dates are not observation times, and Git contains only what was
recorded in it. Ignored caches, private mail and dirty files need explicit
capture. Automatic per-crawl commits are unnecessary and would conflict with
LJ's normal commit workflow; this design needs none.

| Mechanism | Where it helps | Decision for LoveWork |
|---|---|---|
| Hard-linked generations | Ordinary directories; cheap unchanged files; easy `rg`/diff | Initial storage substrate under the sealing contract. |
| Content-addressed objects | Dedup identical bytes across paths and nonadjacent runs | Optional next step; retain run trees/manifests as the readable interface. |
| Immutable per-run ledger segments | Avoid copying a growing JSONL in full every run | First storage optimization if measured growth warrants it; old JSONL becomes a compatibility projection. |
| Btrfs snapshots/reflinks | Copy-on-write storage on supporting filesystems | Optional backend, not a cross-host prerequisite; filesystem snapshots do not supply semantic provenance. |
| Restic | Separate deduplicated backup, with snapshots browsable through a mount | Complement to local history, not a replacement for observation contracts. |

The filesystem and backup alternatives are documented in
[Btrfs subvolumes](https://btrfs.readthedocs.io/en/latest/Subvolumes.html) and
[Restic restore/mount](https://restic.readthedocs.io/en/stable/050_restore.html).
No storage migration is part of this design task.

Capacity must be measured: first-copy bytes + whole changed files per run +
directory/inode overhead. An append-only ledger growing by d each run can
cost roughly `d × n(n+1)/2` across n full-file generations. The earlier
1–2 GB/principal/year estimate is unverified and must not drive retention.
Measure seal duration, bytes copied/linked, inode growth, largest changed
files and restore time across representative full/consume/replay runs.
Set a capacity alert before disk exhaustion. No automatic pruning: retention
must preserve referenced dependencies and requires LJ's explicit decision.

## 7a. How existing components attach to history

| Existing component | Planned integration |
|---|---|
| `runs.jsonl` and wrapper records | Join through common run lineage and sealed manifest references, retaining legacy aliases. |
| `assessments.jsonl` | Add versioned input/evidence references; run membership alone does not identify the exact page scored. |
| `outcomes.jsonl` | Keep actual principal actions/outcomes distinct from evidence-linked inferred availability events. |
| Gmail archive and replay | Share digest/verification tooling; preserve archive times. Operational archive replay remains distinct from isolated AsOf. |
| Legacy CSV archives | Inventory as earlier, incompletely documented evidence; retain original bytes and provenance uncertainty. |
| `rescore.py` / `reassess.py` | Reuse deterministic rule and cached-assessment logic where appropriate under isolated replay; do not inherit live-state mutation behavior. |
| Simulator advert/profile proposals (ch17) | Frozen evidence and pinned effective inputs provide the corpus; no backfill by fetching today's pages. |
| Gold cases (ch18) | Pin evidence, effective inputs, clock and expected behavior; recorded outputs remain separate from stochastic reruns. |
| MANUAL and `report-heal` | Add coverage-aware stability comparison of adjacent runs: observed-empty, unscored, omitted and failed are different outcomes. |
| STATE.md and dashboard | Read manifest/index/journal projections; a later date selector uses the same AsOf contract and displays unavailable history explicitly. |

These bindings are implementation requirements, not claims that existing
schemas already capture every needed reference. Read-only temporal queries,
change journals and integrity checks use local compute and I/O, with no LLM
or network calls. New scoring and acquisition have separately declared costs.

## 8. Bottom-up implementation plan and acceptance gates

Each phase provides value independently and advertises its limitations. These
are future implementation tasks, not completed work in this documentation pass.
[Chapter 28](28-asof-implementation-plan.md) maps these standing gates to the
living work packages, dependencies, writer inventory and rollout. P1 requires
both the tested seal engine and integration of all active writers; fixture-only
completion does not establish production snapshot consistency.

### P0 — establish the boundary and protect existing evidence

- Inventory every writer, input path and source acquisition path; reconcile
  wrapper/pipeline IDs through explicit aliases for legacy records.
- Import surviving CSV archives, reports, emails and Git versions into an
  inventory with provenance grades. Preserve bytes; no historical re-fetch.
- A bootstrap snapshot is dated **now**, even when files claim older dates.
  Mark uncertain timezone/acquisition time and missing inputs explicitly.
- Gate: answer one old-report question with supporting files and an explicit
  list of which stronger historical claims remain impossible.

### P1 — seal bounded run state safely

- Implement a small shared run/seal helper (Python for manifests and lifecycle;
  rsync for copies), atomic lock, start-input pinning and final manifest.
  The wrapper calls it, but direct entry points must participate too.
- Cover full, incremental, consume, replay, reassessment and failed runs;
  late rechecks/heals/delivery use child events. Preview uses temporary state.
- Bootstrap owner consumption only from a sealed, allowed advert corpus.
- Gate: files with identical bytes and eligible matching attributes share
  `(device, inode)` across snapshots; changed bytes do not. Mutating live
  files cannot change old bytes, including when reuse falls back to copying. Exercise same-size
  edits, attribute churn, concurrent writers, failure/interruption, disk-full,
  unsafe symlinks, active SQLite and duplicate IDs. Verify digest and manifest
  closure; no incomplete publish or hidden live fallback.
- Deliverable: browsable history from the first sealed run, labelled
  `state_snapshot` until capture completeness supports a stronger claim.

### P2 — capture observations and causal dependencies

- Add response-time capture including cache hits, failures, expiry, coverage
  and model input/output references at all acquisition/scoring boundaries.
- Preserve exact policy/profile/history consumed; join decisions, outputs and
  owner evidence through IDs. Capture effective time-dependent settings.
- Fix lifecycle finalization to require qualified scope evidence; replay and
  consume must not refresh physical liveness. New views preserve legacy rows.
- Gate: cached replay, failed source, capped feed, changed query, same-day
  rerun and incomplete org traversal cannot create false closure or new life.
  Every newly scored decision resolves to its complete captured input set,
  or explicitly reports an input gap. Crash after fetch retains its evidence.

### P3 — temporal queries and a truthful cockpit

- Implement the rebuildable manifest index, pairwise change journal, temporal
  reducers and read-only AsOf commands; add the shared status projection and
  bounded evidence navigation from chapter 27. Qualify and deduplicate any
  materialized availability events linked into the outcome timeline.
- Gate: late-arriving email, later rejection, identity correction, changed
  profile and consumer delay all honor knowledge cutoffs. Before earliest
  evidence returns unknown; a cutoff during a run reports the boundary.
  Rebuild indexes from sealed history and compare outputs deterministically.
- Deliverable: first/last qualified observations, gap-aware timelines,
  cross-run diffs and operational status without network or LLM calls.

### P4 — replay, evaluation and controlled learning

- Implement isolated playback/counterfactual execution with dependency checks,
  cost previews and no live-data access; bind gold cases to evidence and policy.
- Gate: later outcome/profile/case information cannot leak into replay;
  production files remain byte-identical. Missing model responses invalidate
  exact playback. Original output is retrievable independently of reruns.
- Run chapter 19's join/coverage measurement before calibration; split data
  by time and opportunity family, retain a held-out set, and report uncertainty.
- Deliverable: a policy change can be evaluated against an unchanged corpus,
  explained, and retained or rejected with evidence (chapter 22).

**Priority:** prevent further evidence loss first; build semantics against
captured observations second; optimize storage after measurements. Do not
wait for the simulator to start history, or call a file snapshot a simulator.

## 9. Failure modes and design boundaries

| Failure | Required evidence or guard |
|---|---|
| Quiet feed confused with dead credentials | Scoped acquisition outcome, gap event and cockpit coverage warning. |
| Parser drift yields plausible but scrambled fields | Retained source response, parser version and a replay fixture comparing structured outputs. |
| Mutable registry or ledger is corrupted | Sealed consistent copies, content hashes, row counts and independently rebuildable views. |
| Snapshot changes retroactively | No live hard links, enforced writer boundary and digest verification. |
| Partial snapshot appears complete | Validated seal, atomic publication and explicit interrupted-run recovery. |
| Report totals change without explanation | Journal separates evidence, scope, profile, policy and rendering changes. |
| Historical replay learns from the future | Principal knowledge cutoff and complete pinned dependencies; no live fallback. |

The design starts with files and local readers; database indexing is justified
by measured questions, not by tidiness. A backup complements queryable history
and must preserve or reconstruct its manifests and dependencies. Physical
hard links may be preserved by a suitable backup or restored as independent
copies; semantic evidence identity relies on digests, not inode numbers.
Storage backends may evolve without changing the temporal/query contract.

*Unified 2026-09-14 from the GLM and Codex drafts. Retains the wdbackup
filesystem-first design, explicit journal and operational integration, together
with the audited capture, knowledge-time, liveness and replay constraints.*
