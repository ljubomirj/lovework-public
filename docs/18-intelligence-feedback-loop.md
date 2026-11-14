# Chapter 18 — The Intelligence Feedback Loop

> **Audience:** builders and agents designing the missing middle of the
> intelligence layer — the path from raw evidence to learned, auditable
> changes in the person model and decision policy.
> **Status:** design, partially implemented. The dataset spine
> (`runs` / `assessments` / `outcomes` JSONL) is live; `reactions` and
> `propositions` streams defined here are not yet built.
> **Implementation order:** [chapter 34](34-agent-system-plan.md) AS0, AS4
> and AS5; [chapter 27](27-agent-ergonomics.md) owns the system-wide contracts.
> Schemas and endpoints below are proposed interfaces, not deployed tools.
> **See also:** [`09-intelligence-layer.md`](09-intelligence-layer.md)
> (thesis), [`11-agentic-intelligence-harness.md`](11-agentic-intelligence-harness.md)
> (harness design), [`13-probabilistic-simulator-model.md`](13-probabilistic-simulator-model.md)
> (formal frame), [`17-howto-simulator.md`](17-howto-simulator.md)
> (simulator HOWTO).

## Why this chapter exists separately from 09, 11, 13

The earlier chapters lay out *what* LoveWork should own (the intelligence
layer), *what kind of harness* runs it (Tau-backed or otherwise), and the
*mathematical frame* (probabilistic factorisation of P(accept)). This chapter
fills a gap all three share: **the data that the loop reads is underspecified.**

The ledger today has *what the system believed* (`assessments.jsonl`) and *what
happened in the world* (`outcomes.jsonl`) — but nothing about *what the
principal felt or wanted.* A rejection is evidence about reach, not about
whether a recommendation was sound. An application is not necessarily
enthusiasm. Without a third stream, the ledger is a passive archive and any
"learning loop" built on it will conflate taste with circumstance.

This chapter defines the missing streams, the surfaces that capture them, the
stores that hold learned claims, and the report that makes the loop
auditable. It is the design half of the equation; chapter 19 is the
implementation-order critique.

## The core principle: separate stores, never collapse

| Store | Author | Mutability | Purpose |
|-------|--------|-----------|---------|
| `soul.md` | principal, by hand | by hand only | identity, taste, voice — the authored persona |
| `reactions.jsonl` | principal (via dashboard) | append-only | raw evidence, never summarised on capture |
| `propositions.jsonl` | proposed reflection operation | append-only proposal and transition events | learned claims, provenance-tagged, scoped, auditable |

Mixing any two is the failure mode. If `reflect.py` writes `soul.md` directly,
one offhand remark becomes permanent identity and you lose auditability. If
reactions are summarised on capture, you lose the raw signal the reflection
loop needs. If propositions lack scope, contradictions become silent
overwrites.

**The automated reflection loop never writes `soul.md`.** Proposed reactions
and proposition events live under `state/<principal>/dataset/`; authored
profiles remain under `profiles/<principal>/`. The planned matcher view reads
eligible accepted propositions alongside authored context. Approval and
activation are separate events; source rows are never edited in place. The
loop may propose an authored-profile patch for the principal to adopt under
the current session's authority; a proposal grants itself no permission.

## Stream 1 — `reactions.jsonl` (the missing signal)

A **reaction** is the principal's felt or expressed response to a *specific
recommendation*, captured close to the moment it happens. The defining rule:
**a scored reaction needs an unambiguous assessment reference**. An advert-only
or conversational reference may be captured with its raw text, candidate links
and `attribution: unresolved`; never silently assign it to the latest assessment.
Resolution appends a link event. Unattributed statements remain evidence but
are ineligible for assessment-level evaluation until resolved.

A reaction is the answer to *"what did you think of this lead?"* — distinct
from an outcome (which answers *"what happened externally?"*).

### Row schema

