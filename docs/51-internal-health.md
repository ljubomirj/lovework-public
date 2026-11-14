# 51 — Internal Health: One Metric Registry, One Variance

**Status:** Phases 0–5 (2026-11-05): observation, actions, controller,
maintenance clock and the curated corpus. `health.py` reads the existing
authorities and prints one snapshot; `health_actions.py` runs one named repair
under the target/guard contract; `health_loop.py` ranks the repairs and runs them
serially; `maintenance.py` schedules observation every tick, control/deep daily
and a trend weekly; `health_corpus.py`/`health/cases` pin the lessons. None judges
the principal's market. Phase 6 is observe-only calibration.

**The one-line idea:** the detectors already exist (`wiki_audit`,
`monitor.signals`, `tick.audit_receipts`, `pack_index`, `status`). What was
missing is a single written definition of **healthy**, a distance from it, and
a reason to act on the largest distance first. Health is a **projection**, not a
new detector.

---

## 1. The question

LoveWork has many alarms and many repairs. A reader can see a dead link, a pack
gap, a failed receipt and an old heartbeat in four different files, with no
common scale. "Is the system healthy?" had no answer that could be compared with
yesterday's answer.

The health track answers three questions:

1. **What should be true?** — a named target, owned by one authority.
2. **How far is it from true?** — a normalised error in `[0, 1]`.
3. **Which small action reduces the most error?** — ranked, serial, measured.

The target is **internal consistency**, not the principal's judgment. A GO with
no pack, a receipt that claims a missing file, and a wiki link that 404s are
health defects. Whether the principal should pursue a role, write a letter, or
apply is a **principal decision**; it stays in the tick's principal queue and is
not a health variance.

---

## 2. The contract

A metric is a small declaration:

| field | meaning |
|---|---|
| `name` | stable key, `subsystem.fact` |
| `subsystem` | `loop`, `scheduler`, `wiki`, `packs`, `leads`, `receipts`, `repo`, `deploy` |
| `kind` | `counter`, `gauge`, `age`, `bool`, `ratio` |
| `direction` | `lower` is better, `higher` is better, `band` is a range |
| `target` | the good value (or `(low, high)` for a band) |
| `tolerance` | the good-side grace before the error starts (0 in the first slice) |
| `scale` | the distance at which the error reaches `1` |
| `weight` | severity in the weighted mean |
| `source` | **the one function that owns the fact** |
| `freshness` | how long an observation is valid |
| `required` | must this deployment observe it? |
| `authority` | `agent` (internal repair), `principal` (judgment), `history` (evidence limit) |
| `description` | one plain sentence |

The normalisation is deliberately simple:

```
lower:   error = clamp((value - target - tolerance) / scale, 0, 1)
higher:  error = clamp((target - tolerance - value) / scale, 0, 1)
band:    error = distance outside [low - tolerance, high + tolerance] / scale
bool:    error = 0 when true (for target=true), else 1
```

`tolerance` is the good-side grace: a value inside the grace is still healthy,
and the error starts at the edge of the grace. The first slice sets it to `0`
for every metric, because these are counts of discrete defects; the field is in
the contract for a metric that later has natural noise. `scale` answers "how
bad is one?" after the grace.

**Two buckets, never mixed:**

- **classes** — repairable internal defects. These count in the variance.
- **gaps** — history and evidence limits (`sealed_report_links`,
  `junk_title_history`, `implausible_location_history`, `redirect_url`,
  `attic_pages`, `legacy_linkless`, `linkless_go_maybe`). These are reported,
  not repaired and not counted. A sealed report is history; a non-actionable
  FLAG with a junk title is history; an advert that was never linkable is an
  evidence limit.

**Unknown is not healthy.** An applicable required metric with no observation
has status `unknown`, error `1`, and a non-zero `coverage.unknown`. A metric the
deployment cannot use (no hub, no deploy stamp) is `n/a` and is excluded from
the denominator — it is not a blind spot, it is not part of this installation.

---

## 3. The first metric set

Eleven metrics, weight 27. The weights are a judgment, not a measurement; they
are deliberately visible so they can be argued with.

