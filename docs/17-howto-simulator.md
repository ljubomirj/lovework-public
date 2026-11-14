# Chapter 17 — HOWTO: Simulator from the Historical Dataset

> **Audience:** builders and agents who want to turn LoveWork's crawl history
> into a working simulation of work-life decisions.
> **Status:** decision-simulator roadmap; interfaces below are proposed, not
> executable instructions. The existing `calibration.py` aligns model score
> scales; it does not calibrate career outcomes. The existing `rehearse.py`
> runs ATA mock interviews, not this chapter's decision rehearsal.
> **Implementation order:** [chapter 34](34-agent-system-plan.md), especially
> AS0 (evidence), AS4 (validated learning) and AS5 (evaluation), under the
> operating doctrine in [chapter 27](27-agent-ergonomics.md). See also
> [`09-intelligence-layer.md`](09-intelligence-layer.md) (thesis),
> [`11-agentic-intelligence-harness.md`](11-agentic-intelligence-harness.md)
> (harness design), [`13-probabilistic-simulator-model.md`](13-probabilistic-simulator-model.md)
> (formal frame), [`../DECISIONS.md`](../DECISIONS.md) D17 (profile model).

## Why a HOWTO exists separately from the design docs

Chapters 09, 11, and 13 already lay out *what* LoveWork should build and *why*.
This chapter describes the destination and its evidence gates. Read
[chapter 19](19-howto-dataset-second-opinion.md) before implementation:
measure usable joins, label meaning and selection bias before building a
predictor. The phases below are capability dependencies, not an instruction
to build every sketched class or command.

The core insight shared with the ARC-AGI-3 Schema harness:

```text
editable world model (person + company + process)
    -> compare bounded alternatives (inference and attention have costs)
    -> execute cheapest authorized action that can improve a decision
    -> observe outcome
    -> update model
```

LoveWork can borrow the epistemic pattern without copying the benchmark
machinery.  The "world" here is not a grid of pixels but a person's career
ecosystem.

---

## Phase 0 — Take stock: what exists already

Before building anything, know what you have.

### The decision ledger (historical inventory)

The counts below are the original July 2026 inventory, not current status.
Use the principal's generated cockpit and linked ledger receipts for current
counts. A file's existence does not establish usable labels or calibration.

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
- **Outcome calibration**: no outcome-calibrated predictor is established by
  this design. `calibration.py` implements model scale alignment; preserving
  score comparability is a different claim from predicting observed outcomes.
- **Reflection loop**: no periodic review of decisions vs outcomes.
- **Regression harness**: no test suite of past judgment cases.

---

## Phase 1 — Make the existing data simulator-ready

### 1a. Backfill outcome links to assessments

Outcomes may be *org-level*: prior contact does not always identify the
advert or assessment that caused it. First measure the existing joins under
chapter 19. Then represent qualified associations as append-only link events:

```python
# Retain assessment_id, outcome_id, principal, identity scope, evidence refs,
# link method, recorded_at, and any superseded link id in a separate event.
# Exact role identity still needs temporal validation; fuzzy/org matching
# creates a candidate association, never an automatic role-level label.
# Rebuild joined views from events; never rewrite an assessment row.
```

These links establish what can be compared, not whether a comparison is
statistically or semantically valid. Later outcomes remain evaluation targets,
outside the evidence available to the historical decision. Retain ambiguous,
unmatched and conflicting links rather than forcing coverage upward.

**Checkpoint:** a CLI that shows, for any assessment, whether we know what
happened next.

### 1b. Structured assessment schema and reaction capture

After measurement identifies actual gaps, validate future assessment writes
without rewriting legacy evidence. This illustrative shape separates immutable
assessment inputs/output from later links:

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
    # Proposed references to exact inputs; outcomes live in separate events.
    input_refs: list[str]
    knowledge_cutoff: datetime