```json
{
  "event_type": "reaction",
  "reaction_id": "uuid",
  "assessment_id": "4120b896470d42cf82c01869387b405b",
  "advert_hash": "e74984c72eb0b31a",
  "run_id": "90296e7bdf48459a9c3f24fe567ba4f2",
  "org_name": "Poetiq",
  "title": "AI Engineer",
  "profile_name": "lj",
  "role": "general",

  "reaction": "excited",
  "action_intent": "apply",
  "primary_reason": "work",
  "secondary_reasons": ["mission"],
  "expression_kind": "explicit",
  "energy": "high",

  "verbatim": "this is exactly the agentic-platform-building thing I want",
  "recorded_at": "2026-07-12T19:05:00",
  "source": "dashboard",
  "recorded_by": "lj"
}
```

### Closed vocabularies

**`reaction`** — six values, deliberately coarse to force a choice:
- `excited` — yes, more of this
- `interested` — positive but not electric
- `indifferent` — neutral; wouldn't pursue but no aversion
- `bored` — mildly negative; the work sounds dull
- `repelled` — strong negative; active aversion
- `unsure` — can't tell; needs more info (valuable signal, not a non-signal)

**`action_intent`** — what the principal intends to do, mapped onto the
existing matcher action vocabulary (do not fork):
- `apply` ↔ `APPLY_NOW`
- `research` ↔ `WATCH` / `MONITOR`
- `discuss` ↔ `WARM_INTRO_ONLY`
- `defer` ↔ "not now, maybe later" (no current matcher equivalent — known gap)
- `ignore` ↔ `DROP`-by-choice (distinct from system DROP)
- `contact` — reach out directly outside formal apply

**`primary_reason` / `secondary_reasons`** — drawn from a fixed list so
patterns are aggregatable:
`work` · `mission` · `people` · `trajectory` · `money` · `location` ·
`reach` · `timing` · `culture` · `ideology`

Up to two reasons. The `ideology` value is LoveWork-specific: the LJ
profile explicitly downweights Anthropic / EA-aligned companies. A `repelled`
+ `ideology` reaction is a different learning signal than `repelled` + `money`.

**`expression_kind`** (capture provenance, not statistical confidence):
- `explicit` — the principal typed it or clicked a deliberate button
- `inferred` — derived from behaviour, retained as a separate hypothesis with
  its derivation; never counted as principal-expressed taste.

**`energy`** (optional) — `high` / `medium` / `low`. Captures something the
outcome stream cannot: a person may apply out of recruiter pressure or low
energy rather than enthusiasm. Energy at reaction time predicts later regret.

**`verbatim`** — the human text, preserved raw. Never collapsed into the
persona on capture. This is the raw material the reflection loop reads.

### Explicit negative space — what is NOT a reaction

Without this discipline the stream fills with noise:

- **A rejection is not a reaction.** `outcome{kind:rejection}` is evidence
  about *reach*, not about whether the recommendation was sound. Do not let
  rejections flow into reactions.
- **Silence is not `indifferent`.** A principal not engaging with a category
  of leads is ambiguous (busy? uninterested?) and must not be auto-encoded
  as a reaction. Repeated silence remains ambiguous; it may motivate a bounded
  question about attention or relevance, but cannot manufacture taste labels.
- **An application is not necessarily `excited`.** An apply may be recruiter
  pressure, urgency, or low energy. `applied` outcome → at most an `inferred`
  reaction of `action_intent:apply`; never auto-upgrade to `excited`.
- **Environment/system failures are not reactions.** A broken link, a missed
  crawl, an ATS error do not reflect taste.
- **A one-off is not a proposition.** A single `bored` reaction does not
  update the persona. It updates the *evidence ledger*. Propositions require
  corroboration.

### The join that makes it valuable

```
assessment (what system believed)  ─┐
reaction   (what principal felt)   ─┼─► disagreement_score, learning signal
outcome    (what happened)         ─┘
```

The first query finds disagreement: recommendations to pursue whose
attributable reactions express boredom, indifference or ignoring. This is a
review candidate, not automatically an error: timing, energy, new information
or a different goal may explain it. Establish the reason and decision-time
scope before constructing a regression label.