| metric | subsystem | direction | target | tol | scale | weight | source | fresh |
|---|---|---|---:|---:|---:|---:|---|---|
| `loop.heartbeat_age_min` | loop | lower | 150 | 0 | 120 | 3 | `monitor.signals` | 2 h |
| `loop.failed_receipts_24h` | loop | lower | 0 | 0 | 3 | 3 | `monitor.signals` | 2 h |
| `scheduler.failing_jobs` | scheduler | lower | 0 | 0 | 3 | 3 | `monitor.signals` | 2 h |
| `wiki.defects` | wiki | lower | 0 | 0 | 10 | 3 | `wiki_audit.audit_wiki` | 26 h |
| `packs.go_without_pack` | packs | lower | 0 | 0 | 2 | 3 | `wiki_audit.audit_wiki` | 26 h |
| `packs.go_unprepared` | packs | lower | 0 | 0 | 2 | 2 | `wiki_audit.audit_wiki` | 26 h |
| `leads.missing_page` | leads | lower | 0 | 0 | 10 | 2 | `wiki_audit.audit_wiki` | 26 h |
| `receipts.audit_defects` | receipts | lower | 0 | 0 | 3 | 2 | `tick.audit_receipts` | 26 h |
| `repo.hub_lag` | repo | lower | 0 | 0 | 3 | 2 | git: principal HEAD vs hub | 2 h |
| `deploy.code_dirty` | deploy | lower | 0 | 0 | 1 | 2 | git status of the run tree | 2 h |
| `deploy.stamp_matches` | deploy | bool | true | 0 | 1 | 2 | `deployed.json` vs HEAD | 2 h |

Notes:

- `loop.heartbeat_age_min` uses the monitor's own 150-minute stale line, so the
  health module cannot disagree with the alarm that is already read.
- `wiki.defects` is the **text/link/title remainder**: the pack and lead classes
  are separate metrics, so one defect is counted once.
- `repo.hub_lag` is commits in the principal HEAD that the deploy hub does not
  have. It is `required=False`: a principal running their own copy may not have
  a hub.
- The two `deploy` metrics are also `required=False`: a development checkout has
  no `deployed.json`; the metric is `n/a`, not a defect. `LOVEWORK_DEPLOY_ROOT`
  lets a dev checkout observe the production deployment without pretending its
  own tree is the deployed one.

---

## 4. The snapshot and the variance

Run it from `lovework-agent/`:

```bash
../venv/bin/python3 health.py --profile lj --explain          # the metric table
../venv/bin/python3 health.py --profile lj --json             # the whole snapshot
../venv/bin/python3 health.py --profile lj --json --write     # store latest + history
../venv/bin/python3 health.py --profile lj --deep --write     # fresh audits, no cache
```

`health.py --profile lj` prints:

```
lj: variance 0.222 · coverage 11/11 · 2026-11-04T21:22:35+00:00
  ...
  worst: loop.failed_receipts_24h 1.00, packs.go_without_pack 1.00
```

The scalar is the weighted mean:

```
variance = sum(weight * error) / sum(weight)      # over applicable metrics
```

For the live run below the numbers are **6 / 27 = 0.222** (weighted error 6,
weight 27). A single wiki defect at weight 3 and scale 10 gives `0.3`, and
`3 * 0.3 / 27 = 0.033`; the scalar is a trend, the per-metric errors are the
truth. The compact table above the scalar is what a reader should act on.

The snapshot also carries:

- `by_subsystem` — the weighted mean inside each subsystem.
- `worst` — the five largest `weight * error`.
- `rose` — names whose error rose by more than `0.01` against the last
  `history.jsonl` row.
- `coverage` — `required`, `applicable`, `observed`, `unknown`.
- `gaps` — the evidence limits, unchanged.

Writes are explicit: `--write` writes `state/<p>/health/latest.json` and appends
`state/<p>/health/history.jsonl`. The wiki audit and receipt audit are cached in
`state/<p>/health/{wiki-audit,receipts-audit}.json` for one hour; `--refresh` or
`--deep` forces a fresh read. A read without `--write` touches nothing.

Exit codes: `0` healthy, `1` defects, `2` a required metric is unknown, `3` no
principal state.

---

## 5. What the first live run found (lj, 2026-11-04 21:22)

Run read-only from the dev tree against the live state. The first read, before
the pack repairs, was:

