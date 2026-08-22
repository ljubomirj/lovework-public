# Chapter 11 — Agentic Intelligence Harness

> **Audience:** builders designing the next LoveWork agent.
> **See also:** [`09-intelligence-layer.md`](09-intelligence-layer.md), [`02-architecture.md`](02-architecture.md), [`10-ecosystem-survey.md`](10-ecosystem-survey.md), [`12-tau-dependency-strategy.md`](12-tau-dependency-strategy.md).
> **Local references:** `~/LJ-AI-agents/tau/`, `~/LJ-AI-agents/Continual-Harness-ARC-AGI-3/`.

## The thesis

LoveWork must become an agent, not just a pipeline.

The current system already crawls, extracts, scores, tracks lifecycle, writes a
wiki, and exposes an MCP surface. That is enough to find opportunities. It is
not yet enough to own the product's durable intelligence.

The durable product is the judgment loop:

```text
observe opportunity -> simulate person/opportunity fit -> recommend action
-> observe user response/outcome -> update person model and policy
-> make the next recommendation better
```

Hermes, Tau, LLM APIs, browser tools, Gmail scraping, Firecrawl, and MCP are
execution layers. They can help run crawls, parse pages, summarize evidence, or
host a tool loop. LoveWork's owned asset is the intelligence layer: the evolving
model of the person and the harness that turns lived feedback into better
future decisions.

## A simulator, not merely an agent loop

The harness should eventually operate as an editable, evidence-grounded world
model of work. It models three things together:

```text
person state       -> capability, taste, constraints, trajectory, evidence
company state      -> real role, team need, hiring process, filters, incentives
process state      -> source quality, uncertainty, available experiments, policy quality
```

For a lead, the harness should construct a compact model, replay the available
evidence against it, and rehearse possible paths before asking the human to
spend a real action. “Planning inside the model is free” is useful here in the
same limited, practical sense as it is for executable world models: compare
application angles, identify the pivotal unknown, and choose the cheapest
information-gathering action. The actual application, conversation, and job
remain the external test.

This implies a stricter decision object than a score:

```text
observations and provenance
-> explicit assumptions and competing hypotheses
-> simulated principal/company/process outcomes
-> recommended experiment or action
-> observed result
-> model and policy update, if justified
```

The model must be inspectable and replayable. A person should be able to ask:
“Why do you think this company would take me seriously?”, “Which part is an
inference rather than evidence?”, and “What result would change your mind?”

## Harness integrity: learning without hidden help

ARC-AGI-3 harness work is useful design inspiration, especially the ideas of
state grounding, mechanism discovery, executable hypotheses, backtesting, and
cheap planning inside a simulator. It is not a direct benchmark analogue for
career decisions. LoveWork needs an explicit integrity rule: distinguish what
the system discovered from what a human supplied after seeing the case, and
distinguish a genuine policy improvement from selecting a stronger model only
after feedback.

Every decision or regression case should therefore record:

- provenance for each decisive claim;
- whether a fact/rule was supplied by the principal, obtained from a primary
  source, inferred by the model, or added by a human reviewer;
- the model and policy used for the first assessment;
- any rerun, intervention, or fallback, and why it happened; and
- the held-out or later real-world evidence that supports the update.

This is not bureaucracy. Without it, a harness can look self-improving while
quietly accumulating human insight, post-hoc heuristics, or evaluation leakage.
The correct standard is not “no human knowledge”—the principal is necessarily
an expert source about their own life—but **labelled, auditable human knowledge
and a fair account of what the harness itself contributed**.

## What LoveWork should own

LoveWork should own these components directly:

| Component | Purpose | Why it is strategic |
|---|---|---|
| **Person model** | Living model of principal taste, capability, constraints, trajectory, and energy. | This is the persona simulator; it should become more faithful over time. |
| **Decision policy** | Current rules for fit/reach/flourish/action, thresholds, caps, and exceptions. | This is the judgment surface users experience. |
| **Decision ledger** | Append-only record of every recommendation, user response, action, and outcome. | This is the training data for future judgment. |
| **Reflection loop** | Periodic review of decisions and outcomes, producing profile/policy/test updates. | This is how the system learns with the client. |
| **Evaluation harness** | Regression tests for known judgment cases and historical decisions. | This prevents "learning" from breaking previous hard-won insight. |
| **Editable harness stores** | Prompt/policy, memory, skills, and specialist critics. | This is the bridge to Continual Harness-style self-improvement. |

