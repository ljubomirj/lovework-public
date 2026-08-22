# Chapter 09 — Intelligence Layer Manifesto

> **Audience:** founders, builders, and agents deciding what LoveWork must own.
> **See also:** [`05-matcher.md`](05-matcher.md), [`03-profiles.md`](03-profiles.md), and [`../DECISIONS.md`](../DECISIONS.md) D17.

## The core claim

LoveWork is not valuable because it crawls jobs. Crawling, parsing, email
ingest, browser automation, LLM calls, and agent orchestration are plumbing.
They matter, but they are not the product's durable edge.

The product must own the **intelligence layer**: the longitudinal judgment loop
that learns a person over time and becomes increasingly faithful at predicting
which work will help that person flourish.

The core question is not:

```text
Does this job match this CV?
```

The core question is:

```text
If this person actually lived through this opportunity, under their real
circumstances and trajectory, would future-them thank present-them for pursuing it?
```

That is the value proposition. A job board finds listings. A recruiter fills a
role. LoveWork should model the person, simulate the fit, observe outcomes, and
improve the next decision.

## Simulation is the value proposition

The aim is not a more ornate recommender system. It is a practical simulation
of a working-life decision before the person pays its real price in time,
money, reputation, energy, disappointment, or missed opportunity.

LoveWork ultimately needs three linked simulations:

| Simulation | Question it must answer | Current starting point |
|---|---|---|
| **Person** | What work would this person genuinely like, be able to do, and grow through? | Three-layer profile, soul, work authorization, feedback, and outcomes. |
| **Company / hiring system** | What is this organisation actually trying to hire; how will it perceive this principal; what evidence or route would change the result? | Advert enrichment, provenance, prior-contact history, reach assessment, and application angle. |
| **Process / harness** | Which observations, questions, experiments, and actions are worth taking next—and how reliable is the system making that call? | Registry, decision ledger, regression cases, and reflection loop. |

The output is a **counterfactual rehearsal**, not a claim to predict a life
with certainty. It should make assumptions visible: “if the role is really as
described, if the team has this decision latitude, and if this evidence is
credible to them, then this is the likely lived experience and the most useful
next move.” When an assumption matters, the recommendation becomes an
experiment—read the primary page, ask a founder, seek a warm introduction,
test a portfolio angle, or defer—rather than a confident fiction.

This is how LoveWork can work where ordinary job search does not. Most people
underachieve relative to their potential because the market sees fragments of
them, opportunities are assessed too shallowly, and every exploratory move is
expensive. LoveWork's ambition is to help a person become an overachiever
relative to the gifts and constraints life gave them: not by promising a
perfect career, but by making far more of the expensive decisions well before
they must be lived.

```text
evidence about person + company + market
             -> explicit world model and assumptions
             -> rehearse plausible paths at near-zero action cost
             -> select the cheapest informative real-world experiment
             -> observe what happened and revise the model
```

The simulator is valuable precisely because reality remains the authority. Its
purpose is to spend less of reality on avoidable mistakes, not to replace
reality with a persuasive story.

For the formal probabilistic frame—separating fit from company hiring reality,
candidacy, and the value of the next action—see
[`13-probabilistic-simulator-model.md`](13-probabilistic-simulator-model.md).

## The persona simulator

The "living model of person" is a persona simulator, not a static profile. It
needs faithful representations of:

- **Persona:** wants, dislikes, taste, ambition, temperament, energy, identity.
- **Capability:** skills, evidence, credibility on paper, learning speed.
- **Circumstance:** money pressure, family, location, visa, health, time, current commitments.
- **Trajectory:** where the person might go next, not only where they have been.
- **Opportunity:** role, org, manager, culture, compensation, remote policy, risk.
- **Likely lived experience:** thrive, stagnate, burn out, get bored, grow, earn, learn, regret.

The current 3-layer profile model is the first version of this simulator:

```text
bio-long.md       -> the long path
cv-short.md       -> the present tip
possibilities.md  -> future branches
soul.md           -> taste, identity, wants, avoids
work_auth.md      -> hard constraints
roles/*.md        -> context-specific scoring lenses
```