## Surface — dashboard capture UX

The reactions stream lives or dies on the capture path. Rules:

1. **Capture at the place of review.** Dashboard buttons are one proposed
   adapter; CLI/MCP/chat may call the same domain operation. Six compact
   buttons can capture the essential signal in one tap. Bind principal and
   assessment server-side; anonymous trusted-LAN reads do not establish the
   identity or authority of a feedback writer.
2. **`reaction` is the only required field.** Action-intent, reason, energy,
   verbatim are optional. Friction kills the stream.
3. **The page auto-refreshes every 30s**, so capture must be a fire-and-forget
   POST that survives a page reload — not a long-lived form.
4. **Optional second stage.** Clicking an emoji opens a small inline expand
   (not a modal) with optional action_intent / primary_reason dropdowns and a
   one-line free-text box. Submit or skip. One tap for the essential signal;
   three more taps for the detail.
5. **Optional Hermes hook, not the primary path.** A thin listener that
   detects when the principal discusses a known org in chat and offers
   "record a reaction to [Poetiq AI Engineer]?". The hook calls the same
   `/react` endpoint with `source: "hermes"`; it does not own
   `reactions.jsonl`.

The proposed endpoint is `POST /react` with a principal-bound request context,
`{assessment_id, reaction, idempotency_key}` and optional attribution fields.
The shared operation verifies scope and identity, fills references, records
through the writer boundary, and returns an event receipt. A retry returns
the existing receipt; a changed reaction appends an event with a supersedes
link. Cross-principal references and ambiguous joins fail visibly. Capture
must not depend on a model call.

A read-only "▤ Reactions log" navbar link renders the last 50 rows for
traceability — but capture happens at the moment of review on the Top Jobs
table, not in a separate ritual.

## Stream 2 — `propositions.jsonl` (the learned layer)

A **proposition** is a learned claim about the principal, with provenance,
confidence, scope, and contradiction state. The output of the reflection
loop and the input to persona updates.

### Row schema

```json
{
  "event_type": "proposition",
  "proposition_id": "pr_0007",
  "claim": "LJ tends to lose interest in roles requiring a strong publication record when the day-to-day is paper-first rather than system-building.",
  "category": "taste",

  "scope": {
    "domain": "ML research roles",
    "role_types": ["research_scientist", "applied_scientist"],
    "condition": "where publication_record is a hard requirement AND day-to-day is paper-first"
  },

  "evidence": [
    {"type": "reaction", "ref": "rx_0034", "assessment_id": "...", "reaction": "bored", "verbatim": "another publish-or-perish lab"},
    {"type": "reaction", "ref": "rx_0041", "assessment_id": "...", "reaction": "bored", "verbatim": "I want to build the system, not mainly publish"}
  ],
  "support": 2,
  "support_detail": "2 distinct expressions about different opportunities; verify common cause and scope",

  "contradictions": [
    {"ref": "rx_0012", "note": "excited about Ineffable MTS despite publication req", "scope": "when role is engineer-titled at frontier lab"}
  ],

  "confidence": "medium",
  "status": "proposed",
  "proposed_at": "2026-07-20T22:00:00",
  "proposed_by": "reflect.py run 2026-07-20",
  "decided_at": null,
  "decided_by": null,
  "decision_note": "",

  "proposed_policy_effect": {
    "axis": "flourish",
    "direction": "down",
    "magnitude": "bounded candidate effect where primary evidence establishes paper-first work; evaluate before choosing a numerical change",
    "regression_impact": {"C1_ineffable": "unchanged", "C5_poetiq_scientist": "unchanged", "E1_poetiq_engineer": "unchanged"}
  }
}
```

### Field definitions

**`claim`** — one declarative sentence. If it can't be said in one sentence,
it's two propositions. Not "LJ dislikes labs" (too broad) and not "LJ
disliked Ineffable on 2026-07-12" (too specific). The middle grain: *"LJ
loses interest in paper-first research roles with hard publication
requirements."*

