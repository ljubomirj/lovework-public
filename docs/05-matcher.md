# Chapter 05 — The Matcher

> **Audience:** everyone; especially anyone interpreting a GO/MAYBE/FLAG/DROP.
> **See also:** [`../lovework-agent/matcher.py`](../lovework-agent/matcher.py), [`../lovework-agent/dspy_signatures.py`](../lovework-agent/dspy_signatures.py).

The matcher is the third LLM call site in the pipeline. Before that call,
LoveWork fetches the primary advert and retrieves relevant facts from both the
short profile and long bio. The result includes three score axes plus an
evidence alignment matrix, material gaps, screening story, likely day-to-day,
and a truthful application angle. Two cheap rules still fire before the LLM to
drop obvious non-fits without spending a call.

## The decision scale

| Score | Decision | Meaning |
|------:|----------|---------|
| 8–10  | **GO**    | Strong fit. The principal should apply today. |
| 5–7   | **MAYBE** | Plausible fit, some misalignment. Worth a look. |
| 3–4   | **FLAG**  | Low signal or partial misalignment. Only if nothing better. |
| 0–2   | **DROP**  | Clearly wrong. |

For **emergent-profession principals (vj, pk)** the matcher leans toward MAYBE
over DROP — the goal is cluster density, not single-match optimisation. See
[chapter 03](03-profiles.md).

## Layer 0 — pre-LLM hard-kills (cheap, no LLM call)

Three families of checks fire before the LLM is ever invoked. All return a
reason string; the two hard-kills force a DROP with `AUTO-DROP:` prefixed to
the reasoning, and the declared exclusions force a low-score DROP.

### Work-authorization kill (`_check_work_auth_kill`)

Regex over the job's location + description text. Patterns like *US citizen
only*, *must be authorized to work in the US* (without sponsorship), *no visa
sponsorship*, *W-2 only* → auto-DROP. A bare *visa sponsorship available* is
**not** a kill. The principal's rules live in `profiles/<name>/work_auth.md`
so the matcher is principal-agnostic. See DECISIONS D10.

### Country work-authorisation kill (`_check_country_auth_kill`)

Declared with a marker in `profiles/<name>/work_auth.md`:

```html
<!-- lovework:work-auth-exclude-countries: US -->
```

The text-based kill above never fires on a role that merely *sits* in an
excluded country: VJ's 2026-09-13 sweep surfaced 72 US/Canada-located listings —
`United States`, `REMOTE (US)`, `San Francisco, CA`, `NYC`, `Austin, TX` — some
scoring MAYBE while the profile calls a purely US role an automatic DROP.

The rule is deliberately conservative, because a wrong kill is worse than a
wrong MAYBE:

- it fires only when the **location** is exclusively in an excluded country;
- a location naming an eligible region is left to the LLM
  (`London, UK or NYC`, `REMOTE (US/Canada/Brazil/Poland/UK/India)`), and so is
  any advert whose text shows a real path (*sponsorship*, *employer of record*,
  *remote worldwide*, *any location*);
- only **unambiguous** US signals count. State codes that collide with country
  codes are excluded on purpose: `Berlin, DE` must not read as Delaware, and
  `Valletta, MT` must not read as Montana — Malta is one of VJ's preferred
  markets. Ambiguous `City, XX` forms go to the LLM instead.

On VJ's live report it kills 67 of 263 distinct locations, sparing every mixed
one. LJ's profile states the same US-avoidance in prose; the marker is not yet
added there, so LJ keeps only the text rule until he asks for it.

### Re-apply cooldown (`_check_reapply_kill`)

Two layers, run in order; the first hit wins (DECISIONS D14):

1. **Same-role cooldown** (12 months, configurable): same role + same org + an
   application/rejection within the window → DROP. Catches re-applying to the
   exact same role.
2. **Org-level cooldown** (9 months default, configurable via
   `LOVEWORK_REAPPLY_ORG_COOLDOWN_MONTHS=0` to disable): *any* rejection at the
   org within the window → DROP, regardless of role. Catches "Poolside rejected
   the Evaluations role 2 months ago — don't surface Poolside Pre-training
   today, even though the title differs." Uses `scan_history(use_gmail=True)`
   so a Gmail rejection not back-filled into the `.txt` file still trips it.

If a Gmail rejection is found but the `.txt` is empty, `crosscheck.py` can
back-fill it; `rescore.py` rebuilds the index from the corrected rules.

### Principal-declared exclusions (`_check_profile_preference_exclusion`, `_check_ideology_exclusion`)

A principal can rule out a *class of work* or a *class of employer* in their own
soul.md, with a marker comment. Both are opt-in per principal — the engine holds
no opinion of its own — and both decide **before** the LLM, so an excluded
employer costs nothing to reject.

| Marker | What it excludes | Who declares it |
|---|---|---|
| `<!-- lovework:exclude-ai-ml-nlp-research -->` | a profession: AI/ML/NLP research roles | vj |
| `<!-- lovework:ideology-exclusion orgs: Anthropic -->` | named employers, on ideological/cultural grounds | lj |

Semantics: all three axes are floored to 1.0, `combined_score` to 1.0, and the
action to DROP, with the reason carried into the reasoning text. Fit and reach
are not re-derived — the marker is a declaration, not an assessment.

Organisations are matched as a **word subset**, so `Anthropic` also catches
`Anthropic_Fellows` and `Anthropic PBC`. That matters: on the 2026-09-12 full
sweep, `Anthropic Fellows` roles surfaced at Fit 8.5 / Flourish 7.0 precisely
because the declared name did not match the alias, and because the ideology
rule then lived only as prose in the prompt — the model applied it
inconsistently (some rows 1.0, others 7.0).

