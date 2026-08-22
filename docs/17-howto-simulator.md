# Chapter 17 — HOWTO: Simulator from the Historical Dataset

> **Audience:** builders and agents who want to turn LoveWork's crawl history
> into a working simulation of work-life decisions.
> **Status:** roadmap, not implemented.  See also
> [`09-intelligence-layer.md`](09-intelligence-layer.md) (thesis),
> [`11-agentic-intelligence-harness.md`](11-agentic-intelligence-harness.md)
> (harness design), [`13-probabilistic-simulator-model.md`](13-probabilistic-simulator-model.md)
> (formal frame), [`../DECISIONS.md`](../DECISIONS.md) D17 (profile model).

## Why a HOWTO exists separately from the design docs

Chapters 09, 11, and 13 already lay out *what* LoveWork should build and *why*.
This chapter says *how* — the concrete, phased, implementable path from the
existing dataset to a working simulator.  It is meant to be executed, not
merely read.

The core insight shared with the ARC-AGI-3 Schema harness:

```text
editable world model (person + company + process)
    -> plan inside the model (free)
    -> execute cheapest informative real-world action
    -> observe outcome
    -> update model
```

LoveWork can borrow the epistemic pattern without copying the benchmark
machinery.  The "world" here is not a grid of pixels but a person's career
ecosystem.

---

## Phase 0 — Take stock: what exists already

Before building anything, know what you have.

### The decision ledger (live, growing)

| Stream | LJ rows | VJ rows | Content |
|--------|---------|---------|---------|
| `runs.jsonl` | 22 | 7 | Every pipeline run: profile, role, sources, model, git commit, profile hash |
| `assessments.jsonl` | 2391 | 1119 | Every scored finding: scores, decision, reasoning, advert url, lifecycle status |
| `outcomes.jsonl` | 209 | 0 | Passive outcome events: applications, rejections, interviews (from `history.py`) |

Ledgers live under `lovework-agent/dataset/` and `state/<principal>/dataset/`.

Each assessment already carries:
- `fit_score`, `reach_score`, `flourish_score`, `combined_score`
- `recommended_action` (APPLY_NOW, WARM_INTRO_ONLY, USE_AS_GAP_SIGNAL, DROP, …)
- `reasoning` (free-text LLM rationale)
- `advert_hash`, `url`, `org_name`, `title`, `location`
- `source`, `run_id`, `policy_version`
- `lifecycle_status` (new, still_open, disappeared, long_lasting)

### The registry (live)

`jobs.csv` under each principal's `cache/` — a flat file tracking every job
ever seen, with lifecycle transitions.  Archived snapshots in `cache/archive/`.

### The wiki (live)

591 org pages, per-run reports, full index — markdown, machine-readable.

### The profiles (live, multi-principal)

Three-layer model (long path / current tip / branching possibilities) + soul +
work_auth + roles/ for LJ, VJ, KJ, PK.

### What is *not* built yet

- **Person simulator**: a runtime model that can simulate "would this person
  like/thrive in this work?" *before* running the LLM matcher.  The matcher
  scores; it does not simulate trajectories.
- **Company/hiring simulator**: a model of an org's real hiring intent, filters,
  decision latitude, and stage — separate from the advert text.
- **Process simulator**: a model of how the harness itself interacts with the
  world (sources, timing, uncertainty, information value).
- **Calibration**: the ledger has 0 outcome-calibrated priors.  Every score is
  still an LLM guess.
- **Reflection loop**: no periodic review of decisions vs outcomes.
- **Regression harness**: no test suite of past judgment cases.

---

## Phase 1 — Make the existing data simulator-ready

### 1a. Backfill outcome links to assessments

Currently outcomes are *org-level* (history.py proves prior contact with an
org, not always which advert).  Add a `match_assessment_to_outcome` script:

```python
# For each outcome entry, try to find the assessment that predicted it:
#   - exact advert_hash match
#   - org_name + title fuzzy match
#   - org_name + date window match
# Store matched outcome_id + observed_decision in the assessment row.
```

This gives the first calibration pairs: *we scored this X, and the real outcome
was Y*.  Without this, the ledger is a diary, not training data.

**Checkpoint:** a CLI that shows, for any assessment, whether we know what
happened next.

### 1b. Structured assessment schema

The current assessment JSONL is free-form at the edges.  Add a Pydantic model
for the canonical assessment row with validation on write:

```python
class AssessmentRow(BaseModel):
    event_type: Literal["assessment"]
    assessment_id: str
    run_id: str
    advert_hash: str
    observed_at: datetime
    profile_name: str
    role: str
    source: str
    org_name: str
    title: str
    url: str
    fit_score: float          # 0-10
    reach_score: float        # 0-10
    flourish_score: float     # 0-10
    combined_score: float     # 0-10
    decision: str             # GO | MAYBE | FLAG | DROP
    recommended_action: str
    reasoning: str
    policy_version: str
    lifecycle_status: str
    # New fields for simulator feed:
    matched_outcome_id: str | None = None
    matched_outcome_kind: str | None = None  # applied | rejection | interview | offer | ignored
    matched_outcome_date: date | None = None
```

Run a validation pass over existing rows.  Reject/fix rows that don't conform.

### 1c. Advert corpus as a first-class dataset

Each assessment references an advert via `advert_hash`.  But the advert text
itself — the primary content the LLM scored against — is not in the dataset.
Add an `adverts.jsonl` or `adverts/` directory keyed by `advert_hash`:

```json
{
  "advert_hash": "sha256...",
  "fetched_at": "2026-07-22T08:23:34",
  "fetch_method": "firecrawl",
  "org_name": "FAR.AI",
  "title": "Research Engineer",
  "url": "https://...",
  "raw_text": "...",
  "extracted_text": "...",
  "char_count": 1234,
  "source": "research_orgs"
}
```

This enables:
- Re-scoring old adverts with new policies
- Comparing reasoning quality across adverts
- Training/fine-tuning on the actual content, not just the hash

**Script:** `backfill_adverts.py` — reads every assessment, re-fetches the URL
(if still live), or copies from cached `lovework-agent/cache/page_*.md` files
when available.

### 1d. Profile snapshots per run

Each run already records `profile_hash`.  Store the actual profile text at
that point in time under `state/<principal>/dataset/profiles/<run_id>/`:

```text
lovework-agent/dataset/profiles/<run_id>/
  soul.md
  cv-short.md
  bio-long.md
  possibilities.md
  work_auth.md
```

This is critical: the profile that generated the scores is versioned alongside
the scores.  Without it, you can't tell whether a score improvement came from a
better matcher or a better profile.

---

## Phase 2 — Build the simulator scaffold

### 2a. Person simulator (v0)

A Python class that holds the principal's current state and can answer "would
this person like this work?" without calling an LLM for every comparison:

```python
class PersonaSimulator:
    profile: PrincipalProfile   # loaded + hashed
    decision_history: list[AssessmentRow]  # recent N
    outcome_history: list[OutcomeRow]

    def fit_estimate(self, opportunity: Advert) -> SimFit:
        """Fast pre-LLM estimate: capability match, taste alignment,
        constraint check (visa, location, money pressure)."""
        ...

    def trajectory_estimate(self, opportunity: Advert) -> SimTrajectory:
        """Where would this lead?  Branching future scenarios."""
        ...

    def simulate_lived_experience(self, opportunity: Advert) -> SimExperience:
        """Thrive, stagnate, burn out, grow, learn, regret — with
        evidence for each."""
        ...
```

The simulator is *not* an LLM wrapper.  It uses the LLM for inference but
treats the output as structured data, not prose.  The v0 should be a thin
layer over the existing matcher with explicit uncertainty labels.

### 2b. Company/hiring simulator (v0)

```python
class CompanySimulator:
    org_state: dict  # from wiki, registry, prior-contact, external sources

    def hiring_reality(self, org: str, role: str) -> SimHiring:
        """P(role is real), P(company will hire), stage, funnel,
        decision latitude, typical timeline."""
        ...

    def screening_model(self, principal: PersonaSimulator, org: str, role: str) -> SimScreening:
        """How would this principal look through this org's filters?
        ATS, recruiting team, hiring manager — with uncertainty."""
        ...

    def reach_assessment(self, principal: PersonaSimulator, org: str, role: str) -> SimReach:
        """What would change the screening outcome?
        Introduction? Portfolio? Different angle? Timing?"""
        ...
```

Sources for the company model:
- Org wiki pages (already exist for 591 orgs)
- Company register lookups (incorporation date, officers, accounts)
- Prior-contact history (applications, rejections, interviews)
- LinkedIn seeds and auth-wall observations
- Gmail LJ-jobs label history

### 2c. Process simulator (v0)

```python
class ProcessSimulator:
    sources: list[Source]
    scheduler: Schedule

    def coverage(self, principal: str, role: str) -> SimCoverage:
        """What fraction of relevant opportunities does the current
        source config actually see?"""
        ...

    def information_value(self, action: str) -> float:
        """How much would this action reduce uncertainty about fit,
        reach, or flourish?"""
        ...

    def expected_value_of_action(self, action: str, person: PersonaSimulator,
                                  company: CompanySimulator) -> float:
        """P(offer) × value of accepting − cost of action.
        See chapter 13 formula."""
        ...
```