**`category`** — fixed list, so the matcher knows how to apply each:
- `taste` — what energises / bores (affects `flourish_score`)
- `reach` — screening-realism beliefs the principal holds about themselves
  (affects `reach_score`)
- `fit` — skill/domain alignment (affects `fit_score`)
- `action` — what the principal does vs what the system recommends
- `constraint` — hard limits (location, ideology, reapply) — usually mirrors
  `work_auth.md`
- `trajectory` — branching-future beliefs (mirrors `possibilities.md`)

**`scope`** — *where the claim applies.* The most important field. "LJ
doesn't want finance" and "this AI/quant bridge is interesting" coexist
because they have different scope. Without scope, the reflection loop
collapses contradictions into overwrites.

**`evidence`** — cite original events, attribution, effective time and common
origin. Prior model assessments, repeated crawls, derived summaries and several
copies of one reaction are not independent corroboration. They can identify a
question, but cannot confirm the model's own belief. Outcomes support only the
dimension they observe; a gold case reusing a reaction is not a second witness.

A single explicit principal statement can establish an authored preference or
constraint within its stated scope. Inferred population-pattern claims need
independent support and uncertainty assessment; “two items” is not a confidence
guarantee. Honor a clear current instruction without waiting for repetition.

**`support` / `contradictions`** — explicit counts on both sides. A
proposition with 3 support and 2 contradictions is *contested*, and the
reflection loop must surface the contradictions in the claim's scope handling,
not ignore them. This is the mechanism that prevents one offhand remark
becoming permanent persona.

**`confidence`** — `low` / `medium` / `high`, with a reason based on evidence
quality, independence, recency and applicability. Do not tailor the scope after
seeing evaluation labels to explain away contradictions. A changed scope is a
new candidate requiring evaluation on independent cases.

**`status`** lifecycle:
- `hypothesis` — inferred claim with inadequate support or applicability
- `proposed` — reflected out, awaiting the principal's review
- `accepted` — principal adopted a scoped claim; activation additionally needs
  compatibility, versioned evaluation and current applicability
- `rejected` — principal rejected; kept for the record (prevents re-proposing)
- `superseded` — replaced by a better-scoped proposition (`superseded_by` ref)

**`proposed_policy_effect`** — *how this proposition would change scoring*, in
machine-readable terms. A taste claim changes a taste/flourish hypothesis,
not screening reach. Test the smallest candidate effect against a baseline;
do not assign arbitrary score increments as though measured. Review presents
both the source evidence and measured or unknown impact.

### The state machine

```
                  (attributed evidence)
   reactions ──► hypothesis
                        │
                        │ (adequate support + scope)
                        ▼
                   proposed ─────────► rejected (kept, won't re-propose)
                        │
                        │ (principal approves)
                        ▼
                   accepted ──► validated, applicable version activated
                        │
                        │ (better-scoped proposition arrives)
                        ▼
                   superseded
```

**Rejected propositions persist**, with their scope and reason. Do not
re-propose an unchanged claim next week. Record the material new evidence or
principal change that would justify reopening it; retention is not a permanent
ban on learning that circumstances changed. Transitions append events, never
overwrite a prior status or rejection.

### Activation, retirement and bounded memory

Acceptance is a reviewed belief; activation is a versioned policy decision.
Before activation, pin its evidence, affected dimension, applicability,
expected decision change, dependencies, baseline, regression results,
evaluation limits and authority. Hard constraints cannot be weakened by a
learned taste claim. A material contradiction marks the claim contested and
makes its use visible; stale applicability excludes it from new decisions
until reviewed. Retirement/supersession preserves the original evidence.

Retrieve only applicable propositions within a context budget, with omission
and conflict counts plus evidence links. Do not append every accepted belief
to every prompt. Retain rejected experiments and reusable procedures with
scope, version, measured benefit and retry conditions; preserving history
must not require loading all history. No new independent evidence means no
model reflection call. Stop at the call/time/cost/review budget and record
what could justify waking the loop again.

### The join back to scoring