```
variance 0.222          weighted error 6 / weight 27
coverage required 8, applicable 11, observed 11, unknown 0
wiki.defects              0   (2,166 org pages; clean)
packs.go_without_pack     2
packs.go_unprepared       0
leads.missing_page        0
receipts.audit_defects    0
loop.failed_receipts_24h  4   (the 10:05–16:05 profile-shadow incident)
loop.heartbeat_age_min   77.5 (last heartbeat 20:05)
scheduler.failing_jobs    0
repo.hub_lag              0   (HEAD == hub)
deploy.code_dirty         0
deploy.stamp_matches      true (e5a2ef6a3)
```

After the repairs, the same read was:

```
variance 0.111          weighted error 3 / weight 27
coverage required 8, applicable 11, observed 11, unknown 0
wiki.defects              0   (0 class(es), 2,166 pages)
packs.go_without_pack     0
packs.go_unprepared       0
leads.missing_page        0
receipts.audit_defects    0
loop.failed_receipts_24h  4   (unchanged: the 24-hour window)
loop.heartbeat_age_min   90.8
scheduler.failing_jobs    0
repo.hub_lag              0
deploy.code_dirty         0
deploy.stamp_matches      true
```

The two pack gaps were real, not health-module noise:

- **Elman** — the pack is `20261011-Elman_Deep_Science_Ventures-Co-founder_CTO-LoveWork`;
  the wiki block says `Elman`. The pack index keyed the advert URL with the pack's
  org string, so `Elman (Deep Science Ventures)` never met `Elman`. The case field
  is also a markdown link, `[https://…](https://…)`, so the index's "URL" was not
  the raw URL either.
- **Soniox GPU** — the 10-03 and 10-04 copies had each been marked "superseded by"
  the other, so no live pack existed. A cycle is not a duplicate; both marks were
  true statements about the other file.

Both are fixed in the code:

- `pack_index` and `pack_for` add the **raw advert URL** as a key; the audit asks
  with the same raw URL. `_field_url` extracts the first URL from the markdown
  link, so the index stores the raw form. The org is written twice and drifts;
  the link is copied.
- `mark_superseded_duplicates` detects a cycle in its own group and removes the
  marker from the chosen best (ready, else newest). The other marker stays. The
  live Soniox group was repaired this way: one marker removed, one kept.
- `tick.audit_receipts` counts a duplicate only when **more than one** pack in a
  group is live. The earlier test (`packs[1:]`) called an older ready pack with a
  marked newer scaffold a defect.

The only non-zero metric after the pack repairs is `loop.failed_receipts_24h`:
4 failures from the 10:05–16:05 incident, inside the monitor's 24-hour window.
They are history, not a current failure; they age out on 2026-11-05, and the
honest variance then reaches `0 / 27 = 0`. The wiki itself is 0 defects on
2,166 pages. The unit tests pin both ends: all-healthy readings give variance 0,
and one missing pack gives `3 / 27`.

---

## 6. Actions (Phase 2 — `health_actions.py`)

An action is a small, idempotent, agent-owned repair:

```yaml
name: packs.prepare_missing
metric: packs.go_without_pack
owner: agent                 # never principal judgment
authority: docs/45 §5
precondition: writer lease free; current GO has no live pack
action: application_packs.prepare_missing_packs(principal)
postcondition: the class is re-read and cannot rise
guard: work_auth.location_evidence
expected_reduction: 1.0 per pack
cost: seconds, one model call only if investigation is needed
receipt: before/after classes, duration, files
```

`health_actions.py` turns that contract into one command. It observes with the
same `health.collect` as Phase 1, checks the precondition, takes one writer
lease, calls the existing authority, observes again with a fresh audit, and
accepts or rejects the result.

### 6a. The executable registry

| action | target metric | guard | owner | executable |
|---|---|---|---|---|
| `selfheal.pass` | `wiki.defects` | `work_auth.location_evidence` | agent | yes |
| `packs.prepare_missing` | `packs.go_without_pack` | `wiki.defects` | agent | yes |
| `packs.reconcile_duplicates` | `receipts.audit_defects` | `packs.go_without_pack` | agent | yes |
| `leads.prepare_missing` | `leads.missing_page` | `wiki.defects` | agent | yes |
| `receipts.audit` | `receipts.audit_defects` | — | agent | yes (read-only check) |
| `repo.publish` | `repo.hub_lag` | — | external | **no** |