```

Validate old rows into a report; preserve their original bytes and qualify
missing fields. Any repaired interpretation is a linked revision. Build the
principal-scoped reactions capture from chapter 18 alongside this work: outcome
links alone cannot supply taste, fit or flourishing labels.

### 1c. Advert corpus as a first-class dataset

Each assessment references an advert via `advert_hash`.  But the advert text
itself — the primary content the LLM scored against — is not in the dataset.
Use captured source versions and consumed-input references in strata (chapter
26), not a competing mutable corpus keyed only by an identity hash. A proposed
evidence view may expose:

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

> **D20 evidence contract:** the frozen per-stratum corpus
> ([chapter 26](26-asof-strata-history.md)) supplies retained advert versions.
> Verify capture coverage for each case; seal existence is insufficient.
> It requires captured input references, not merely a
> run-end cache copy. Retained reports and raw-email archives can also support
> explicitly qualified reconstruction. Refetch-backfill is unsound:
> re-fetching later returns the present, not the page the matcher scored
> (a disappeared advert returns a 404, a changed one returns text we never
> saw). Pre-strata reconstruction uses surviving caches, CSV archives,
> reports, raw emails and recorded Git versions with explicit provenance
> grades; file dates alone do not establish historical acquisition.

### 1d. Profile snapshots per run

Use the actual assembled profile and effective policy inputs pinned for the
operation, with their digests and references. Do not introduce a second
profile archive alongside strata:

```text
state/<principal>/strata/<run_id>/inputs/
  soul.md
  cv-short.md
  bio-long.md
  possibilities.md
  work_auth.md
```

This is critical: the profile that generated the scores is versioned alongside
the scores. Without it, you can't tell whether a score improvement came from a
better matcher or a better profile.

> **D20 input contract:** verify the case's actual coverage rather than
> inferring it from directory names. Pin effective inputs at run start,
> execute against those bytes, and seal them under `strata/<run_id>/inputs/`.
> Copying the profile only at run end can capture a different version.
> [Chapter 26 §3](26-asof-strata-history.md#3-l0-hard-linked-strata-with-a-consistency-protocol)
> defines the full input contract, including policy, clock and history.

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
        """Coverage of named sources and sampled opportunity families;
        unknown total-market recall stays unknown."""
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

    competing_hypotheses: list[dict]
    # Claim, evidence refs, counterevidence, qualitative confidence,
    # and observation that could change the recommended action.
    # A repeated advert does not prove genuine current hiring.

    recommended_experiment: str
    # Cheapest authorized action expected to change a consequential decision
    # Options: read_primary_page, try_product, ask_manager,
    #          seek_introduction, apply, defer, ignore

    expected_value: float
    confidence: str  # low | medium | high
    budget: dict  # calls, tokens, elapsed time, money, human attention
    stop_reason: str | None
    wake_condition: str | None
```

### 3b. Proposed decision-rehearsal interface

The examples below are design sketches, not installed commands. The existing
`rehearse.py` belongs to ATA interviews; reserve a distinct decision interface
through the common facade before implementing this one.

```bash
# Rehearse a specific opportunity
../venv/bin/python3 decision_rehearsal.py \
    --profile lj --role general \
    --url "https://..." \
    [--advert-hash "..."]

# Rehearse all new GO/MAYBE in the latest report
../venv/bin/python3 decision_rehearsal.py \
    --profile lj --role general \
    --from-latest-report

# Rehearse every opportunity in the last assessment set (batch mode)
../venv/bin/python3 decision_rehearsal.py \
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

**Screening model:** Engineering screening criteria need checking against
  the principal's cited evidence; no career deficit is inferred from a title.
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
    proposed_hypothesis="Check whether this requirement limits similar routes",
    evidence_refs=["rejection-message-id", "principal-reflection-id"]
)
```

This records evidence and a hypothesis, not an automatic score decrement.
One observed rejection cannot identify its cause or generalize to other roles
without supporting evidence. Learning follows chapter 18's promotion gate.

---

## Phase 4 — Calibrate from outcomes

### 4a. Define the target and denominator before the evaluator

A score is a judgment, not automatically a probability. Do not divide offers
by every scored lead and call the result score calibration: most leads were
never pursued, exposure is selected by the current policy, and many outcomes
are still unobserved. A proposed evaluator must report separate targets:

| Target | Eligible evidence and denominator | Explicit exclusions / limits |
|---|---|---|
| Recommendation usefulness / taste | Principal reactions to identified, actually reviewed assessments | Silence and applications are not taste labels; report exposure and response coverage |
| Interview reach | Valid role-level applications with known follow-up window, grouped by route and principal | Pending/no-response cases remain censored; org-only links do not become role labels |
| Offer conversion | Qualified interviews with observed process outcomes and follow-up | Generic rejections do not identify causal failure modes |
| Flourishing / regret | Principal reflections after the relevant lived experience and time horizon | Interview or offer success does not establish flourishing |
| Operational quality | Attempted operations / checked artifacts with their success predicates | A healthy run does not prove valuable recommendations |

