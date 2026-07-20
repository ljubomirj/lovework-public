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

For candidate `p`, role `r`, company `c`, at time `t`, the current matcher is
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
P(candidate is viable: work authorisation, timing, basics)
P(interview | viable candidate, company, role)
P(offer | interview)
P(candidate accepts | offer)
P(candidate thrives after accepting)
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
