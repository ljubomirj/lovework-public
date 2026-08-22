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

Two kill checks fire before the LLM is ever invoked. Both return a reason
string and force a DROP with `AUTO-DROP:` prefixed to the reasoning.

### Work-authorization kill (`_check_work_auth_kill`)

Regex over the job's location + description text. Patterns like *US citizen
only*, *must be authorized to work in the US* (without sponsorship), *no visa
sponsorship*, *W-2 only* → auto-DROP. A bare *visa sponsorship available* is
**not** a kill. The principal's rules live in `profiles/<name>/work_auth.md`
so the matcher is principal-agnostic. See DECISIONS D10.

### Re-apply cooldown (`_check_reapply_kill`)

Two layers, run in order; the first hit wins (DECISIONS D14):

1. **Same-role cooldown** (6 months, configurable): same role + same org + an
   application/rejection within the window → DROP. Catches re-applying to the
   exact same role.
2. **Org-level cooldown** (18 months default, configurable via
   `LOVEWORK_REAPPLY_ORG_COOLDOWN_MONTHS=0` to disable): *any* rejection at the
   org within the window → DROP, regardless of role. Catches "Poolside rejected
   the Evaluations role 2 months ago — don't surface Poolside Pre-training
   today, even though the title differs." Uses `scan_history(use_gmail=True)`
   so a Gmail rejection not back-filled into the `.txt` file still trips it.

If a Gmail rejection is found but the `.txt` is empty, `crosscheck.py` can
back-fill it; `rescore.py` rebuilds the index from the corrected rules.

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

## Layer 2 — post-LLM reapply guard

`_apply_reapply_kill` runs after the LLM returns, as a final defence against
the same-role cooldown slipping through. If `reapply_kill` is set, the LLM's
score/decision are overridden to 0/DROP with `AUTO-DROP:` reasoning.

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