The matcher's prompt currently loads `soul.md` + CV + role criteria. The
change is small: it also loads accepted propositions, rendered as a compact
block:

```
LEARNED ABOUT THIS CANDIDATE (from observed evidence, confidence-weighted):
- [medium] LJ loses interest in paper-first research roles with hard publication
  requirements. Applies to: research_scientist, applied_scientist roles where
  publication_record is required AND day-to-day is paper-first.
  (evidence: 2 distinct reactions; 1 contradiction retained for review)
```

The matcher prompt already handles the branching-possibility +1 signal;
propositions are the same mechanism, learned instead of authored.

## Gold cases — the regression target

A gold case is a **regression fixture**: a frozen advert + profile-context
snapshot, with the decision / action / reasoning the principal has *manually
validated as correct*. Not what the system did — what the system *should* do.
Cases need a label source appropriate to the dimension: principal-validated
taste, evidenced constraint, or qualified process outcome. A rejection alone
does not prove a bad recommendation, and applying alone does not prove a good one.

Every proposed persona/policy change must preserve mandatory constraints and
report changes on positive and negative anchors. A principal's changed goal
may legitimately supersede an old label; retain both versions and their scope.
Development/regression fixtures are not untouched evaluation holdouts.

Cases live under `lovework-agent/tests/gold_cases/` as YAML fixtures. The
shape (each case grounded in real rows from the live ledger):

```yaml
case_id: B1
org_name: PlantingSpace
title: Quant Modeling
expected_decision: DROP
expected_reason: "org cooldown — applied 2026-06-14, rejection recorded"
reasoning_dimensions:
  - org_cooldown
  - reapply_detection
evidence:
  - outcomes.jsonl: applied 2026-06-14
  - outcome-event-id: explicit rejection receipt for this same organisation
label_authority: principal-validated cooldown rule at the pinned effective clock
status: illustrative_fixture_requires_verified_evidence
notes: "the matcher's current failure — keeps surfacing as GO/APPLY_NOW"
```

> **Strata pinning (D20, 2026-09-13):** once strata exist
> ([chapter 26](26-asof-strata-history.md)), gold cases gain a `stratum:`
> field plus evidence IDs, profile/policy digests, effective clock and a
> knowledge cutoff. The expected behavior is a separately sourced label.
> Original recorded output is retrievable; a fresh LLM score is not guaranteed
> identical. Exact playback needs the captured requests/responses and tested
> deterministic transformations. Later reactions/outcomes are evaluation
> targets, not historical inputs. See chapter 26 §6 and chapter 27 §6 for
> replay and evidence-backed proposition promotion.

### Case categories (working target, ~24 cases)

**Group A — hard kills (must never regress):** US-only work-auth kills,
role-domain mismatch kills (IT support ≠ ML/AI), ideological counter-align
(Anthropic / EA), paused-track kills (pure quant trader).

**Group B — org-cooldown / reapply (lifecycle memory):** PlantingSpace
(applied→rejected, still surfaced as GO — the canonical bug), Poetiq,
Prolific (AI Engineer rejected + Field Researcher — same org, different
role, tests the role-vs-org cooldown distinction), Poolside, OpenAI
Residency, Prior Labs, a past employer (paused-track + cooldown tension).

**Group C — reach vs fit tension (the prestige-trap axis):** Ineffable /
David Silver (fit 7–8, reach 4–5, `WARM_INTRO_ONLY`), AMI Labs / LeCun
(scientist vs engineer at the same org — tests role-type sensitivity),
Poetiq / AI Scientist (publication-gap reach cap), Isomorphic Labs
(high-prestige, low-reach, platform-not-research).

**Group D — branch alignment (the +1 signal):** PlantingSpace first-run
MAYBE with no branch named vs second-run with branches (a) / (g) — exposes
real non-determinism; Poetiq AI Engineer (correct +1, branch (a)).