The process simulator is the "harness model" — it knows what LoveWork itself
knows and doesn't know, and can recommend the cheapest informative next step.

---

## Phase 3 — Connect the simulators into a rehearsal loop

### 3a. The rehearsal object

```python
class Rehearsal:
    opportunity: Advert
    person: PersonaSimulator
    company: CompanySimulator
    process: ProcessSimulator

    person_fit: SimFit
    person_trajectory: SimTrajectory
    company_hiring: SimHiring
    company_screening: SimScreening
    process_coverage: SimCoverage

    competing_hypotheses: list[tuple[str, str, float]]
    # e.g. ("role is real", "proven by repeat advert + named manager", 0.8)
    #      ("role is ghost", "advert 6mo old, no hires in org", 0.2)

    recommended_experiment: str
    # The single action that maximises information per unit cost
    # Options: read_primary_page, try_product, ask_manager,
    #          seek_introduction, apply, defer, ignore

    expected_value: float
    confidence: str  # low | medium | high
```

### 3b. The rehearsal command

```bash
# Rehearse a specific opportunity
../venv/bin/python3 rehearse.py \
    --profile lj --role general \
    --url "https://..." \
    [--advert-hash "..."]

# Rehearse all new GO/MAYBE in the latest report
../venv/bin/python3 rehearse.py \
    --profile lj --role general \
    --from-latest-report

# Rehearse every opportunity in the last assessment set (batch mode)
../venv/bin/python3 rehearse.py \
    --profile lj --role general \
    --from-dataset --since 7d
```

The rehearsal output is a structured markdown block:

```markdown
## Rehearsal: Research Engineer @ FAR.AI

**Person fit:** 8/10 — strong ML/AI alignment, solid portfolio.
  → The fit estimate is *high confidence* (clear evidence in CV).

**Hiring reality:** medium — FAR.AI is real and growing, but
  the role may attract many applicants.
  → Key unknown: how competitive is the current pipeline?

**Screening model:** The principal has no professional SE experience.
  This is a *medium-risk* gap for a Research Engineer title.
  → Experiment: Does the advert emphasise research output or
     engineering maturity?

**Trajectory:** This role leads toward AI safety research engineering.
  → Consistent with branch (a) — AI/ML research, applied ML.

**Recommended experiment:** Read the primary careers page to check
  if the role values research output over SE background.

**Expected value of applying:** medium (fit is good, reach is
  uncertain, but application cost is low).
```

### 3c. The feedback loop

Once a rehearsal is produced and the human acts, record the outcome back
into the ledger.  This is what calibrates the simulators over time:

```python
# After human acts on rehearsal:
record_feedback(
    rehearsal_id=rehearsal.id,
    action_taken="applied",
    outcome="rejected_at_screening",
    human_reflection="They wanted 5y SE experience I don't have",
    updated_belief="screening_model.reach_score -= 2 for similar roles"
)
```

---

## Phase 4 — Calibrate from outcomes

### 4a. Calibration script

```bash
../venv/bin/python3 calibrate.py --profile lj --min-outcomes 10
```

Output: a table showing, for each score band (0-2, 2-4, 4-6, 6-8, 8-10),
what fraction of roles in that band resulted in a positive outcome.

```text
Score band  | Count | Applied | Interview | Offer | Offer rate
0-2         |  1200 |      12 |         0 |     0 |       0.0%
2-4         |   600 |      18 |         1 |     0 |       0.0%
4-6         |   300 |      25 |         3 |     1 |       0.3%
6-8         |   150 |      30 |         8 |     2 |       1.3%
8-10        |    50 |      20 |        10 |     5 |      10.0%
```

This is the first reality check on the matcher's priors.  It will likely show
that scores are overconfident relative to outcomes — which is expected, and
exactly what calibration fixes.

### 4b. Calibrated score vs raw score

The simulator should present *both* the raw matcher score and a
calibrated-by-outcomes posterior.  Over time, the posterior becomes the primary
score and the raw score becomes a diagnostic.

### 4c. Drill-down by failure mode

For rejected applications, classify the failure mode:

| Failure mode | What it means | How to detect |
|---|---|---|
| credential gap | Missing degree/cert/experience the advert requires | Parse rejection + compare to advert requirements |
| competition | Good fit but someone better applied | Signal: late-stage rejection after multiple rounds |
| fit mismatch | The role wasn't what it seemed | Signal: early rejection + "not a good fit" language |
| visa | Work authorisation issue | Explicit rejection language |
| ghost | Role was never real | No response ever, advert disappears |
| timing | Role filled before application processed | Fast rejection, role still posted |

