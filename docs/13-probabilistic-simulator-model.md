# Chapter 13 — Probabilistic Simulator Model

> **Audience:** builders and agents extending LoveWork's intelligence layer.
> **Status:** design frame, not a calibrated predictor.  See also
> [`09-intelligence-layer.md`](09-intelligence-layer.md).

## Why probabilities

Work discovery is a changing world with partially observable actors: a person,
a company, a role, other applicants, and the hiring process itself.  A single
"match score" hides these different uncertainties and encourages an agent to
sound more certain than its evidence permits.

LoveWork should therefore treat a recommendation as a probabilistic, explicit
model.  Its purpose is not to predict a life exactly.  It is to identify the
assumption that matters most and choose the cheapest useful real-world action
to test it.

## Separate the questions

For principal `p`, role `r`, company `c`, at time `t`, the current matcher is
best understood as a **fit / utility** estimate:

```text
U_fit(p, r, c, t)
```

It asks: *would this work plausibly suit this person and their future?*
That is not the same as the chance of an offer.  The latter contains separate
uncertainties:

```text
P(role is real and still open)
P(company can and intends to hire)
P(principal is viable: work authorisation, timing, basics)
P(interview | viable principal, company, role)
P(offer | interview)
P(principal accepts | offer)
P(principal thrives after accepting)
```

The chance that a pursued lead becomes accepted work is a chain of conditional
events, not a property of the CV alone:

```text
P(accept) = P(open)
          × P(hire | open)
          × P(viable | hire)
          × P(interview | viable)
          × P(offer | interview)
          × P(accept | offer)
```

This factorisation is a thinking aid, not an assertion that the events are
independent.  It makes it possible to say, for example: “the fit is excellent;
the dominant uncertainty is whether the company will hire anyone.”

## The company and hiring model

`P(hire | open)` is a latent company state inferred from evidence, rather than
from the advert alone.  A dossier should preserve both the evidence and its
source class:

| Area | Useful evidence | What it informs |
|---|---|---|
| Legal reality | company register, incorporation, officers, accounts | entity exists; age; public financial limits |
| Hiring reality | repeated adverts, named hiring lead, evidence of prior hires | whether a real hiring process exists |
| Product reality | working web/app surfaces, release cadence, user evidence | execution and product maturity |
| Team reality | likely colleagues, roles, locations, prior work | daily-work hypothesis and reach route |
| Financial reality | filings, named investors, company claims | hiring capacity and runway, with confidence |
| Work reality | employer entity, location, contract, scope, decision maker | whether the role is practically viable |

Every statement must carry a provenance and confidence label: for example,
*registry verified*, *primary company claim*, *marketplace metadata*,
*credible third-party reporting*, or *unverified inference*.  Missing public
evidence is not negative evidence by default—new companies may legitimately
have no filed accounts or press coverage.

## Decisions under uncertainty

The recommendation should combine likelihood with value and cost, rather than
maximise an invented offer probability:

```text
expected value of pursuing
  = P(offer) × value of accepting and thriving
  + learning / relationship value
  - application time, opportunity cost, and emotional cost
```

The next action should usually maximise **information gained per unit of real
cost**.  Examples include reading the primary role page, trying the product,
asking the named manager one concrete question, seeking an introduction, or
deferring until a known funding event.  “Apply” is one action in this vocabulary,
not the automatic answer.

## Honest use before calibration

With one user and initially few outcomes, LoveWork cannot estimate most of
these probabilities accurately.  Early numbers are disciplined priors—a
reasonable, auditable guess—not measurements.  The system must therefore:

- prefer ranges or qualitative bands (`low`, `medium`, `high`) to spurious
  decimals;
- show the assumptions and the evidence that would change them;
- keep fit, hiring reality, candidacy, and expected value separate;
- record confidence in the estimate as well as the estimate; and
- allow the person to disagree with the prior explicitly.

## Learning loop

The decision ledger turns this frame into an eventual model.  For each lead it
should retain the evidence available at the time, predicted bands, action,
company response, interview/offer outcome, later reflection, and any changed
belief.  Over time, observed outcomes can calibrate the priors and reveal which
signals predict genuine hiring, reach, satisfaction, and regret for this
specific person.

```text
evidence -> explicit beliefs -> low-cost action -> observed outcome
        -> ledger -> calibrated future beliefs
```

Until that data exists, the model's real value is intellectual hygiene: it
prevents a persuasive match narrative from concealing the uncertain moving
parts that decide whether an opportunity is worth living.

## Rejection evidence as a decaying prior (proposal, 2026-10-17)

> Status: WRITTEN, NOT IMPLEMENTED. Distilled from LJ, 2026-10-17, the day after
> the PostHog screen-reject. The shipped approximation is the reapply hard-kill
> (`matcher._check_reapply_kill`): same-role 12 months, org-level 9 months
> (`LOVEWORK_REAPPLY_ORG_COOLDOWN_MONTHS`), now reading the outcomes ledger as
> well as the applications scan. This section is what the principled version
> looks like, for the day the constants start to feel arbitrary.

