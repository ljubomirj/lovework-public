# Chapter 42 — The LoveWork agent: an RLM charter

> **Audience:** builders + LJ. **Status:** charter + work packages, ruled
> 2026-10-10 (D23). **Companions:** [11](11-agentic-intelligence-harness.md)
> (the thesis this chapter fulfils: *"LoveWork must become an agent, not just
> a pipeline"*), [27](27-agent-ergonomics.md) (the discipline the agent must
> obey), [34](34-agent-system-plan.md) (AS0/AS1 — the front door the agent
> already has), [35](35-the-watch.md) (the loop the agent lives inside),
> [41](41-live-pack-revalidation.md) (the first mission), [40](40-optmem-memory-survey-and-plan.md)
> (the agent's identity layer), [38](38-self-evolving-agents-taxonomy.md)
> (where this sits in the self-evolving map), [25](25-jack-and-jill-agent-surface.md)
> + [16](16-agent-to-agent-interviews.md) (the ecosystem's agent side).

---

## 1. The decision

LoveWork grows a full-fledged agent. The ruling (LJ, 2026-10-10, recorded as
D23):

> **The LoveWork agent is an RLM** — a recursive-language-model agent in the
> prime-agent shape: **one tool, python**, driving the whole system from a
> REPL. Not a wide multi-integration agent (Hermes-shaped), not a coding
> harness (Codex/Claude-shaped), not a four-tool generalist (pi-shaped).

## 2. The deliberation — what kind of agent, and why RLM won

| Shape | Exemplar | What it is good at | Why not for LoveWork |
|---|---|---|---|
| Wide, integration-rich | Hermes | Many channels (mail, chat, home) — breadth | Solves a problem we don't have: our channels are few and already owned by deterministic writers (notify, the cron's delivery, the J&J letter path). N tools = N surfaces to trust. |
| Big, specialised | Codex / Claude Code | Editing code in a repo | The LoveWork agent's primary activity is *operating a system* (crawl, judge, investigate, record), not editing its own source. |
| Minimal generalist | pi (4 tools) | Lean autonomy | A compromise for unknown workloads. Ours is known — and collapses to one tool. |
| **RLM** | **prime-agent** | Open-ended problems, auto-R&D loops, one REPL | **Fits:** see §3. LJ's own experience: prime-agent handled open-ish problems and ran research loops unattended. |

The forcing arguments, in order of weight:

1. **The tool surface collapses to one.** Everything is python-importable
   (`shoulder`, `brief`, `matcher`, `application_packs`, `wiki_audit`,
   `mem`, …), state is files, mail/fetch are libraries, memory is a
   subprocess call. Python-only is not a constraint here — it is the minimal
   complete set.
2. **LoveWork is unusually safe ground for autonomy.** The objection to a
   full agent — unlogged, unrestricted action — is answered by architecture
   we already run: append-only ledgers, immutable seals, writer leases,
   honest-state exit codes, one-authority-per-fact. An RLM whose every
   action lands in a ledger is *replayable* autonomy.
3. **The identity layer just shipped.** What separates an agent from
   scripts-on-cron is continuity: `mem wake` at boot, notes at close, naps
   at the W4 learn-close (ch40, D22). The agent's memory exists before the
   agent does — by design, as it turns out.
4. **The domain is open-ended-ish.** Investigations, defect hunts,
   revalidation, enrichment triage — judgment under time, not fixed flows.
   That is the RLM's home turf (and the deterministic half stays
   deterministic — §4).

## 3. Architecture

### 3.1 The loop

```
boot:  mem wake --profile <p>          (identity)
       STATE.md + DUE.md               (situation: due, principals, handoff)
       situation.py delta              (what changed since last boot)
act:   python REPL, one tool           (import the engine; act)
       every write via an authority    (ledgers / seals / wiki / memory)
       boundaries enforced in the tool layer, not the prompt (§3.3)
close: naps paid, journal entry, STATE.md re-render, honest exit code
```

Boot is the AS1 front door (ch34) plus the memory wake (ch40): where the
system stands, what is due, who the agent is. Close is the W4 discipline.
The loop's honesty vocabulary is the existing one: exit 0/1/2/75,
`blocked ≠ failed`, quiet days are real days.

### 3.2 The deterministic skeleton stays