The repairs are the one authority each: `selfheal.run_pass` /
`selfheal.selfheal`, `application_packs.prepare_missing_packs`,
`application_packs.mark_superseded_duplicates`, `leads.prepare_missing_leads`,
and `tick.audit_receipts`. The action module does not carry a second list of
packs, leads, or receipt paths.

**`repo.publish` is deliberately blocked.** Publishing rides the scheduled
`scripts/cron/repo.sh` (the principal repo's commit at a verified seal and the
daily morning commit); the health loop must not become a second writer on that
rail. Agents may commit from a checkout — AGENTS.md §8, revised 2026-11-13 —
but the automation path keeps exactly one publisher. Health reports
`repo.hub_lag`; it does not publish it.

**`receipts.audit` is a check, not a repair.** It re-runs the receipt detector
and reports defects. It has no guard and is never ranked as a reducing action.

**`packs.prepare_missing` cuts a CUT pack, not a prepared one.** It moves the
defect from `go_without_pack` to `go_unprepared`; the tick's `complete_pack`
remains the principal-queue action that fills the material. The health action is
accepted when the target error falls more than the guard rises; it does not
pretend the pack is finished.

### 6b. The envelope

- **Precondition.** Every required metric must be observed and above its target
  (`error > 0`). `packs.reconcile_duplicates` also requires the
  `pack_duplicates` kind in the receipt-audit detail; a different receipt defect
  does not select it. An unknown required metric is exit 2, not a green light.
- **One writer.** The repair actions (`packs.prepare_missing`,
  `packs.reconcile_duplicates`, `leads.prepare_missing`) run inside
  `standalone_writer_guard` with mode `health`. `selfheal.selfheal` owns its own
  lease and baseline; the health wrapper calls `selfheal.run_pass` only for the
  report-only dry run. The read-only check takes no lease. A busy lease is exit
  75.
- **Before and after.** The before snapshot uses the existing cache (the
  measurement is not paid twice); the after snapshot uses `deep=True` and a
  fresh wiki and receipt audit. A dry run reuses the before snapshot: no repair
  was applied, so there is no new state to measure.
- **Acceptance.** Both errors are in the metric's normalised units:

  ```
  target_delta = before.target_error - after.target_error
  guard_delta  = after.guard_error  - before.guard_error
  accepted     = target_delta > 0 and guard_delta <= target_delta
  ```

  The rule is the written one: an action whose guard worsens more than its
  target improves is rejected. A target that does not fall is rejected; a target
  that becomes unknown is exit 2. The variance is recorded as context, not used
  as the acceptance test.
- **The location guard.** `work_auth.location_evidence` captures the long/prose
  `Location:` values and the values carrying a work-authorisation or work-mode
  token (visa, sponsorship, remote, hybrid, UK/US/EU, …) before `selfheal.pass`.
  After the pass it checks that each exact value is still somewhere in the wiki
  (the repair moves it to a Note; it must not delete it). A lost value is a guard
  error of `lost / total`. HTML entity unescaping is accepted as preservation.
- **Receipt.** `--write` appends a compact row to
  `state/<p>/health/actions.jsonl` and, when `--apply` is set, writes the after
  snapshot through `health.write_snapshot` (`latest.json` + `history.jsonl`).
  The receipt names the target and guard before/after, the variance before/after,
  the repair stats, the acceptance, and the duration.
- **Exit codes.** `0` accepted / healthy / check clean; `1` rejected or repair
  error; `2` unknown or blocked; `3` no principal state; `75` writer busy.

### 6c. Live verification (2026-11-04)

`health_actions.py --list` prints the table above. A live dry run on lj's
healthy state skipped `packs.prepare_missing` (`packs.go_without_pack healthy`).

A temp fixture with one GO and no pack exercised the full path:

```
target packs.go_without_pack: 1 -> 0 (error 0.50 -> 0.00)
guard  wiki.defects:           0 -> 0
variance                       0.18 -> 0.16
accepted: yes — target fell and the guard stayed within the fall
repair: created 1, existing 0, skipped 0
```