The cooldowns are cliffs: P(apply | org) = 0 inside the window, unconditioned
outside. The principled object is a per-org suppression state driven by
evidence, not by a constant:

- Each org carries s(t) ∈ [0, 1] — the posterior probability that applying
  again now is wrong (would repeat the known outcome).
- A rejection event sets s = 1 at the event date (well: a screen-reject sets
  it lower than an interview-stage reject would — evidence weight should
  carry the stage it came from; a screen-reject is weak evidence about the
  principal and strong evidence about the funnel's filters).
- Between events, s decays exponentially: s(t) = 2^−(t − t_event)/T½. Teams
  churn, funnels change, the principal's profile moves. A half-life T½ of
  6–9 months matches LJ's stated 9–12-month no-reapply instinct once you
  also multiply in P(advert is genuinely new).
- Application decisions then stop being gated and start being weighted:
  apply when E[value] = P(hire) · U − C clears a threshold, with the org's
  P(hire) multiplied by (1 − s). An exceptional GO can eventually outweigh
  an old reject; a mediocre MAYBE never does. That is the behaviour the
  cliff approximates crudely: at 18 months, s ≈ 0.15–0.25 for T½ = 6–9.

What this buys over the constants: the two knobs (evidence weight by stage,
half-life) are measurable from the outcome ledger once enough rows
accumulate — the same measurement-first doctrine as the rest of this
chapter — instead of one integer chosen by feel. What it costs: a state
parameter per tracked org, and the discipline not to tune T½ per org after
the fact (it must stay one number, or it becomes narrative again).

### Evidence against is a ledger, not an event (LJ's framing, 2026-10-17)

"Half-life as probability of not-applying, evidence against, weighted
against our probability of applying" says more than the single-event
version above. An org's history with the principal is a *sequence* of
events, each carrying signed evidence about whether applying again is
futile. The clean algebra is log-odds with per-event decay:

    λ(t) = Σ_k  w_k · 2^(−(t − t_k)/T½)        s(t) = 1 / (1 + e^(−λ))

where w_k is the signed weight of outcome k at time t_k. Per-event decay
(not decaying one accumulated state) falls out of the arithmetic: two
year-old rejections weigh less than one fresh one; an interview-last-time
subtracts from futility rather than adding to it. Starting weights, to be
replaced by fits when the ledger has rows:

| prior outcome at org | w_k (evidence that applying again is futile) |
|---|---|
| screen-reject (applied, template reject) | +1.5 |
| interviewed, then rejected | +2.5 |
| applied, outcome unknown (the Poetiq class) | +0.5, wide variance |
| interviewed, no recorded outcome | +0.5 |
| offer (declined or lapsed) | −2.0 — you are their kind of candidate |
| withdrawal (we pulled out) | 0 |

Note the asymmetry the table encodes: a screen-reject is weak evidence
about the principal and strong evidence about that funnel's filters, so it
decays toward irrelevance fastest in expectation — filters and recruiters
churn quicker than a profile does.

### Which probability does the weight multiply?

Two readings of "weight against our probability of applying", and they are
not the same model:

1. **Multiply P(hire) inside E[V]** — the optimiser's reading, and the one
   LoveWork should implement: each advert is one decision, apply iff
   P(hire)·(1−s)·U > C. No randomness, fully auditable.
2. **Multiply the apply-propensity P(apply)** — the sampler's reading: the
   system *sometimes* applies, with probability damped by s. This is what
   generates exploration, and reading 1 has no exploration at all: an org
   scored a hair under the bar is never sampled again even after s has
   decayed enough that its true EV is positive. If lockout ever shows up
   in the ledger, the fix is a small explore term (ε-floor on (1−s), or a
   periodic s-reset on the org's oldest evidence), not a switch to
   stochastic applying.

### One decay family, two clocks

This is not the model's first decay constant. Chapter 45's EXPIRED pile
already runs one in production: a case's value halves every 15 days and
retires at ⅛ after 45 days — *their* silence about *one case*, clocked in
days. This section's T½ is *their* rejection of *us at the org*, clocked
in months. Same functional form, different question, different clock; the
staleness doctrine's 10-day cap is the hard-knife cousin on the advert
itself. The simulator gains no new mathematics here — one more constant
and a ledger.

### Calibration path

`outcomes.jsonl` already carries everything the fit needs — `kind`
(applied / interview / offer / rejection) and `date` per org. The
measurable is P(eventual success at org | last outcome kind, Δt since it);
fit w_k and T½ from it when rows accumulate, and until then keep the
constants above under the one-number discipline. The reapply hard-kill's
6/18-month cliffs remain the shipped behaviour; this section is the shape
they approximate.