Pin inputs, target definition, clock, policy, model, identity joins and label
provenance. Report sample size, missingness, coverage, uncertainty and known
selection bias. With insufficient evidence, return `not estimable` with the
capture step that would help; do not invent a numeric prior from a minimum
row-count threshold. Observational comparisons support descriptive claims,
not causal claims that a recommendation improved a life.

### 4b. Keep scales, predictions and decisions distinct

The existing `calibration.py` aligns one model's score scale with a reference
model. It does not validate the reference model against lived outcomes.
Preserve raw scores, scale transformation/version, qualified outcome estimates,
and the resulting action as separate fields. A reach estimate never replaces
fit or flourish; a change of provider is itself an evaluation condition.

### 4c. Failure attribution is a hypothesis unless established

| Observation | Permitted interpretation |
|---|---|
| Employer explicitly names a missing credential or work authorization | Attributed employer explanation, with the source and scope |
| Rejection after multiple rounds | Observed process stage; competition remains a possible explanation |
| Generic “not a good fit” | Rejection with unknown reason, not proof of principal-role mismatch |
| No response by a named date | Follow-up incomplete / censored, not proof of a ghost role |
| Advert disappears | Listing no longer observed under stated coverage; not proof it was filled |

Store the raw receipt separately from inferred causes, preserve competing
explanations, and only update the dimension the evidence actually addresses.

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

These are regression cases once a scoped expected behavior is validated.
A case used to design a patch is not an untouched holdout; keep development,
regression and time/opportunity-family-separated evaluation partitions distinct.

### 5c. Improvement metric

Tracking improvement is not about raising scores.  It's about:

```text
1. Does the simulator catch known bad recommendations before the human does?
2. Does it rank reviewed opportunities usefully, while measuring exposure,
   missing labels and a small authorized sample of overlooked branches?
3. Does its uncertainty estimate correlate with actual prediction error?
4. Does the next action improve the decision enough to justify its total cost?
```

Define these metrics before a candidate change and compare with a frozen
baseline. Preserve hard constraints, positive anchors and branch diversity;
report both improvements and regressions. Repeatedly tuning on the same cases
cannot establish generalization. Stop when the budget is spent, no new
independent evidence exists, or no plausible result would change the decision.
A bounded negative result plus its retry condition is a useful retained result.

---

## Phase 6 — Democratise (multi-principal)

Once the simulator works for one principal (LJ), generalise:

1. **Parameterise by profile**: the person simulator reads from
   `profiles/<name>/` without code changes.
2. **Principal-specific calibration**: each principal gets their own
   calibration table and regression suite.
3. **Share procedures, not personal conclusions**: principal evidence,
   preferences, calibrations and hypotheses stay scoped to that person.
   A transferable procedure may be evaluated with authorized, non-identifying
   fixtures; resemblance alone never authorizes reuse of another person's data.

The original July inventory showed VJ with 7 runs, 1119 assessments and no
outcomes. Re-measure present coverage per principal; do not inherit LJ's
calibration or treat those historical counts as current.  That's
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

## Implementation entry gate

Use [chapter 34](34-agent-system-plan.md) as the cross-system plan of record;
this chapter supplies simulator-specific contracts, not a competing backlog.

1. Identify a real decision the principal needs and measure the cheapest
   available evidence path. Establish a bounded baseline before adding a model.
2. Audit joins, consumed-input coverage, knowledge cutoffs and labels using
   chapter 19's qualifications. Record unanswerable questions explicitly.
3. Capture attributable reactions and outcome links append-only, with the
   principal boundary enforced by the shared domain operation, not the UI.
4. Run a small decision-rehearsal experiment only when the evidence can
   distinguish it from the baseline. Prefer existing matcher functions over
   new simulator classes; record calls, elapsed time, money and review burden.
5. Promote only a tested, scoped proposal under chapter 18's gate. Preserve
   negative trials, baseline and holdout versions, stopping reasons and the
   conditions that would justify another attempt.

Acceptance is a better, auditable decision at an acceptable total cost, or an
honest finding that more evidence is required. Generating a class hierarchy,
a calibration table or a longer explanation is not acceptance.