**Group E — candidate positive anchors:** pursued roles such as Poetiq AI
Engineer, PlantingSpace before application, Matcha and Runway are candidates
for principal review, not automatically gold. Record what was worthwhile and
at what time before labeling fit, action or flourishing. A suite containing
only negative cases can drift toward recommending nothing; qualified positive
anchors and checks on overlooked branches counter that failure.

### The forcing-function value

Drafting the gold cases does something the architecture docs cannot: **it
forces the principal to articulate what "good judgment" actually means, in
concrete cases, before any learning loop runs.** The reflection loop without
gold cases is unfalsifiable ("did it get better?"). With them, every change
has a pass/fail number.

Build gold cases *in parallel* with reactions capture, not after — they are
the other half of the same gate.

## The reflection report (`wiki/reflections/`)

The surface that decides whether the loop lives or dies. If the principal
does not read it, propositions pile up unreviewed, the matcher never learns,
and `reactions.jsonl` becomes a passive archive — the failure mode this whole
chapter exists to escape.

Four rules:

1. **Lead with the decision, not the analysis.** The first block answers
   "do I need to do anything?" — *N propositions proposed, 0 auto-activated,
   K gold cases passing.* The principal can stop reading after the summary
   if busy.
2. **Every claim shows its receipts inline.** No "based on patterns in your
   decisions." Every proposition cites the specific reactions / assessments
   / outcomes that support it. If the evidence isn't worth showing, the
   proposition isn't worth proposing.
3. **Silence is the default good outcome.** A deterministic gate may record
   “no new independent evidence; review skipped” without invoking a model.
   A completed pass with no useful proposal retains a short receipt and cost.
   Report tests as passing only when actually run or explicitly cite a still
   applicable prior result. Never manufacture content to justify reflection.
4. **Honest about contradiction.** When evidence points both ways, say so —
   don't smooth it into a confident claim.

Lives at `wiki/reflections/YYYY-MM-DD-HHMM-reflection.md` (sibling to
`reports/`, same naming convention). A navbar link **🪞 Reflections** beside
📋 Reports.

### Report shape (worked example, abbreviated)

```markdown
# REFLECTION — 2026-07-20 22:00 BST

Window: 2026-07-13 → 2026-07-20 (7 days)
Inputs: 31 reactions, 412 assessments, 47 outcomes, 24 gold cases

## Summary
- 3 propositions proposed (awaiting review)
- 0 auto-activated
- 1 gold case failing under current policy ⚠ (see below)
- 23/24 gold cases passing under current policy
- Candidate and baseline evaluated on the same pinned cases; holdout not yet available

> Nothing here is applied yet. Accepted propositions require a separate
> validated activation event. soul.md and role files are untouched.

## ⚠ Gold case regression (1)
### B1 — PlantingSpace / Quant Modeling (post-rejection)
Expected: DROP (org cooldown, rejected 2026-06-15)
Got:     GO 7.2 / APPLY_NOW
Root cause: org-cooldown check isn't reading the rejection row from
outcomes.jsonl — only the apply row. Code fix belongs in matcher.py.

## Proposed propositions (3)
### P-0007 — paper-first research roles lose interest
[full claim, scope, evidence, contradictions, would-change, regression impact]

## Patterns observed (not yet proposable)
- Quant-adjacent roles get muted reactions even when scored GO. Watch.
- No reactions captured for Harnham contract roles. Can't tell if the
  contract-AI track is energising or just urgent.

## Accepted propositions (stable, 4)
[table for visibility; no action needed]

## Reactions processed (31)
excited: 9, interested: 8, indifferent: 6, bored: 4, repelled: 1, unsure: 3
```

### Why each section is shaped that way

- **Summary at top, before the regression.** Answers "do I need to do
  anything?" first. The gold-case failure is flagged ⚠ because it is
  independently wrong (not contingent on approval).
- **Propositions are full sections, not table rows.** A table would force
  click-through to see evidence. The report's job is to make accept/reject
  decidable *without leaving the page.*
- **The regression-impact block is the most important part of each
  proposition.** This is what makes the loop falsifiable. Without it,
  approval is vibes; with it, every acceptance is a bet against a concrete,
  checkable prediction.