This implements **P-0002** (`Anthropic / EA-aligned orgs downweighted on
ideology`, accepted 2026-07-15): LJ is a bloomer and e/acc — his own public bio
lists e/acc, and his X archive calls Anthropic "doom cult central" — so a role
there is a near-zero *flourishing* prospect regardless of technical fit. Until
2026-09-12 the rule was a preference recorded in the propositions table and
never enforced by the engine. `rescore.py` carries the same checks, so
historical index rows are corrected with fresh ones.

## Layer 1 — the LLM score

After the kills pass, the LLM is prompted with the full profile string (soul →
work_auth → cv-short → possibilities → role — see [chapter 03](03-profiles.md))
plus the job and additional context (registry status + prior-contact summary).
`principal_evidence.py` retrieves the most relevant profile/bio paragraphs
using deterministic lexical and domain-synonym matching (for example,
voice/audio/transcription → speech/ASR). The LLM must tie each positive claim
to those supplied facts, state unsupported requirements as gaps, and produce a
specific application angle rather than generic enthusiasm.

`enrichment.py` supplies the full primary-page copy before retrieval and
matching. This prevents terse discovery snippets and unhelpful HTML metadata
from dominating the assessment.

Successful assessments are content-addressed under `cache/assessments/`, keyed
by matcher version, principal/role/model, advert evidence, and job metadata.
Provider failures are never cached and are reported as **UNSCORED**, not as a
numeric zero/DROP.

### Special signals in the prompt

The prompt instructs the LLM on a few non-obvious rules:

- **Long-lasting jobs** (open >30 days, `long_lasting` lifecycle) score 1–2
  points **lower** — the company may be picky or unserious.
- **Prior rejection for same role within 6 months** → score 0 (DROP).
  (Defensive double-cover of Layer 0's same-role kill.)
- **"New" status** (first seen this run) → fresh opportunity, no penalty.
- **Branching-possibilities bonus (D17):** if the role aligns with one of the
  principal's explicit branching possibilities (the `matcher signal:` lines in
  `possibilities.md`), add **+1** and name the branch letter in the reasoning.

The bonus is the mechanism by which Layer 3 of the profile actually affects
the score. Without it, the matcher would systematically under-weight roles
that point at a direction the principal is actively exploring.

## Layer 2 — post-LLM deterministic guards

Two rules run after the LLM returns and can override its score, because a
plausible-looking advert must not be recommended on fit alone:

### Reapply guard (`_apply_reapply_kill`)

A final defence against the same-role cooldown slipping through. If
`reapply_kill` is set, the LLM's score/decision are overridden to 0/DROP with
`AUTO-DROP:` reasoning.

### Staleness (`_apply_staleness`)

An advert that has been open a long time is a firm-side failure — it cannot fill
the role, filled it and never closed the advert, or farms CVs for the ~10% of
staff who leave each year — and all three are bad for the principal. Age is the **larger of two lower bounds**: `posted_at`
(the date the source reports — HN's comment `created_at`, Jack & Jill's feed
publication date) and `registry.age_days` = `last_seen − first_seen`, i.e. how
long *we* have observed the advert open. Each alone is a **lower bound** on the
real age (an org first crawled last month hides its year-old posting; a J&J
re-list resets the feed's date): the rule can under-punish, never wrongly kill a
fresh advert — and taking the larger stops a relist laundering a long-open role. Thresholds are env-tunable and deliberately tight:

| Observed open | Effect |
|---|---|
| `< LOVEWORK_STALE_FLAG_DAYS` (10) | untouched |
| `10–24 days` | combined score capped at `LOVEWORK_STALE_FLAG_CAP` (4.0) → FLAG, reasoning prefixed `STALE (Nd):` |
| `>= LOVEWORK_STALE_DROP_DAYS` (25) | score 0 → DROP, reasoning `AUTO-DROP: stale advert: …` |

`LOVEWORK_STALE_DROP_DAYS=0` disables the rule. The LLM prompt carries the same
doctrine (`STALE ADVERTS ARE NEAR-ZERO VALUE`) so the model does not spend score
on an advert the engine will discard, and every report listing shows its observed
age. The rule is stricter than the registry's `long_lasting` label (30 days) by
design. Doctrine, measurement limits and the audit tool: the `stale-adverts`
skill; measure a crawl with `stale_audit.py --profile <p>`.

## The DSPy adapter (`JobMatcherDSPyAdapter`)

A drop-in replacement for `JobMatcher` that uses typed DSPy signatures
(`dspy_signatures.MatchJob`) instead of hand-written prompts. Same interface;
same kills; same bonus rules (encoded in the signature's docstring). The
advantage is that DSPy signatures can be optimised against a metric
(`BootstrapFewShot`, `MIPRO`, `GEPA`) — the planned Phase-2 quality lever.

Verified consistent with the legacy matcher on a held-out set (see
`ARCHITECTURE.md` section 6). Use via `--dspy` on the CLI, or
`use_dspy=True` on `run_pipeline`.

The Talk Machine founding-engineer role is a golden regression: its advert
must retrieve LJ's speech-recognition, spoken-document/playlist demo, product,
and small-team evidence, and the resulting contract must support an APPLY_NOW
high score.

## What a GO means in practice

- **lj, kj:** apply today. The matcher has checked work-auth, re-apply
  cooldowns, prior contact, lifecycle, *and* found a branching-possibilities
  match. It is the strongest signal LoveWork produces.
- **vj, pk:** strong data about a possible profession. Read the reasoning —
  the named branch letter tells you which seed direction this role points at.
  The application decision is the principal's, after cluster review.

## What's next

- [`06-dashboard-mcp.md`](06-dashboard-mcp.md) — call `match_profile` as a tool.
- [`03-profiles.md`](03-profiles.md) — what the `matcher signal:` lines look like.