LoveWork may outsource or swap these components:

| Replaceable layer | Examples |
|---|---|
| Agent runtime | Tau, Hermes, Codex, OpenCode, custom asyncio loop |
| Crawl/extract worker | SmartCrawler, Firecrawl, browser automation, external crawler |
| LLM provider | OpenCode Go, DeepSeek, Zai, Gemini, OpenRouter |
| Frontend | Markdown wiki, dashboard, MCP, future web app |

The rule is simple: outsource execution, own judgment.

## Tau as the near-term harness

Tau is a good near-term base because it has the right separation:

```text
tau_ai      -> provider/model streaming
tau_agent   -> portable harness, tools, events, messages, loop
tau_coding  -> one concrete app face
```

For LoveWork, use Tau's pattern rather than copying a coding agent wholesale:

```text
lovework_agent/
  tau_runner.py       # creates AgentHarness around LoveWork tools
  tools.py            # typed LoveWork tool surface
  prompts.py          # agent policy and task framing
  sessions.py         # transcript persistence and run state
```

The Tau-style harness should call existing LoveWork functions, not fork the
pipeline. `pipeline.run_pipeline()`, `JobMatcher`, `WikiStore`, `JobRegistry`,
`history.scan_history()`, and `cases.py` remain the domain engine.

### Initial LoveWork tool surface

The first Tau-backed agent should expose a small set of typed tools:

| Tool | Action |
|---|---|
| `run_pipeline` | Crawl one source or all sources for a profile/role. |
| `search_registry` | Query current and historical jobs. |
| `read_latest_report` | Load the newest report and summarize actionable leads. |
| `match_profile` | Score an ad hoc listing against a profile. |
| `inspect_history` | Check prior contact/application/rejection context. |
| `create_case` | Turn a pursued lead into an application case directory. |
| `record_feedback` | Store LJ's response: pursue, ignore, wrong, boring, too low reach, etc. |
| `propose_profile_patch` | Draft a reviewable profile or market-position update. |
| `propose_policy_patch` | Draft a reviewable matcher/prompt/test update. |

The agent should not silently edit the person model or policy. It should
propose diffs first, with evidence from the ledger.

## The decision ledger

The decision ledger is the first data spine. It should be append-only and
machine-queryable. The first version now lives under `lovework-agent/dataset/`
and is written as a byproduct of normal crawls:

```text
dataset/runs.jsonl         -> one row per pipeline run
dataset/assessments.jsonl  -> one row per scored finding
dataset/outcomes.jsonl     -> passive application/Gmail outcome evidence
```

The design rule is low friction: cron and existing `applications/`/Gmail habits
populate the dataset. Manual feedback tools can come later; they should not be
required for the historical dataset to start accumulating.

One row should represent one assessment or one user/outcome event:

```json
{
  "event_type": "assessment",
  "assessment_id": "uuid",
  "run_id": "uuid",
  "advert_hash": "registry-hash",
  "observed_at": "2026-07-06T21:00:00",
  "profile_name": "lj",
  "role": "general",
  "source": "neolabs",
  "org_name": "Isomorphic Labs",
  "title": "Research Scientist",
  "url": "https://...",
  "fit_score": 8.0,
  "reach_score": 2.0,
  "flourish_score": 3.0,
  "combined_score": 4.0,
  "decision": "FLAG",
  "recommended_action": "USE_AS_GAP_SIGNAL",
  "reasoning": "High intellectual fit, low screening reach...",
  "policy_version": "matcher-multiaxis-v1"
}
```

User feedback and outcomes should be separate events linked back to the
assessment when exact attribution exists. In the first implementation, outcomes
are often org-level because `history.py` can prove prior contact with an org
without always knowing which exact advert caused it:

```json
{
  "event_type": "outcome",
  "outcome_id": "uuid",
  "run_id": "uuid",
  "org_name": "Poetiq",
  "advert_hash": "optional-registry-hash",
  "kind": "rejection",
  "date": "2026-07-20",
  "source": "applications"
}
```

Runs carry the temporal context:

```json
{
  "event_type": "run",
  "run_id": "uuid",
  "started_at": "2026-07-09T12:00:00",
  "profile_name": "lj",
  "role": "general",
  "sources": ["neolabs", "hn_jobs"],
  "git_commit": "...",
  "profile_hash": "sha256...",
  "policy_version": "matcher-multiaxis-v1",
  "model": "mimo-v2.5"
}
```

This gives LoveWork enough data to learn the difference between aspirational
fit and live, usable opportunity.

## The reflection loop

The first agentic intelligence loop should be conservative:

1. Read recent assessments, feedback, and outcomes.
2. Identify recurring errors:
   - high-score roles LJ rejects immediately
   - low-reach prestige traps
   - boring day-to-day despite good title
   - good opportunities hidden under mediocre flat scores
   - repeated manual corrections to the same profile rule
3. Propose a patch to one of:
   - `profiles/<name>/market-position.md`
   - `profiles/<name>/soul.md`
   - matcher prompt/policy
   - scoring weights/caps
   - regression fixtures
4. Run harness tests on known cases.
5. Present the patch and evidence for review.

The agent should optimize for small, auditable changes. A reflection pass that
produces no patch is acceptable. Silence is better than noisy self-editing.

## Regression cases as product memory

Known judgment failures should become tests. Examples:

| Case | Expected behavior |
|---|---|
| Isomorphic Labs Research Scientist | High fit, low reach, low/medium flourish; action `USE_AS_GAP_SIGNAL`, not `APPLY_NOW`. |
| Ineffable MTS | Strong fit but needs angle; likely `WARM_INTRO_ONLY`. |
| Applied agentic-systems startup role | Higher reach/flourish; can be `APPLY_NOW`. |
| Pure benchmark/SOTA role | Low flourish even when fit is high. |
| US-only no sponsorship | Pre-LLM `DROP`. |
| Recent rejection at same org | Org-cooldown `DROP`. |

These are not merely tests of code. They are executable memory of the user's
judgment.

## Continual Harness as the future pattern

Continual Harness provides the longer-term pattern: improve the harness from
trajectory evidence. Its local implementation keeps four editable stores:

```text
prompt.current.md
memory.json
skills.json
subagents.json
```

and evolves them from recent trajectory windows with an append-only evolution
log. LoveWork can map this directly:

| Continual Harness store | LoveWork equivalent |
|---|---|
| Prompt | Decision policy: scoring/action prompt, thresholds, exceptions. |
| Memory | Person/job-market facts with confidence and provenance. |
| Skills | Reusable procedures: assess top-lab reach, inspect compensation, extract hidden visa rules, evaluate founder fit. |
| Subagents | Specialist critics: reach critic, flourishing critic, prestige-trap critic, CV-gap critic, opportunity-upside critic. |
| Trajectory | Decision ledger: assessments, user feedback, outcomes, tool traces. |

Do not start here. Continual Harness-style refinement is powerful only after
LoveWork has enough trajectory data and regression tests. Otherwise it becomes
prompt churn.

## Research grounding and reading notes

These are design references, not claims that their reported benchmark results
transfer to LoveWork. The useful common pattern is explicit state, executable
hypotheses, persistent learning stores, replay, and a clear account of what
was learned from which evidence.