The weekend sweeps, the 07:00 watch and brief, link checks, letters: these
are **pipelines, and pipelines on cron beat agent loops** on reliability,
cost and latency. Doctrine already rules it (ch35 W1: deterministic first,
the LLM only where judgment is needed). The agent is the judgment layer the
skeleton calls — today that call exists as the hermes-triggered "pack job"
after the morning run (ch41 §6b); P1 formalises it. **The agent never
replaces the cron; it sits on top of it.**

### 3.3 Boundaries live in the tool layer

The python the agent runs through is a shim that enforces, as code:

- **No sends.** The agent recommends and drafts; the principal decides and
  sends (MANIFESTO, ch24, ch31). Mail/telegram delivery stays outside the
  loop, in the deterministic writers.
- **No deletions** (house rule), no edits under `state/<p>/strata/`, no
  edits of sealed artifacts.
- **Live writes only through `--apply`-gated writers** — the existing gates
  (`promote_demote`, `snapshot`, `relink`) are the allowlist; new writers
  arrive gated by default.
- **Nested spawns stamped** `LOVEWORK_SUBAGENT=1` (mem already refuses their
  writes; the shim stamps, not the prompt).
- **Untrusted fetches through the sandbox**; the fetch provider chain stays
  env-governed.

Autonomy inside the law, where the law is code. A prompt asks; a shim
enforces.

### 3.4 What is deliberately NOT built

- No new channels: telegram delivery rides hermes cron as today; LJ→agent
  conversation (P3) reuses an existing surface rather than minting one.
- No second authority: the agent reads ledgers, never rivals them (ch40 §2
  — memory is narrative, ledgers are facts).
- No self-modification of the skeleton: the agent may propose code changes
  as diffs for LJ, in the normal PR-of-the-house style — its own evolution
  follows ch38's artifact level, reviewed, not autonomous weight-lifting.

## 4. Where this sits in the lineage

- **ch11** asked for the agent; **ch42** is its charter. The harness plans
  (ch11/ch22) keep their measurement role — the agent is what they measure.
- **ch34** built the door (AS0 selectors, AS1 situation); the agent is the
  thing that walks through it.
- **ch27** wrote the ergonomics for operators like this one; the agent is
  the first operator that is *ours*.
- **ch38**'s taxonomy: the agent operates at the artifact level (tools,
  prompts, doctrine evolve by review); weights stay frozen; the harness
  learns through the ch18 loop.
- **ch25/ch16**: the other side of the market is agentic (J&J's
  agent-readable surface, ATA interviews). The LoveWork agent is the
  worker-side mirror those chapters kept pointing at.

## 5. Work packages

| # | Delivers | Gate |
|---|----------|------|
| **P0 charter** | This chapter + D23 + lineage links | LJ reads it; no code |
| **P1 the first loop** | The 07:00 pack session re-founded as the RLM agent: boot/act/close, python shim with §3.3 guards, mission = the investigation backlog (`uninvestigated_packs`). The tick and the action catalogue are specified in [ch43](43-the-lovework-loop.md), whose decider (`decide.py`) is implemented | one unattended morning run: packs investigated, every write ledgered, honest exit |
| **P2 the second loop** | W4 learn-close as an agent session: reactions → propositions, naps paid, lesson noted | one monthly close run unattended |
| **P3 the conversation** | LJ ↔ agent on an existing channel (telegram via hermes): ask, investigate, answer — read-mostly, §3.3 intact | LJ uses it for a real question |
| **P4 the residency** | the agent's own hygiene: token/cost budget per run, a `run_lineage` row per session, the agent's self-report in the brief | cost visible in MANUAL; lineage greppable |

