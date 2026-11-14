# Market Position — NN

_Loaded by the matcher alongside soul.md and work_auth.md. Provides
feasibility reality and flourishing signals so the score reflects what's
actually worth pursuing, not just semantic text overlap._

_Last updated: 2026-07-06_

---

## Market reality

This section describes how the market (recruiters, hiring committees,
screeners) actually sees NN's profile — not what NN wishes were true.

- **No recent ML academic publications.** No NeurIPS/ICML/ICLR/CoRL
  papers. No arXiv preprint track. This is the single biggest gap for
  Research Scientist titles at top-5 academic-adjacent labs.
- **No recent top-lab internship or postdoc.** No Ridge Analytics Group, Fathom Research,
  Anthropic, FAIR, Lumen Labs Labs Brain affiliation.
- **20 years in quant finance and trading systems.** The CV reads as
  "quantitative researcher/trader / platform builder" — not "ML
  research scientist." This is a strength for applied/infra/product
  roles but a weakness for pure research titles.
- **Strongest market lane:** applied ML engineering, agentic systems,
  research engineering (not research scientist), evaluation/tooling/
  platform roles, data-centric ML, quant+AI hybrids, small senior
  technical teams, AI-native product/R&D shops.
- **Top-lab pure Research Scientist roles are low-reach** unless
  there is a non-standard entry: personal referral, an unusual
  artifact (open-source project, blog, talk), founder-fit angle,
  applied systems emphasis in the JD, or the role explicitly values
  production engineering experience over publication record.
- **Older principal (50+).** Some teams filter on career stage for
  junior-mid roles; senior/head/principal titles bypass this.
  Age discrimination is real in tech hiring; roles that demand
  "10+ years of experience" or "staff/principal equivalent" are
  structurally more reachable than ones asking for "2-3 years
  post-PhD."

---

## Flourishing anti-patterns

These are roles that look good on paper (prestigious lab, impressive
title, high salary) but NN would likely dislike the day-to-day work.

**matcher signal:** When a role matches an anti-pattern below, drop
flourish_score by 2-3 points and note which anti-pattern in reasoning.

- **Narrow benchmark chasing.** Incremental SOTA improvements on
  fixed benchmarks (0.1% on GLUE, 1% on MATH). Paper-first work
  where the primary output is a publication, not a working system.
- **Large-org process.** Multi-quarter planning cycles, cross-team
  consensus, stakeholder management, pipedream project proposals.
- **Pure evaluation / metrics without building.** Designing eval
  sets, writing data cards, running human evaluation studies, no
  systems to ship.
- **Prestige role, low agency.** Famous lab + impressive title +
  a narrow pre-defined problem that you execute on without
  influencing direction.
- **Slow science.** Long feedback loops (months to see if an idea
  works), heavy experimental overhead, no fast prototyping.
- **Domain mismatch under prestige cover.** A lab that's famous
  for something NN doesn't care about (protein folding, climate,
  formal verification) hiring for "ML engineer" — the brand
  masks the domain misalignment.

---

## Flourishing signals

Roles that would energise NN. When a role matches these, add 1-2
points to flourish_score and name the signal in reasoning.

- **Build working systems.** The output is a running system, not a
  paper. Agentic workflows, tooling, platforms, data/feedback loops.
- **High agency, small senior team.** You own a problem area end-to-end.
  Fast iteration. Your decisions directly affect product direction.
- **Research that turns into product.** Applied frontier where you
  prototype something new and it ships. No separation between
  "research" and "engineering."
- **Weird applied frontier.** A problem where NN's 20-year
  cross-domain experience (quant finance + ML systems + trading
  infrastructure) is uniquely valuable — not generic ML engineering.
- **High-leverage infrastructure.** Building the platform/tools that
  enable others to do better research. Training infra, evaluation
  frameworks, data pipelines, simulation environments.
- **Quant + AI bridge only.** Roles at the intersection of quantitative
  modelling, markets/trading, and modern ML/AI can be useful only when AI/ML
  is central and the role moves NN from quant toward ML/AI. Plain quant
  trading or quant research is paused and should not be scored as a positive.

---

## Screening story notes

_How NN might (or might not) get through screening for common role types._

| Role type | Screening story | Typical reach |
|-----------|----------------|---------------|
| Research Scientist (top lab) | Needs referral or unusual artifact. CV alone won't pass. | 1-3 |
| Research Engineer (top lab) | Resume screen possible. Applied angle helps. | 4-6 |
| Applied ML / ML Engineer | Good match. Production emphasis is a strength. | 6-8 |
| Agentic Systems / AI Engineer | Strong match. Rapidly growing field, small teams. | 6-9 |
| Quant + AI bridge | Strong only if AI/ML is central and the role is R&D/tooling/agentic systems; plain quant trading/research is paused. | 5-8 |
| AI Platform / Tooling | Strong match. Infra + ML intersection. | 6-8 |
| Plain quant trading / quant research | Pre-2026 track, paused. Only revisit if requirements are genuinely AI/ML and useful as a bridge. | 1-3 |
| Staff/Principal individual contributor | Good match. Career stage alignment. | 5-7 |