- **Confirmation is evidence-led, never a quota.** Independent support may
  strengthen an existing claim; record it against that claim without creating
  redundant propositions. No target share of confirmations or proposals exists.
- **Honest about being a code fix dressed as a proposition.** Some failures
  are bugs, not learned preferences. The report's voice distinguishes taste
  from bugs.
- **"Patterns observed (not yet proposable)"** is the negative space where
  the loop earns trust by *not* over-proposing. Each item notes what
  evidence would push it over threshold — telling the capture surface
  what is most valuable to capture next week.

## The full loop, end to end

```
principal reviews Top Jobs on dashboard
    → clicks 😍 / 😐 / 🥱 on a row              [reactions surface]
    → appends to reactions.jsonl                [Stream 1]
        ↓ (new eligible evidence, material decision, available budget)
proposed reflect.py operation runs
    → reads reactions + assessments + outcomes
    → proposes propositions with scope+evidence  [Stream 2]
    → runs gold-case regression                  [gold cases]
    → writes wiki/reflections/YYYY-MM-DD-*.md     [report surface]
        ↓
principal reads report over coffee
    → appends acceptance/rejection events
    → activation gate checks evidence, tests, scope and authority
    → matcher retrieves the applicable activated version
    → later independent evidence evaluates whether it helped
        ↓ (loop)
```

The loop can run as ordinary domain operations driven by any authorized
agent. The reflection operation never writes `soul.md`; a separate authored
profile change follows the principal's current authority. Activation requires
its review and validation receipts, not merely a changed JSON field.

## Sequencing

Use chapter 34 AS0/AS4/AS5 as the plan of record; these are its learning gates:

1. **Capture attributable evidence.** A shared principal-scoped operation for
   reactions and association events; cheap adapters, retry idempotence,
   append-only corrections and clear unresolved identity. Preserve raw text.
2. **Define targets and establish a baseline.** Pin a small useful set of
   cases with qualified labels. Separate hard invariants, development cases
   and time/opportunity-family-separated evaluation. Report missing labels;
   a target fixture count is not evidence sufficiency.
3. **Bound one reflection experiment.** A deterministic gate selects new
   independent evidence and a consequential uncertainty. Fix scope, proposal
   budget and stop rule; compare one candidate with baseline. Record negatives.
4. **Review and activate separately.** Present effects and limitations, append
   the decision, and activate an applicable version under existing authority.
   Retrieval is selective; no silent persona rewriting or automatic escalation.
5. **Measure later usefulness and retirement.** Check independent evidence,
   attention/compute cost, contradictions and distribution changes. Supersede
   stale claims without erasing history. Broader autonomy is a separate policy
   choice, never inferred merely from repeated success on development cases.

Acceptance is one traceable evidence → hypothesis → evaluated proposal →
reviewed activation → later observation cycle, including an honest no-change
result. A new runtime is not a prerequisite. A growing pile of proposals,
prompt text or model-generated agreement is not learning.

## Relationship to chapter 11's phases

Chapter 11 proposed phases A–E (make the ledger real → Tau agent → reflection
→ harness eval → Continual Harness stores). This chapter sharpens them:

- **Phase A (make the ledger real)** is mostly done for `runs` / `assessments`
  / `outcomes`. The missing ledger stream is `reactions`, defined here.
- **Phase B (Tau-backed agent)** is *not* on the critical path. The
  reflection loop is not an agent; it is a batch function. Defer Tau until a
  concrete workflow appears that MCP + a host agent cannot do.
- **Phase C (reflection agent)** is what this chapter specifies concretely as
  `reflect.py` + `propositions.jsonl` + the report.
- **Phase D (harness evaluation)** is the gold-case suite defined here.
- **Phase E (Continual Harness stores)** is downstream; propositions are the
  bridge to it.

The principle: establish evidence and measurement, then a small bounded
reflection loop. Regression success is necessary but not sufficient: held-out
or later evidence must support usefulness, with cost and uncertainty visible.