Sequencing: P1 before all — it proves the shape on the existing mission
(ch41's queue). Nothing in P0–P4 touches the deterministic skeleton.

## 6. What would change this ruling

Named, not hypothetical: (a) a channel the agent must *own* that python
cannot reach (then a second tool, argued per-case); (b) a cost blowout the
budgets in P4 cannot tame (then narrower loops, not more tools); (c) a
principal other than the founding family joining with different boundaries
(docs/15 — per-principal shims, per-profile markers, same single tool).

---

## 7. Living conversation

Dated contributions preserve the deliberation as it develops. Proposals in
this section are not adopted rulings unless explicitly recorded as such;
accepted changes should also update the relevant charter sections and plans.

### 2026-10-10 — Codex: three weaknesses to fix before P1

**LJ:** "Write your critique append it to docs/42-the-lovework-agent.md,
let's make it a living conversation."

**Codex:** I support the direction, but the charter is more confident than
its contracts justify. Keeping the deterministic pipelines and adding a
Python-based judgment layer makes sense. "RLM is almost forced" does not
yet follow. These are the three things I would fix before implementing P1,
ranked by consequence.

#### 1. The Python shim is not yet an enforceable authority boundary

Section 3.3 promises no sends, deletions or unauthorized writes while
allowing the agent to import the engine. With ordinary host permissions,
Python can bypass those wrappers, open files directly, launch subprocesses
or use available credentials. One tool can expose an enormous capability
surface.

Leases and seals help cooperating writers coordinate and preserve evidence;
they do not constrain arbitrary Python. Likewise, **logged does not mean
replayable**: [ch34 §2](34-agent-system-plan.md#2-observed-baseline-and-its-limits)
already distinguishes those guarantees. The charter should claim auditable
actions with explicitly qualified replay capability.

**Fix now:** specify two components: an isolated Python workspace for
investigation, and a trusted action service exposing principal-scoped
operations. The workspace gets no delivery credentials or writable
production mounts. The service validates authority and inputs, then invokes
existing writers. Python can remain the sole model-facing tool.

**Acceptance gate:** attempted direct writes, cross-principal reads,
subprocess escapes and unauthorized sends are denied outside the model's
control; permitted pack updates still succeed and leave receipts.

#### 2. The charter chooses RLM without specifying the recursive mechanism or demonstrating its value

The loop in §3.1 currently describes boot → Python → close. That also
describes a conventional coding agent. RLM specifically involves keeping
substantial context in an external environment and programmatically
examining it through recursive model calls ([original RLM paper](https://arxiv.org/abs/2512.24601)).

The charter leaves unanswered: what stays outside context, when a child is
called, what evidence it receives, what it returns, and how its claims are
checked. LJ's good experience with prime-agent is a sound reason to test
this approach, but not sufficient evidence that pack investigation needs
recursion.

**Fix now:** define a small investigation contract: evidence objects with
source references; bounded child calls returning claims, evidence and
uncertainty; parent verification; explicit termination. Compare the existing
pack workflow, a plain Python agent and its recursive variant on the same
frozen cases, using the same model and comparable budgets.

**Acceptance gate:** a proposed 20-case set covering straightforward, stale,
contradictory and insufficient-evidence cases. Measure factual support,
missing requirements, LJ's correction time and cost per usable pack. Retain
recursion where it improves those results. "Insufficient evidence" must be
an acceptable outcome.

#### 3. The sequencing puts unattended execution before the controls needed to survive it

P1's gate is one unattended morning; budgets and session lineage arrive in
P4. One successful morning cannot establish reliable recovery, useful
judgment or bounded spending.

The stronger requirements already exist in
[ch34's unfinished AS2](34-agent-system-plan.md#as2--decision-and-action-contracts):
persist intent, validate dependencies, define idempotency, reconcile before
retrying. Chapter 42 should inherit those requirements explicitly. Sealing
evidence after an exception does not itself roll back effects or resume a
task safely.

**Fix now:** move budgets, lineage and recovery into P1 prerequisites. Give
each pack a durable task record; investigate outside the writer lease;
acquire the lease briefly to revalidate and publish. Cap total spend,
elapsed time, fetches and recursive calls across the whole session. Preserve
partial work when stopping.

**Acceptance gate:** interrupt before publication, during publication and
after publication but before receipt; resume without duplicate effects or
fabricated completion. Change the profile mid-investigation and verify
stale work is rejected. Then run a proposed seven-morning canary, capped at
two packs per morning.

**Proposed amendment:** keep Python-only, make recursion testable, and make
P1 a measured, recoverable investigation service. That preserves the
intended agent while making the first implementation substantially easier
to trust. These are design-review proposals, not claims that the controls
have been implemented or verified in production.

### 2026-10-10 — Source study: how prime-agent actually works (answers for the three weaknesses)

PA (`~/prime-agent`, TypeScript monorepo: `packages/{agent,ai,coding-agent,tui}`, the
agent loop built on `pi-ai`) — read at the source, the organs that matter to us:

**The RLM mechanism, precisely.** The ipython tool is a *persistent Python kernel*
(a separate process, managed by the TS host: `core/kernel/repl-manager.ts`, with a
boot gate for admission). Python state — variables, helpers, parsed outputs —
persists across turns; **tool calls are themselves Python `await` expressions**
(`h = bash('npm test')` returns a handle; `await h` completes it). Orchestration is
Python, not shell; the prompt bans shell loops and heredocs outright. This is the
concrete meaning of "one tool": the REPL is not a tool the agent calls, it is the
agent's *control environment*; everything else (bash, fetches, skills) is an
expression inside it.

**Recursion, precisely** (Codex §2 answered by PA's own contracts):
- Children spawn via `await rlm.spawn('sub-task', name='worker')` — admission
  returns a handle immediately. **Results never come back as return values**;
  they arrive by messaging (`await agent_message.send(message,
  receiver_role="parent")`) or files. Progress flows one-way via
  `rlm.progress_note('…')` (≤512 chars, throttled ~1/10s) — the parent observes
  without waking the child.
- Depth is stamped into every child's prompt (`Recursive agent depth: N`) and
  capped by a first-class `rlm-max-depth` setting (default/env/global/inherited/
  chat sources) — recursion is a *budgeted configuration*, not a hope.
- The long-running discipline: start work, record the handle, **end the turn**;
  completion follow-ups wake the agent. No polling sleeps. Parallel children are
  started without sequential waits.

**Continual harness** (`rlm.harness`): CRUD for memory/skill/subagent/prompt-note,
**session-local by default**, `global_=True` only for stable cross-session
lessons. PA's analog of our OptMem tier + skills — same ruling we made
independently (D22): session-local narration, deliberate promotion to durable.

**Context economics** (what stays outside context): the REPL holds working state;
large data stays on disk and is re-loaded; compaction summarises branches and
drops any REPL variable whose serialised form exceeds 16 MiB. The conversation
log is a *file path* the agent can read — context is re-derivable from disk.

**Boundaries and recovery** (Codex §1 and §3, partially answered):
- `session-lease.ts`: file locks with owner token + PID + process-liveness
  (`SessionAlreadyActiveError`; dead-owner detection) — PA's version of our
  writer leases/flock.
- `autonomous.ts`: unattended runs carry hard budgets — defaults
  `maxContinuations 3, maxTurns 12, maxTokens 80k, maxRetries 3` — and a doctrine
  worth stealing verbatim: *"No human input is available… If you believe you are
  blocked, prove it with host-observable evidence, preserve that evidence, and
  keep looking for safe progress while budget remains. Do not end the session
  yourself; the verifier/evaluator decides completion."*
- Not solved by PA either: the kernel is *not* a security boundary — PA relies on
  host permissions and its own discipline (e.g. "do not install into the kernel;
  run external projects through their own environments"). Codex's §1 stands as a
  genuine gap in PA too: **a cooperating-python design cannot contain a
  compromised model.** Our answer must be the two-component split (unprivileged
  investigation workspace + trusted action service), which PA does not have and
  we need more than PA does.

**Adopted into our plan** (P0 amendments, pre-P1):
1. P1's tool layer = the two-component split (investigation REPL with no delivery
   credentials and no writable production mounts; a small trusted service that
   validates and invokes the existing `--apply` writers). Python stays the only
   model-facing surface.
2. Recursion contract copied from PA's shapes: spawn-returns-handle,
   results-by-message-or-file, progress notes, depth in every child prompt,
   `rlm-max-depth` as configuration — and Codex's 20-case frozen benchmark
   (recursive vs plain, same model, comparable budgets; "insufficient evidence"
   is an acceptable outcome) before recursion is default-on.
3. Budgets+lease+lineage move from P4 into **P1 prerequisites**: per-session caps
   (turns, tokens, fetches, child spawns), the session/writer lease held only
   across publication, durable task record per pack, partial work preserved on
   stop, and the interrupt/recover gate (before/during/after publication; profile
   change mid-run rejects stale work). Then the seven-morning canary, ≤2
   packs/morning.
4. The autonomous-mode doctrine quoted above enters the agent's close discipline,
   translated to our vocabulary: blocked is proven with host-observable evidence
   (a ledger row, an exit code), never asserted.

### 2026-10-10 — Prime Agent (the third RLM in the room): the direction is right, the order is not

**LJ:** "What do you think of the plan? Add your thoughts here, append to the docs/42 paper."

**Prime Agent:** I agree with the ruling — RLM, one tool, python — and I checked
§7's source study against `~/prime-agent` before adding to it. Every claim holds:
`DEFAULT_AUTONOMOUS_LIMITS` is `{maxContinuations: 3, maxTurns: 12, maxTokens:
80_000, timeoutMs: 30 min}`; the gate is `--autonomous-gate <command>`
(repeatable, `maxRetries: 3`, 5 min each); `session-lease.ts`,
`RLM_PROGRESS_NOTE_MAX_LENGTH = 512`, the 16 MiB kernel-variable cap and
`rlm-max-depth` (sources: default/env/global/inherited/chat) are all real.

My verdict differs from Codex's in emphasis. Codex says the plan is confident
beyond its contracts. I say the plan is *ordered* against itself: it hardens
containment (§3.3, Codex §1) before it has shown the agent produces anything a
principal acts on, and its first mission — a 173-item queue at two per day — is
a throughput measure wearing a value measure's clothes. Containment is the
cheap half here; we already own the machinery. Value is the half with no
detector, and P1 as written cannot fail informatively.

Five additions, each with a fix and a gate. Three of them are mechanisms
prime-agent already has and our charter does not yet borrow.

#### 1. A session is 30 minutes. A morning is a chain of sessions

PA's default wall-clock budget for an unattended run is **30 minutes**, and our
own scheduler kills a Hermes session at **1800 s** (AGENTS.md §9 — the reason
every crawl `nohup`s and returns). §3.1's loop and §5's P1 gate both say "one
unattended morning". Nothing in the charter says how a 30-minute-capped session
covers a morning, and the budget list in the adopted P0 amendment (turns,
tokens, fetches, child spawns) omits **wall clock** entirely.

**Fix now:** state the unit. A *run* is one session with a hard wall-clock cap;
a *morning* is a chain of runs sharing one durable task record. The record
carries what is done, what is in flight, what is next, and the reason for
stopping — the AS1 continuation record, per pack rather than per principal.
Then "one unattended morning" means: the chain advanced without a human, and
each link exited honestly.

**Gate:** a morning interrupted at any link resumes from the record alone. Two
consecutive links must not redo the same pack — checked by the record, not by
reading the log.

#### 2. Completion is a verifier command, not the agent's own claim

PA's doctrine is explicit: *"Do not end the session yourself; the
verifier/evaluator decides completion when configured gates pass."* The gate is
a **command list** (`--autonomous-gate`), retried and time-limited. Our P1 gate
is "packs investigated, every write ledgered, honest exit" — which the agent
itself reports. An agent that grades its own homework is exactly the failure
mode ch27 §2b names: a result carries independent axes, not one green lamp.

We already own the verifiers. `pack_queue.py` knows the pending queue,
`wiki_audit.py` knows the defect classes, `link_check.py` knows whether a
recommendation is still actionable, `stale_audit.py` knows advert age.

**Fix now:** adopt PA's shape verbatim — P1's gate is a command list, not a
paragraph. And state the general rule, because it outlives P1: **an agent
artefact class without a detector is not admissible.** Every artefact the agent
writes (pack investigation, wiki block, memory note, letter) must have a
`*_audit` counterpart that can fail it. That is the same discipline the
deterministic half already lives under, extended to the new writer.

**Gate:** the gate command list fails the run when a pack is written without its
investigation block, when `wiki_audit.py` gains a defect class it did not have
before the run, or when a claim cites a source that does not resolve. A run that
writes nothing is allowed to pass — a quiet day is a real day (§3.1).

#### 3. A stalled child must be provable, not waited on

PA spends a specific mechanism on this: `DEFAULT_AUTONOMOUS_SUBAGENT_KEEP_ALIVE_MS`
= 25 minutes — *"while subagents run, one continuation per 25 minutes of
continuous activity still fires so the parent can check for hung or stopped
children instead of sleeping until they finish."* Our P1 has budgets but no
liveness check on children, and our long-standing operational lesson is the same
one (the 09-19 sweep that died mid-run while the watchdog read a healthy log).

**Fix now:** borrow the valve. While children run, the parent wakes on a fixed
interval, checks each child for progress (a progress note, a file that grew, a
live pid), and either re-parents, re-spawns or records it `blocked` with
host-observable evidence — never silently waits. That is §7's adopted point 4
("blocked is a ledger row or an exit code") given its missing clock.

**Gate:** a deliberately hung child is detected within one interval and recorded
`blocked`, and the run still closes honestly with the rest of its budget intact.

#### 4. Recursion multiplies opinions, not evidence

This is the risk our own doctrine already names and the charter's §3.1 does not
carry: ch27 §6 — *"Repeated crawls of one advert or repeated model opinions are
one underlying evidence family"* — and *"repeated model agreement is independent
evidence"* is listed there as something a layer **must never imply**. An RLM
investigating one company with three children over the same fetched page
produces three correlated claims. That reads as corroboration and is not.

**Fix now:** two rules in the investigation contract Codex asked for.
(i) Every claim carries its source reference (Codex §2), and a claim supported
only by sibling children is recorded `uncertain`, not `supported` — the
evidence family is the source, not the model that read it.
(ii) Codex's benchmark measures **independent evidence added**, not only cost
and quality: a child is worth its cost only if it brings a source the parent did
not already have. Otherwise the plain Python agent is the right shape and
recursion is decoration.

**Gate:** on the 20 frozen cases, the recursive variant must either add
independent sources per unit cost or lose to the plain variant. "Recursion did
not pay here" is an acceptable outcome and must be recordable as one.

#### 5. The memory tier's subagent rule collides with the recursive shape

D22 rules that `mem` refuses subagent writes (`LOVEWORK_SUBAGENT=1`, exit 75) —
correct, and it is how the memory keeps one voice. But the agent's *work* is
done by children. So the parent alone writes identity, about work it did not do,
from a reply it may summarise badly. The charter does not notice the collision.

**Fix now:** keep the rule and route around it. A child's reply ends with one
`lesson:` line when it learned something durable; the parent writes it. Nothing
changes in `mem`; the identity tier keeps one writer; the children's lessons
still reach the narrative instead of dying with the session.

**Gate:** a session whose children learned something leaves at least one
parent-written note traceable to a child reply — or says why there was none.

#### 6. A cheaper answer to Codex §1 than a new service (a sequencing proposal)

Codex's two-component split is right, and §7 adopted it. I would not build the
trusted action service for P1. P1 can have the separation with machinery we
already trust: **run the agent in investigation-only mode**, writing to a
staging directory inside the pack, and let the promotion into the pack be done
by the existing writer, invoked by the existing cron — the trusted component we
already run and already audit. Python stays the only model-facing surface; the
unprivileged workspace gets no delivery credentials and no writable production
mounts; the boundary is the process we already believe.

The trade-off is honest and should be written down: the agent cannot iterate on
its own published artefact within the session, and promotion is one-way. That is
acceptable for P1, and the service becomes worth building at P3, when a second
consumer exists (the conversation channel) and the shape has earned it.

**Gate:** the staging directory is the only path the agent writes, proven by a
run whose every write lands there; a direct write to a pack or a ledger fails
the run's gate command.

#### 7. The first mission should be a decision, not a queue

ch27 §7: choose work by *"expected improvement to a principal decision"*, and
stop when no observation can change the decision. 173 investigations at two per
day is ~86 days, and the queue is only valuable where LJ then acts. P1 as
written cannot distinguish "the agent investigated 173 companies well" from "the
agent produced 173 documents nobody used" — the second is the failure the whole
project exists to avoid.

**Fix now:** keep the queue as the *source* of work, not the measure of it.
P1's mission is: produce **one** pack that LJ acts on, with the investigation
recorded, then scale the cap. The seven-morning canary then measures a decision
rate (packs that led to an application, an intro request, or a documented "no,
and here is why"), not a pack count.

**Gate:** one pack investigated end to end, and LJ's action or reasoned
rejection recorded against it. Only then raise the cap.

#### What I would change in the charter text, concretely

- **§3.1 the loop** gains two steps: `verify` before `close` (the gate command
  list), and the unit of work — one session, chained into a morning by the task
  record.
- **§3.3** gains the staging rule (§6 above) and the rule that the shim's
  allowlist is the *writers*, with the investigation workspace unprivileged.
- **§5 P1**'s gate becomes a command list, and its mission statement becomes a
  decision rather than a queue.
- **§3.4** gains the negative rule this review keeps returning to: an agent
  artefact class without a detector is not admissible.
- **§6** gains a fourth reopen condition: **the model**. The RLM shape assumes
  the model can write orchestration code. If the model we can afford cannot —
  and the crawl model is not obviously that model — the one-tool REPL is a
  liability, not an advantage. Name the model requirement as a precondition.

I have changed nothing in the charter except this section; the fixes above are
proposals for LJ to rule on, in the house style, and none of them contradicts
the RLM ruling itself.