The overall variance was still above zero because the fixture's loop and deploy
metrics are unknown and the new pack is CUT (`packs.go_unprepared` 1); the
acceptance is on the target and guard, exactly as written. The receipt landed in the fixture's
`state/example/health/actions.jsonl`. Unit tests: 14 passed in
`tests/test_health_actions.py`.

---

## 7. The controller (Phase 3 — `health_loop.py`)

```
observe -> rank -> pick one -> act -> observe again
        -> accept if the target fell and the guard did not
        -> receipt -> stop at the slot, a repeated no-effect action,
           a repeated state, or an unrecoverable boundary
```

The controller is serial by construction: one writer lease, one action, one
fresh after-snapshot. It does not run in the hourly tick's principal queue; Phase
4 will place it in the maintenance window after the principal work.

### 7a. The rank

For each executable, non-read-only action whose precondition passes:

```
expected_reduction = weight * target_error / total_applicable_weight
                     * action.expected_reduction
cost               = measured_seconds if the action has run, else cost_seconds
score              = expected_reduction / cost
```

The target error is the current snapshot's normalised error. The plan is sorted
by score descending, then action name, and printed in the dry run. The cost is
the registry estimate until the action has run once; after that it is the
measured duration, so a slow repair stops outranking a cheap one.

### 7b. The stop boundaries

| reason | condition | exit |
|---|---|---:|
| `healthy` | variance 0 and no required unknown | 0 |
| `unknown` | variance 0 but a required metric is unknown | 2 |
| `no_action` | variance is non-zero but no action has an eligible target | 1 / 2 |
| `dry_run` | default mode: the plan was returned, nothing ran | 1 / 2 |
| `max_actions` | `--max-actions` reached | 1 / 2 |
| `slot_budget` | `--max-minutes` reached | 1 / 2 |
| `no_effect_twice` | the same action failed to reduce its target twice | 1 |
| `state_repeated` | a different action left the same measured state | 1 |
| `writer_busy` | the action's lease was busy | 75 |
| `action_error` | the repair raised or returned an error | 1 / 2 |
| `guard_rejected` | the guard worsened more than the target improved | 1 |
| `target_unknown` | the target could not be measured after the action | 2 |
| `missing_state` | the principal has no state root | 3 |

`1 / 2` means `2` when coverage has a required unknown and `1` when the variance
is a known defect; zero only when both are clean. Unknown is blindness, not a
known defect.

**No-effect twice vs repeated state.** A no-effect action increments a per-action
counter; two attempts of the same action stop with `no_effect_twice`. A
**different** action that leaves the state unchanged after the previous state
stops immediately with `state_repeated`. An accepted action resets the
no-effect counter and makes its fresh after-snapshot the next observation.

### 7c. The command and receipts

```bash
../venv/bin/python3 health_loop.py --profile lj                 # dry-run plan
../venv/bin/python3 health_loop.py --profile lj --apply --write # act, receive
../venv/bin/python3 health_loop.py --profile lj --apply --write     --max-actions 3 --max-minutes 8 --deep
```

Dry run is the default: it observes, ranks, and returns the plan; it runs no
action. `--apply` runs the plan; `--write` appends `health/controller.jsonl` and
passes `write=True` to each action, so the action's own `health/actions.jsonl`
row and after-snapshot are recorded. The controller row carries the before/after
summary, the initial plan, each chosen action and compact receipt, the stop
reason, the action count, and the duration.

Exit codes: `0` healthy/clean, `1` defects remain, `2` unknown required metric,
`3` no principal state, `75` writer busy.

### 7d. Verification (2026-11-05)

Live lj dry run:

```
variance 0.111111 -> 0.111111   (weighted error 3 / weight 27)
coverage 11/11 observed, 0 unknown
plan: []
stop: no_action — variance is not zero but no action has an eligible target
exit: 1
```

The only non-zero metric is the four failed receipts inside the monitor's
24-hour window; no action in the registry repairs a historical failed receipt.
The controller correctly refuses to invent one. The window ages out on
2026-11-05; the same dry run then stops `healthy`.

Temp fixture with one GO and no pack, `--apply --max-actions 1 --write`:

```
plan: packs.prepare_missing (target error 0.50, cost 30 s, score 0.002)
action: accepted — target packs.go_without_pack 1 -> 0 (error 0.50 -> 0.00)
variance: 0.18 -> 0.16
stop: max_actions
exit: 2   (the fixture has no heartbeat, so one required metric is unknown)
```

The action was accepted; the `exit 2` is the fixture's missing heartbeat, not a
repair failure. The action receipt and the controller row were written in the
fixture's `health/`. Unit tests: 15 passed in `tests/test_health_loop.py`.

---

## 8. The clock (Phase 4 — `maintenance.py`)

The clock is the maintenance phase, not the principal action queue. Five passes
sit in `maintenance.PASSES` in this order:

| pass | period | command | ceiling |
|---|---|---|---|
| `health-observe` | every tick | `health.py --write` | 120 s |
| `health-control` | daily | `health_loop.py --apply --write --max-actions 3 --max-minutes 3` | 240 s |
| `health-deep` | daily | `health.py --deep --write` | 120 s |
| `health-trend` | weekly | `health.py --trend --write` | 60 s |
| `brief` | every tick | reads the newest health line | — |

They run **after** `self-heal`, `watch` and `packs`, and **before** the brief.
The controller is daily and bounded; the observation is every tick. The
maintenance runner shares the tick's deadline, so a pass that cannot fit is
deferred by name rather than started. `health-control`'s 240-second process
ceiling contains its own three-minute controller budget.

**Markers.** The daily passes use `cache/health-control-<date>`,
`cache/health-deep-<date>`; the weekly trend uses
`cache/health-trend-<ISO-year>-W<week>`. A pass that returns an expected health
exit (`0` healthy, `1` defects remain, `2` an unknown required metric) writes its
marker. `75` from the writer lease is a deferral, not a failure. The controller
does not retry a stopped day on the next tick; the hourly observation still runs
and the monitor/brief can say what is unresolved.

**The brief** reads `health/latest.json` and `history.jsonl` through
`health.read_status` and adds one line via `health.brief_line` to the brief it
writes: variance, previous/delta, unknown count, a rise, and the worst metric. It
does not carry the whole table. A quiet hour writes no brief at all.

**The monitor** reads the same stored status in `monitor.signals`, prints it in
`_facts_text`, and the prompt treats a **rise** or a **required unknown** as the
only health alarm; a steady non-zero variance is quiet. The monitor does not
recompute health.

### 8a. Verification (2026-11-05)

A temp fixture ran the four health passes through `maintenance.py --only`:

```
first run:  ran health-observe, health-control, health-deep, health-trend
            health-control applied one pack; health/history.jsonl, latest.json,
            actions.jsonl, controller.jsonl and trend-2026-W45.md written
second run: ran health-observe; skipped health-control, health-deep, health-trend
            (daily/weekly markers)
```

The health passes reported `alarms` because the fixture has no heartbeat, so one
required metric is unknown (exit `2`); that is the fixture's blind spot, not a
pass failure. A live lj dry run of the controller at 2026-11-05 03:53 found
variance `0.111111` (weighted error 3 / weight 27), coverage 11/11 observed and
0 unknown, no eligible action, and stopped `no_action`. No live state was written
by this phase.

---

## 9. Testing against LEARNINGS and MEMORY (Phase 5 — the corpus)

The corpus is `health/cases/*.yaml`, loaded and run by
`lovework-agent/health_corpus.py`; the tests are
`lovework-agent/tests/test_health_cases.py`. It is **curated data**, not prose
parsing. Every case names a source anchor, a before/after metric fixture, the
action and guard, and the expected outcome:

```yaml
- id: soniox-supersession-cycle
  source: {file: LEARNINGS.md, anchor: "A supersession cycle is zero live packs"}
  action: packs.reconcile_duplicates
  before:
    receipts.audit_defects: {value: 1, detail: pack_duplicates}
  after:
    receipts.audit_defects: 0
  guard: packs.go_without_pack
  expect: accepted
  note: breaking the cycle restores one live pack; the guard must not rise
```

The loader validates the source file, the exact anchor, the action and metric
names, the guard, and the expectation. A lesson that is rewritten or removed
breaks its case, which is the point: the corpus must be re-read, not silently
kept.