The next version should make this explicit: LoveWork should simulate a person
taking an opportunity, not merely compare profile text to advert text.

The company model deserves equal seriousness. A role is not only a title and
requirements list: it is a hiring manager's problem, a team at a particular
stage, an applicant funnel, an ATS filter, budget, risk tolerance, and a
possibly unstated view of what would count as convincing evidence. LoveWork
should therefore represent both **principal fit** and **company receptivity**.
High intellectual fit with low screening reach is not a bad recommendation;
it is a different process hypothesis: perhaps “find an introduction”, “show
the speech-product evidence”, or “use this as a gap signal”.

## The decision object

The matcher currently returns score, decision, and reasoning. The intelligence
layer should evolve that into a structured decision object:

```text
fit_score
energy_score
growth_score
money_score
trajectory_score
risk_score
regret_risk
likely_failure_mode
likely_upside
recommended_action
questions_to_resolve
```

The action vocabulary should also grow beyond GO/MAYBE/FLAG/DROP:

```text
ignore
watch
research
discuss
apply
contact
create_case
defer_until_circumstance_changes
```

A good decision is not always "apply". Sometimes the correct action is to ask
one clarifying question, talk to a founder, wait for a UK hiring path, or use
the listing as evidence for an emerging profession.

## The judgment loop

The moat is the loop:

```text
simulate -> recommend -> observe reaction/outcome -> update person model -> retest old decisions
```

Examples of feedback that must update the simulator:

- "No, this sounds boring."
- "Actually this is exactly my kind of thing."
- "I applied and regretted it."
- "I ignored it but now wish I hadn't."
- "They rejected me."
- "The call was great; the title looked wrong but the team was right."
- "The work was good but the commute made it impossible."

These are not chat memories. They are training data for personal judgment.
LoveWork should keep a decision ledger tying together:

```text
opportunity_seen
system_recommendation
reasoning_at_the_time
user_reaction
action_taken
external_outcome
later_reflection
model_update
```

Over months, the ledger becomes more valuable than the raw crawl data.

## What LoveWork should own

LoveWork should own:

- The living person model.
- The opportunity representation.
- The decision policy.
- The decision ledger.
- The feedback and outcome schema.
- The harness that tests whether decisions improve.
- The explanation style that makes the judgment auditable to the client.

LoveWork does not need to own:

- The LLM provider.
- The crawler implementation.
- The email client.
- The browser automation substrate.
- The outer agent shell.
- The hosting/runtime abstraction.

Hermes, tau, Codex, browser tools, Firecrawl, OpenCode-Go, DeepSeek, or future
models can run the machinery. LoveWork's durable asset is the client-specific
intelligence state and the policy that uses it.

## The harness

An agent that claims to learn must have a harness. The harness should answer:

```text
Are we becoming a better coach for this person?
```

That is different from generic benchmark accuracy. The evaluation set is made
from past decisions, user corrections, and real outcomes. A future policy should
be tested against old cases:

- Would it still recommend the lead the user hated?
- Would it now catch the role that looked weak but became valuable?
- Would it respect a newly learned dislike?
- Would it preserve a long-term branch even when short-term fit is noisy?
- Would it avoid re-approaching an org after a rejection?

The near-term harness can be simple: a table of historical opportunities with
expected actions and notes. The long-term harness can resemble a continual
self-improvement system. The principle is the same: the intelligence layer
should improve by comparing its simulated judgment against lived evidence.

For the concrete architecture and research lineage, see
[`11-agentic-intelligence-harness.md`](11-agentic-intelligence-harness.md).

## Product thesis

"Find me jobs" is a feature.

"Learn who I am and guide my work-life trajectory over years" is a company.

The crawler feeds the system. The LLM speaks for the system. The agent drives
the system. But the product is the longitudinal intelligence layer: a faithful
persona simulator, calibrated by feedback, making better and better judgments
about what work this person should pursue.
