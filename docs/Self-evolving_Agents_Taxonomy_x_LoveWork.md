# Self-evolving Agents × LoveWork — Summary & Implications

**Source:** [A Taxonomy of Self-evolving Agents](https://x.com/Shilong_Liu_AI/status/2074800880017342665) by Shilong Liu, 2026-07-08
**Tailored for:** LoveWork's intelligence layer, persona simulator, decision ledger, and longitudinal judgment loop
**Date:** 2026-07-09

---

## Core Thesis in One Sentence

Self-evolving agents improve at three levels — the outputs they produce (**artifacts**), the way they're built (**harness**), or the model itself (**weights**) — and the future lies in evolving all three together.

---

## What This Means for LoveWork

LoveWork is already a self-evolving system, just not yet self-aware of the taxonomy it occupies. Mapping Shilong's framework onto LoveWork reveals where you already do self-evolution, where you could formalise it, and where the hard problems still lie.

---

### Level 1: Artifact Iterative Optimization — Already Running

**Taxonomy definition:** An agent repeatedly produces, evaluates, and refines outputs (artifacts) against criteria set by a human.

**How LoveWork does this today:**

| Artifact | Loop | Evaluation |
|----------|------|------------|
| Crawl results (registry entries) | Crawl → parse → score → log | LLM fit/reach/flourish/action scores |
| Wiki pages | New org/role → write markdown → archive | Human review (implicit) |
| Opportunity recommendations | Pipeline → score → GO/MAYBE/FLAG/DROP | User decision (apply/ignore) + outcome |
| Case files | Lead → case_dir → README → status checklist | User follow-through |

**What you could formalise:**

The scoring pipeline is already an iterative optimisation loop — it takes a raw job posting and transforms it into a scored, categorised recommendation. But the loop currently runs once per crawl. A *genuine* artifact-iteration approach would re-evaluate prior decisions:

> *"Given what I've learned about LJ's preferences from the last N decisions, would I still score this role the same way?"*

This is what Shilong calls "the improvement-verification loop" — and LoveWork could run it as a periodic background pass: re-score old `MAYBE` roles with updated profile context, re-rank stale entries, and flag divergences.

**Relevant quote:**
> *"With stronger large models, this loop becomes useful for accelerating engineering and scientific discovery."*

For LoveWork: stronger models → better assessment of subtle fit signals (culture, growth arc, mission alignment) that currently fall through the scoring cracks.

---

### Level 2: Agent Harness Self-improvement — This Is LoveWork's Core Differentiator

**Taxonomy definition:** Improving the agent itself (prompts, memory, tools, skills) without updating model weights.

This is where LoveWork's "intelligence layer" lives. Shilong's taxonomy gives you a vocabulary for what you've already built and points to what's missing.

#### Prompt Learning & Memory ← You have this

LoveWork's multi-axis scoring prompts (fit/reach/flourish/action), the persona simulator, the work-auth kill, and the per-profile criteria files are all **prompt-level harness** — structured knowledge injected into the LLM's context window.

But are they improving over time? Currently, prompts are hand-tuned by LJ and the agent. A harness-level self-improvement loop would:

- Log which scoring prompts produce decisions that correlate with positive outcomes
- Automatically adjust emphasis weights across axes based on longitudinal data
- Extract recurring patterns from the decision ledger and inject them as hard rules

**Relevant papers — and LoveWork equivalents:**

| Paper | Idea | LoveWork analogue |
|-------|------|-------------------|
| **GEPA** | Extract rules → store in prompts | The "rules" in the persona simulator + work-auth kill |
| **ACE** | Structured playbooks | The scoring criteria, profile layers, decision checklist |
| **Mem0** | Persistent memory for agents | The SQLite registry + CSV ledger + decision database |

#### Tool & Skill Creation ← Incubating

LoveWork already creates tools: the pipeline itself, the sources (Gmail, HN, company pages, LinkedIn), the REPL, the wiki writer. But these are hand-authored, not agent-generated.

The taxonomy points to a harder but more valuable path: **LoveWork should create its own tools and skills**.

Concrete example: after observing that LJ engages more with roles from mission-aligned climate-tech startups than from big finance, LoveWork could:
1. Generate a `climate_tech_scorer.py` tool that enriches any job listing with a climate-alignment heuristic
2. Register it as a skill (in the `lovework/.claude/skills/` structure that already exists)
3. Automatically apply it in subsequent pipeline runs

**This is already prefigured in your codebase.** LoveWork's skill directories (`lovework-agent/skills/`, synced via `sync_skills.sh`) exist. The agent runtime (`agent_runtime.py`) is extensible. What's missing is the *creation loop* — an agentic step that says "I notice this pattern; let me build a tool to handle it and slot it into my own harness."

#### Multi-agent Self-evolving ← Your Roadmap

> *"If a user only cares about stock-related questions, they do not need cooking tools."*

LoveWork already runs 4 profiles (LJ, KJ, VJ, PK) — each with different career arcs, value systems, and criteria. Shilong's multi-agent thesis maps directly onto your 3-layer profile architecture:

| Profile | Domain | Could benefit from a dedicated expert agent |
|---------|--------|---------------------------------------------|
| LJ | Quant finance, AI/ML, technical leadership | Quant-analyst agent, AI-startup agent |
| KJ | Clinical / healthcare | Medical-career agent |
| VJ | (Mystery profile) | — |
| PK | (Mystery profile) | — |

The routing problem Shilong flags ("how to find the suitable agent") maps onto your profile-selection mechanism (`--profile lj/vj/kj/pk`). As you scale to Phase 3 (multi-candidate service), routing becomes a first-class problem: which profile does this opportunity suit best? What if it suits two?

**A human is a router.** LoveWork's router is currently LJ picking `--profile`. The next step is automated routing — and the taxonomy says this requires strong base models.

---

### Level 3: Model Learning without Gold Answers — The Moat

**Taxonomy definition:** Updating model weights using weak or self-generated signals when no ground truth exists.

LoveWork can't fine-tune the underlying LLM (DeepSeek, GPT, Claude). But the *principle* — learning from weak signals — is directly applicable to LoveWork's decision ledger.

**The core problem:** LoveWork makes a recommendation. LJ acts on it (or doesn't). Months later, an outcome materialises (good job, bad job, short stint, promotion, regret). How does LoveWork learn from that sparse, delayed, noisy feedback?

Shilong's taxonomy suggests several approaches:

| Approach | LoveWork application |
|----------|----------------------|
| **Pseudo ground truth** | Treat explicit user actions (apply, skip, follow-up) as weak labels for scoring quality |
| **Internal signals** | Scoring confidence (tight fit/reach/flourish agreement vs wide variance) as a reliability signal |
| **Self-play** | Simulate alternative scoring configurations on historical data and compare predicted outcomes to actuals |
| **Weak signals from environment** | Silence (LJ doesn't engage with a category of roles) *is* a signal — formalise "persistent ignore" as negative feedback |

**This is the moat.** Any system can crawl and score jobs. What makes LoveWork durable is the *longitudinal judgment loop* that improves recommendations over time based on outcomes. The taxonomy gives you a framework to design this loop explicitly.

---

### The Blurred Boundary — Where You Already Are

> *"The boundary among model, harness, and artifact evolution becomes blurry."*

LoveWork's current architecture already lives in this blur:

- **Artifact → Harness:** The wiki pages produced by the pipeline become input to the persona simulator on the next run (context feedback)
- **Harness → Artifact:** Updated profile criteria change how artifacts (scores) are produced
- **Model ↔ Harness:** Switching from DeepSeek to a local model or Gemini changes what the harness can do

The taxonomy validates your instinct to build **LoveWork as a system** rather than a pipeline. The three levels are not separate concerns — they're three handles on the same evolving intelligence.

---

### The Three Questions — Applied to LoveWork

The taxonomy closes with three diagnostic questions. Here are LoveWork's answers:

| Question | LoveWork Answer |
|----------|-----------------|
| **What evolves?** | The persona model (profile layers), the scoring criteria (fit/reach/flourish/action weights), the source catalogue (what to crawl, how often), the decision ledger (longitudinal memory) — all harness-level. Artifacts (wiki pages, scores) are the byproduct, not the target. |
| **What feedback drives it?** | User decisions (apply/ignore → weak labels), outcome assessment (did it work out?), explicit corrections from LJ during REPL sessions, longitudinal patterns in the ledger. |
| **Where does the loop close?** | On the **person's life and career satisfaction**. Not on benchmarks, not on code quality, not on crawl coverage. This is both the hardest loop (sparse, delayed, human-judged feedback) and the most valuable to close well. The taxonomy calls this "the world" — the hardest environment, where self-evolving agents matter most. |

---

### Summary: What to Build Next

| Priority | What | Why (from taxonomy) |
|----------|------|---------------------|
| **1** | Formalise the **decision ledger as a learning signal**: score every past GO/MAYBE/FLAG/DROP against the outcome, compute a "scoring accuracy" metric over time | You already have the data; this turns artifact iteration into harness improvement |
| **2** | Build a **harness self-improvement pass**: after N recommendations, have LoveWork propose changes to its own scoring prompts, profile criteria, or source priorities based on observed patterns | This is the self-improvement loop that makes harness-level adaptation real |
| **3** | Create a **regression suite of historical decisions** — lock in known good scoring behaviour so harness changes don't regress quality | Shilong's implicit point about evaluation: if you can't measure whether the system got better, you can't claim it evolved |
| **4** | Prototype a **weak-signal feedback model**: persistent non-engagement with a role category → automated down-weight of that category | Turns silence into signal (Level 3 technique applied to Level 2 harness) |
| **5** | Explore **multi-expert routing across profiles**: as new profiles come online, route opportunities to the right persona automatically | Prepares for Phase 3 (multi-candidate), solves the routing bottleneck Shilong identifies |

---

> *"A better prompt is useful. A better memory is useful. A better tool is useful. A better model is useful. Still, the final value of self-evolution should be measured by whether it helps us build better things."*
>
> — Replace "build better things" with "live a better career life" and you have the LoveWork mission statement.