| Reference | Relevance to LoveWork | Caution / use |
|---|---|---|
| [Schema](https://schema-harness.github.io/) / [Haven Feng's ARC-AGI-3 explanation](https://x.com/HavenFeng/status/2077770350700765578) | Joint state grounding and mechanism discovery; an editable program as world model; planning inside a certified simulator. | Treat the published public-set score as self-reported until independently verified. LoveWork borrows the epistemic pattern, not the benchmark claim. |
| [Greg Kamradt's Schema notes](https://x.com/GregKamradt/status/2077949388673151332) | Clear formulation of an interpretable, diffable, replayable program-world-model. | Keep the concern about post-hoc model fallback and human/environment knowledge injection visible in every LoveWork evaluation. |
| [Poetiq — “Benchmarks Are Dead”](https://poetiq.ai/posts/benchmarks_are_dead/) | A useful prompt to assess systems by durable real-world capability rather than a single static score. | LoveWork's target is not benchmark performance; it is better career-life judgment with auditable evidence. |
| [Shilong Liu — *A Taxonomy of Self-evolving Agents*](https://x.com/Shilong_Liu_AI/status/2074800880017342665) | Separates artifact, harness, and model evolution; gives three questions: what evolves, what feedback drives it, where does the loop close? | LoveWork is primarily harness evolution; its loop must close on the person's career life, not its own reports. |
| [Continual Harness](https://continual-harness.github.io/) and [code](https://github.com/feng-rrRay/Continual-Harness-ARC-AGI-3) | Persistent memory, reusable skills, subagents, reset-free refinement, and action efficiency. | Adopt its persistent-store discipline only with reviewable changes and regression tests; do not chase its ARC-specific machinery. |
| [Lilian Weng — *Harness Engineering for Self-Improvement*](https://lilianweng.github.io/posts/2026-07-04-harness/) | Filesystem memory, background work, context engineering, harness optimisation, evolutionary search, and the risks of reward hacking or diversity collapse. | Prefer structured, provenance-bearing stores and small proposed patches over rewriting one giant prompt. |

Local, preserved sources for deeper reading:

- `~/Whimsical/doc/Self-evolving_Agents_Taxonomy_x_LoveWork.md` — the prior
  LoveWork-specific mapping of Shilong Liu's taxonomy.
- `~/Whimsical/doc/A_Taxonomy_of_Self-evolving_Agents-X-Shilong_Liu_AI-2074800880017342665-20260709.md`
  and `.txt` — source capture and working notes.
- `~/Whimsical/doc/Continual_Harness_An_Efficient_Self-Improving_Agent_on_ARC-AGI-3-jun2026.{pdf,mhtml,txt}`.
- `~/Whimsical/doc/Harness_Engineering_for_Self-Improvement-lilianweng.github.io-jul2026.{pdf,mhtml}`.

## Proposed phases

### Phase A — Make the ledger real

- Done: promote the active ledgers to stable `lovework-agent/dataset/*.jsonl`
  instead of `cache/`.
- Done: add `run_id`, `advert_hash`, profile hash, policy version, model, and
  git commit to the run/assessment spine.
- Done: append passive `outcome` rows from existing `history.py` scanning of
  `applications/` and Gmail.
- Next: add explicit user feedback rows only when there is a low-friction UI or
  future LoveWork submission agent. Avoid a separate manual CLI as the primary
  capture path.
- Later: add `reflection` and `policy_change` rows when the reflection agent
  begins proposing profile/policy/test patches.

### Phase B — Tau-backed LoveWork agent

- Wrap existing LoveWork tools in Tau-style typed tools.
- Create a simple `lovework_agent/tau_runner.py`.
- Agent task: read latest report, propose next actions, ask targeted questions,
  record feedback.
- Keep all domain logic in existing modules.

### Phase C — Reflection agent

- Add a scheduled reflection command:

```bash
../venv/bin/python3 reflect.py --profile lj --role general --since 30d
```

- It reads ledger + wiki + profile files.
- It writes a proposed markdown/diff artifact under `wiki/reflections/`.
- It does not auto-apply profile or policy changes.

### Phase D — Harness evaluation

- Build a small fixture suite of known cases.
- Every proposed policy/profile change must run against the fixture suite.
- Store before/after scores so the agent can explain the effect of a change.

### Phase E — Continual Harness-style stores

- Add editable stores:

```text
lovework-agent/intelligence/
  policy.current.md
  memory.json
  skills.json
  critics.json
  evolution.jsonl
```

- Add a conservative evolver that proposes changes from trajectory windows.
- Accept changes only after tests pass and LJ approves, at least initially.

## Non-goals for the first agentic version

- No autonomous applications.
- No silent edits to profile or policy.
- No self-modifying code.
- No hidden memory store outside the repo/data directory.
- No generic "AI coach" behavior detached from observed opportunities.

The first agent should be a disciplined operator and reflective analyst, not a
free-running life coach.

## Design rule

Every agentic action should leave evidence:

```text
What did it observe?
What did it infer?
What did it recommend?
What did the user do?
What happened?
What should change next time?
```

If LoveWork captures that loop, it becomes more than a job-search tool. It
becomes a longitudinal work-fit simulator that learns with the person.