Track these in `outcomes.jsonl` with a `failure_mode` field.  Over time, the
company simulator learns which orgs/roles tend toward which failure modes.

---

## Phase 5 — Simulate the simulation's accuracy

### 5a. Backtesting

For every past assessment where we now know the outcome, ask:

> If the simulator existed on that date, given only the information available
> then, what would it have recommended?  Is that better or worse than what the
> matcher actually said?

```bash
../venv/bin/python3 backtest.py --profile lj --since 30d
```

This is the ARC-AGI-3 Schema equivalent of "replay the program against the
training grid."  The program here is the simulator's policy, and the grid is
the historical decision sequence with known outcomes.

### 5b. Regression suite from historical corrections

Every time a human overrides or corrects a recommendation, that becomes a
regression case:

```json
{
  "case_id": "reg-016",
  "advert_hash": "...",
  "original_decision": "GO",
  "human_correction": "DROP",
  "reason": "This is a prestige trap — amazing name, boring actual work.",
  "date": "2026-07-20"
}
```

These are the LoveWork equivalent of ARC-AGI-3 holdout puzzles — specific
judgment failures that the simulator must not repeat.

### 5c. Improvement metric

Tracking improvement is not about raising scores.  It's about:

```text
1. Does the simulator catch known bad recommendations before the human does?
2. Does it rank opportunities so that the top 5% contain more realised good
   outcomes than the bottom 95%?
3. Does its uncertainty estimate correlate with actual prediction error?
4. Does the recommended experiment actually reduce uncertainty when executed?
```

Define these four metrics.  Measure them after every calibration pass.  A
simulator that improves on these metrics is genuinely learning, not just
becoming a more articulate guesser.

---

## Phase 6 — Democratise (multi-principal)

Once the simulator works for one principal (LJ), generalise:

1. **Parameterise by profile**: the person simulator reads from
   `profiles/<name>/` without code changes.
2. **Principal-specific calibration**: each principal gets their own
   calibration table and regression suite.
3. **Cross-principal patterns**: if VJ's rejection patterns resemble LJ's at
   the same career stage, the simulation can transfer.

The VJ dataset is already growing (7 runs, 1119 assessments).  It has zero
outcomes so far — which means the simulator's priors for VJ are entirely
inherited from profile and domain knowledge, not yet calibrated.  That's
honest and useful: it sets a baseline to measure calibration improvement
against.

---

## How this connects to the existing docs

| Doc | What it provides for the simulator |
|-----|-----------------------------------|
| Ch 09 — Intelligence Layer | The *why*: simulation is the value proposition. Three linked simulations. Decision ledger. |
| Ch 11 — Agentic Intelligence Harness | The *what*: Tau-backed agent, reflection loop, Continual Harness stores. Dataset schema. |
| Ch 13 — Probabilistic Simulator Model | The *math*: factorisation of P(accept), company evidence classes, decision under uncertainty. |
| Ch 03 — Profiles | The person model foundation: 3-layer profile, soul, work_auth, possibilities. |
| Ch 05 — Matcher | The current scoring surface — what the simulator starts from and will supersede. |
| Ch 14 — Operational Meta-Loop | The schedule-and-watchdog harness that keeps the system running between simulation builds. |

---

## Immediate next steps (what to do now)

1. **Run `rg assessment.*outcome` across the dataset** to see how many
   assessments already have matched outcomes.  If fewer than 10% of assessed
   roles have outcome data, the first priority is improving outcome capture
   (see Phase 1a).

2. **Write the `match_assessment_to_outcome` script.**  This is the single
   highest-leverage piece of work: it turns the ledger from a diary into
   training data.  Target: one afternoon.

3. **Write the Pydantic `AssessmentRow` schema.**  Add it to `snapshot.py`.
   Run a validation pass.  Target: one morning.

4. **Write `backfill_adverts.py`.**  Pull primary content from `cache/page_*.md`
   and registry to create the advert corpus.  Target: one afternoon.

5. **Write `calibrate.py` (v0).**  Simple script: read assessments + outcomes,
   bin by score, compute outcome rates.  Target: one morning.

6. **Write `rehearse.py` (v0).**  Wrap the existing matcher with uncertainty
   labels and a recommended-experiment field.  Do not build a new scoring
   engine — use the existing one but make its assumptions explicit.
   Target: one day.

Each step produces a working CLI from day one.  No step requires the previous
step to be perfect.  The simulator grows by accretion, not by a Big Build.