### 9a. What the corpus covers

- **Every agent repair class**: `selfheal.pass` (`wiki.defects`),
  `packs.prepare_missing` (`packs.go_without_pack`),
  `packs.reconcile_duplicates` (`receipts.audit_defects`), and
  `leads.prepare_missing` (`leads.missing_page`).
- **The guard that can be harmed**: `work_auth.location_evidence`, with one
  accepted case (the value moved to a Note) and one `guard_rejected` case (the
  value lost).
- **A repair that leaves its target in place is `rejected`**, not a success and
  not a loop; the controller records the miss and stops.
- **The false-positive classes**: sealed-report links, non-actionable junk
  titles/implausible-location history, and a steady complete state. They are
  `healthy`; the gaps do not make an action eligible.
- **No action is the honest answer**: `packs.go_unprepared` (the principal
  queue owns `complete_pack`) and the historical 24-hour failed-receipt window.
- **MEMORY is narrative, not a detector**: one case is anchored in `MEMORY.md`;
  it confirms that a healthy snapshot stays healthy because a turn log cannot
  mint a metric.
- **A golden snapshot shape** pins the keys of a snapshot, the metric rows, the
  variance block and the rendered header/gaps line — the shape, not live counts.
- **A simulated week** runs seven controller days: accepted → healthy,
  no-effect twice, repeated state after a different action, no action,
  healthy, max actions, healthy. Every day stops; the controller never chases a
  no-effect action across the week.

The current corpus has 13 cases (12 `LEARNINGS.md`, 1 `MEMORY.md`);
`tests/test_health_cases.py` has 5 tests and passes.

### 9b. No rollback

No health action is transactional. A guard rejection stops the controller and
is recorded, but the repair authority's mutation is not undone; the
`work_auth.location_evidence` repair itself preserves the value in a Note, so
the rejected case is the stop path, not a rollback path. The corpus therefore
has `guard_rejected`, not `rolled_back`. A true rollback would need a page/pack
snapshot and restore, which the additive one-writer contract does not provide.

---

## 10. Design rules

1. **One authority per fact.** If two modules disagree, the health metric names
   the one that owns it and the other is a bug.
2. **History is not a defect.** Sealed reports and non-actionable FLAG/DROP rows
   are gaps. Repairs never rewrite them.
3. **Unknown is not healthy.** Coverage is part of the report.
4. **No double counting.** `wiki.defects` excludes the pack/lead classes.
5. **Internal consistency only.** Principal judgment stays in the principal
   queue.
6. **Serial and bounded.** One writer, one action, a measured postcondition.
7. **Additive.** A repair moves or marks; it does not delete.
8. **The metric is the receipt.** Before and after use the same read.
9. **The scalar is a trend.** The per-metric table is the truth.
10. **No commit or push from health.** LJ reviews and commits.

---

## 11. Phase status

| phase | state |
|---|---|
| 0 — contract | this chapter |
| 1 — observe | `lovework-agent/health.py`, `tests/test_health.py`; live read on lj |
| 2 — actions | `lovework-agent/health_actions.py`, `tests/test_health_actions.py`; one action per invocation, dry-run by default; live skip and temp-fixture repair verified |
| 3 — controller | `lovework-agent/health_loop.py`, `tests/test_health_loop.py`; dry-run plan, serial apply, score, slot bounds, no-effect and repeated-state stops, controller receipt; live dry run and temp-fixture apply verified |
| 4 — clock | `maintenance.py` health passes (observe every tick, control/deep daily, trend weekly), `health.read_status`/`brief_line`/`--trend`, `brief.py`/`monitor.py` health lines, tests; temp-fixture clock verified |
| 5 — corpus | `health/cases/*.yaml`, `health_corpus.py`, `tests/test_health_cases.py`: 13 sourced cases, golden snapshot shape, simulated week |
| 6 — calibrate | observe-only week, then one subsystem at a time |

The next action is **Phase 6**: observe-only calibration. Run the clock in
report-only mode for a week, compare the predicted reductions with the measured
ones, then enable the controller one subsystem at a time. The corpus is the gate;
the live schedule stays off until that observation is reviewed.
