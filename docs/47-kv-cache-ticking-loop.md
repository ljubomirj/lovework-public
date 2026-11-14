# Chapter 47 — The KV-cache-safe ticking loop

> **Audience:** builders + LJ. **Status:** design only — no production code
> changes. This chapter states the prefix-cache invariant, lists what breaks
> it in each loop today (code-cited, no speculation), gives the ticking-loop
> shape that preserves it, and defines the hit-rate metric with the read
> path on the local runtime.
> **Companions:** [43](43-the-lovework-loop.md) (the tick this constrains),
> [35](35-the-watch.md) (brief loops), [30](30-inference-chain-and-data-map.md)
> (measured local throughput), [42](42-the-lovework-agent.md) (the RLM charter).

---

## 1. Why this chapter exists

> **Superseded for the model choice (2026-10-21, docs/43 §14).** The loop now calls the
> OpenCode Go relay (`opencode-go` / `deepseek-v4.1-flash`, reasoning max) instead of a
> local 8081 server; the KV-cache rationale below is kept as the design record of *why a
> local server was attractive*, not as current configuration.

A local LLM served LoveWork from host-a (`192.0.2.10:8081`, Strata,
`qwen3.8-flash-next-iq3_xxs`, 256K context — ch43 §7). Every tick call
re-sends a prompt whose first ~12K tokens largely repeat the previous call.
Measured on the same box (ch30 §9): prefill ≈ 1,791–2,235 tok/s, a median
scoring call spending **6.3 s** mostly in prefill, and a full LJ crawl
projecting to **≈6.2 h of GPU time** (prefill 190 min + decode 183 min).
Prefill of an unchanged prefix is pure waste: the K,V for those positions
already exist. A ticking loop that rebuilds its prompt bytes on every tick
pays the 6-second prefill every time; a loop that keeps its prefix pays it
once per session. That is the whole prize, and the whole discipline below.

---

## 2. Background sources

**Hermes, three passages.** (a) The Gupta read-through of the Hermes source:
the prompt builder keeps "the stable prefix stable for as long as possible";
`MEMORY.md`/`USER.md` are loaded at session start and the snapshot is then
**frozen** — mid-session writes hit disk but do not mutate the built prompt
until a rebuild; Honcho recall on later turns is attached to the current
user turn at call time only, so the stable prefix never moves. (b) The
Buzovskyi 5-systems note names the three cache-breakers explicitly:
**switching models mid-session, changing memory files, changing context
files** — and notes cron runs in fresh isolated sessions with no history.
(c) The Claude Code lessons (Trq212/Lance Martin): caching is **prefix
match** — any change at position N invalidates everything after it; order
static-first, dynamic-last; pass updates (dates, mode changes) as message
content, never by rewriting the system prompt; never add/remove tools or
switch models mid-session; compaction must be a **cache-safe fork** (same
system prompt, tools, history, compaction prompt appended as a new user
message at the end).

**LJ's own long-context evidence.** `~/logBook` (LJ, 2026-07-30): a job
whose system context was "30K-50K cached and unchanged after the 1st use"
ran flat at ~40 tok/s on hipfire while every other 7900 XTX engine croaked
on the first step. Unchanged prefix + a server that honours it is the
difference between a flat curve and no curve.

**ChEMBLdb-query material: absent on this host, stated plainly.** The brief
named a `~/applications/` chembl tree and logBook Hermes-strictness notes on
context immutability. Searched 2026-10-12: no `*chembl*` path exists under
`~/applications/` (only career-ops outputs match the name pattern search),
and `grep` over `~/applications/logBook`, `lovework/JOURNAL.md` and
`lovework/LEARNINGS.md` for `context-immutab|prefix.*preserv|frozen prefix|
append-only transcript` returns nothing. So this chapter cites **no**
ChEMBL passage and invents none — the "frozen large context" grounding is
the hipfire passage above plus ch30's measured prefill/decode table.

---

## 3. The invariant

For one KV session (one model, one server-side context slot), let the
request be the ordered byte sequence the server tokenizes:

`MODEL ‖ SYSTEM ‖ TOOLS ‖ HISTORY ‖ CURRENT`

**Invariant:** bytes `[0, K)` must be token-identical across consecutive
calls for the server to reuse K,V for positions `[0, K)`. Consequences,
all mechanical:

1. A one-byte change at position N (a date, a counter, a reordered tool,
   a reworded sentence) keeps the hit only for `[0, N)` and recomputes
   everything after. There is no "small edit" — only "early edit" (fatal)
   and "late edit" (cheap).
2. Sampling/decoding parameters (`temperature`, `max_tokens`,
   `response_format`, `reasoning_effort`) are **not** prompt bytes, but
   whether they join the server's cache key is server-defined — see §6.3.
   The design pins them per action type regardless, so a parameter wobble
   can never be the explanation for a miss.
3. The model id is position zero: switching models (or the server swapping
   weights under one name — `hipfire serve` holds one model at a time,
   ch30 §9) resets K to 0 for every session.
4. Only the **common prefix across calls in one session** earns anything.
   Two calls sharing 11K of 12K tokens save ~5.5 s each at measured
   prefill rates; two calls sharing nothing save nothing however frozen
   each one is internally.

---

## 4. What breaks it in each loop today

Every citation is a file:line in `lovework-agent/` as of 2026-10-12.
"Breaks" means: moves, or re-emits with different bytes, something before
the dynamic tail — or makes a shared prefix impossible by construction.

### L1 — matcher `match()` (`matcher.py`) — cold by construction

- The whole profile text opens the user prompt (`matcher.py:1084-1086`:
  `f"""You are a personal talent agent. … {self.profile} …`), followed by
  org/title/location/description and then `Additional context: {context}`
  (`matcher.py:1088-1095`). Profile bytes sit at the **top**, so any
  profile edit re-keys every later byte.
- The profile itself is live-read and concatenated from six files with
  `"\n\n---\n\n"` joins (`config.py:238-296`); `_profile_file`
  (`config.py:219-235`) falls back to the live file outside an operation.
  Editing `soul.md` between two ticks changes byte ~200 of the next call.
- `_build_context` (`matcher.py:269-312`) injects per-advert dynamic text
  mid-prompt: registry status with `first seen … last seen … age N days`
  (`matcher.py:288-290`), prior-contact summary (`matcher.py:295-300`).
  The staleness ages derive from `date.today()` (`matcher.py:387,404`) —
  a **date injection above the advert tail**, so the same advert
  re-scored tomorrow shares no prefix past the context block.
- `LLMClient.structured` appends the Pydantic JSON schema to the last user
  message (`llm_client.py:349-357`). The schema bytes differ per call
  type (MatchResult vs CrawlDecision vs Investigation), so even identical
  prompts diverge at the suffix by call type — correct as far as it goes
  (suffix, not prefix), but it means prefix-sharing is only ever
  **within** one call type.
- Verdict: per-advert prompts are unique strings. No cross-advert prefix
  exists to hit. The cache play here is template freezing + profile
  pinning (§5), which converts "every call recomputes 12K tokens" into
  "same-shape calls share the template head" — worth little while each
  call carries a different advert, worth everything once batching or
  sessioning groups same-type calls.

### L2 — crawler decision/extract (`crawler.py`) — same class, one extra

- Decision prompt interpolates goal/org/url/page (`crawler.py:478-494`),
  extract prompt ditto (`crawler.py:525-545`); page content is truncated
  at a fixed char offset (`crawler.py:411`, `content[:12000]`). A fixed
  *character* offset is not a fixed *token* boundary: two pages of
  different lengths put the truncation seam — and hence every byte after
  the shorter page's content — at different token positions. Shared
  prefix ends where the page bytes diverge, i.e. almost immediately.
- Same verdict as L1: freeze the template (§5) so the only variance is
  the page itself, placed last.

### L3 — tick `investigate_pack` (`tick.py`) — closest to safe, two faults

- Good: fixed preamble, fixed evidence order (advert, then site root),
  fixed rule list (`tick.py:647-669`); `run_id` timestamp is transcript
  label only (`tick.py:603-604`), never prompt bytes; the rendered date
  (`tick.py:678`) is post-call. These are the pattern §5 copies.
- Fault 1: the preamble hardcodes the principal —
  `"the principal is LJ, …"` (`tick.py:650-651`). vj/kj ticks diverge at
  byte ~60, so the three principals can never share a session prefix. A
  `{principal}` parameter in the same position (frozen per session, not
  per binary) fixes it without moving anything.
- Fault 2: org/role/URL facts are interpolated **before** the evidence
  (`tick.py:652`), so per-pack bytes start early. Correct placement is
  after the frozen frame, adjacent to the evidence they describe.

### L4 — shoulder brief (`brief.py`) — ordering already right, tail unbounded

- Good: `_PROMPT` is a frozen template (`brief.py:119-135`); org-memory
  context is appended **at the end** (`brief.py:312-315`) — exactly the
  static-first rule; the brief timestamp `at` stays in the receipt/file,
  never in the prompt (`brief.py:329-334`); candidate lines use
  `json.dumps(…, sort_keys=True)` (`brief.py:203-216`) — deterministic
  byte order, the right call.
- Fault: `{principal}` is in the first line (`brief.py:119`), and the
  event-detail JSON is unbounded mid-prompt (capped only by
  `MAX_CANDIDATES=12`, `brief.py:54`). Per-principal sessions plus a
  char-cap on the detail block complete the discipline.

### L5 — reactions / reflect / consolidate — small, leave alone

- Classify prompt embeds frozen vocab lists + verbatim tail
  (`reactions.py:237-243`); single-message nap calls (`consolidate.py:102`).
  Spend is trivial (cents/day); any change here risks the loops above for
  no measurable gain. Frozen by policy, not by code.

### L6 — interactive agent loop (`agent.py` + `llm_runtime.py`) — the real risk

- System prompt rebuilt from **live** profile files on every construction
  (`agent.py:126` → `config.py:238-296`): no pin, no hash, no freeze.
- Tool list has a conditional append (`tools.py:423-424`: `run_python`
  iff profile+role) — a toolset that differs by call path is a prefix
  that differs by call path (the Claude Code "never add/remove tools"
  rule).
- Multi-turn history + tool-result serialization live in pi-agent, outside
  this repo (`llm_runtime.py:140-187` is the boundary). Whether tool
  results are appended verbatim, re-wrapped, or reordered is unverified
  here — but every call's exact messages are recorded
  (`llm_client.py:317-336`), so divergence is auditable post-hoc from
  `model_calls.jsonl` today. §6.3 lists the probe.

**What is already safe (do not touch):** brief `at` outside the prompt;
tick `run_id`/render date outside the prompt; crawler date parsing
(`crawler.py:303-304`) is filtering, not prompt bytes; `decide.py:376`
`at` is payload, not prompt.

---

## 5. The ticking-loop shape that preserves it

Five rules. Each is checkable from `model_calls.jsonl` without new
instrumentation.

### 5.1 Frozen prefix layout (byte order, fixed)

One session = one (principal, action type, model, toolset). Every request
in it is laid out:

```
[1] SYSTEM: frozen boilerplate for the action type (role, rules, JSON
    discipline). Versioned: edits bump PROMPT_VERSION in the receipt,
    never silently.
[2] PROFILE: principal block, loaded once per loop process, sha256
    recorded in the receipt. Profile edits take effect at next process
    start, never mid-run.
[3] TOOLS: fixed set, fixed order, for the whole loop. No conditional
    append; new tools arrive as new action types (new sessions).
[4] TASK FRAME: fixed per-action-type template with fixed field order
    (org → role → url → evidence, always in that order).
[5] DYNAMIC TAIL (the only part allowed to vary): registry ages,
    fetched page text, event JSON, memory-wake blocks, schema hint.
    Append-only: new facts extend the tail, never splice the head.
```

`structured()`'s schema hint stays exactly where it is (end of last user
message) but is generated from a **frozen schema string per action type**
— serialize once per process, reuse the bytes.

### 5.2 Append-only transcript discipline

- Within a session, messages are appended and never edited, reordered, or
  re-rendered. Retries resend **identical bytes** (today's retry already
  does — `llm_client.py:255-313` reuses `request_kwargs` — keep it so).
- One action per tick (ch43 §5) already bounds sessions: each tick extends
  its session by at most one exchange, so prefix grows monotonically and
  hits compound through the day.
- Compaction (when 256K fills) is a cache-safe fork per the Claude Code
  rule: same system, same tools, same history, compaction prompt appended
  as a new user message — never a differently-shaped side call.

### 5.3 Where timestamps and counters may live

- In the **dynamic tail only**, on a trailing `as-of` line
  (`as of <UTC iso> · registry age <N>d · envelope <calls>/<mins>`).
  Same-day reruns then share everything above the line.
- Never in the system prompt, the profile block, the tool list, or the
  task frame. `LOVEWORK_OUTER_RUN_ID`, envelope counts, cursor positions
  stay in receipts/ledger rows (as today) — addressable, joinable, and
  costing zero cache.
- Counters that must be model-visible (e.g. "pack 3 of 9") go in the
  current user message's tail, phrased so completion of one item does not
  rewrite the next item's prefix (absolute references, never "the above").

### 5.4 Session topology

- Separate sessions for lj/vj/kj (their profile blocks differ at byte
  ~200 — sharing is impossible, so don't try; today's launcher already
  runs one process per principal).
- Separate sessions per action type (match vs extract vs investigate vs
  brief — the schema-hint suffixes differ, §4 L1).
- One model per loop (was `LOVEWORK_LOCAL_LLM_MODEL`; retired 2026-10-21 — the loop
  now uses the relay's `deepseek-v4.1-flash`, configured once in `config`), pinned for the day;
  a model change starts new sessions, never continues old ones.
- Sampling params pinned per action type (today's values already differ
  by site — brief 0.2, reactions 0.0, tick default effort — keep the
  values, freeze them in one table so a tuning edit is a deliberate
  version bump).

### 5.5 Rollout (design only — no code in this chapter)

1. Freeze templates + pin params (mechanical, receipt-asserted).
2. Profile pinning: hash at process start, refuse mid-run swaps.
3. `{principal}` parameterization of the tick preamble (L3 fault 1).
4. Detail-block caps in the brief tail (L4 fault 2).
5. Hit-rate detector in `tick.py --audit` style (ch43 §9 precedent: an
   artefact class without a detector is not admissible) — §6.2.

---

## 6. Measurement plan

### 6.1 The metric

Per window (one tick, one day-principal, one session):

```
prefix_hit_rate = Σ cached_prompt_tokens / Σ prompt_tokens
```

with two cost witnesses reported alongside:

```
prefill_s_per_call      = prefill_ms / 1000  (the avoidable part)
decode_tok_s            = completion_tokens / decode_s
```

plus a client-side reuse rate needing no server support:

```
template_reuse_rate = fraction of consecutive same-session calls whose
  canonical requests (model_transcripts.canonical_request) share a
  ≥4,096-byte common prefix with the previous call
```

`prefix_hit_rate` is the acceptance metric; the witnesses price a miss
in seconds; `template_reuse_rate` is computable today from
`model_calls.jsonl` digests (`request_digest`, `model_transcripts.py:103`)
and must track the server number — divergence means the client model of
the session is wrong, which is itself a defect.

### 6.2 How to read it from the local runtime

1. **Server usage fields (primary).** Probe once:
   `curl 192.0.2.10:8081/v1/models` to confirm the serving model id,
   then one chat completion with `stream_options.include_usage` (or the
   non-stream `usage` object) and inspect
   `usage.prompt_tokens_details.cached_tokens`. If present, per-call
   cached/total tokens land in the transcript ledger — the accounting
   slice is extending `record_call` with optional usage kwargs, so this
   chapter defines the fields, that slice stores them, no overlap.
2. **Server timings (independent witness).** Same probe: prefill_ms per
   call. A frozen-prefix second call must show prefill collapse (order of
   6 s → <1 s at ch30 rates); if usage says "hit" and timings say
   otherwise, trust timings and file the defect against the server
   reading.
3. **Client digest analysis (available today).** Group a day's
   `state/<p>/loop/transcripts/model_calls.jsonl` by session, compute
   longest-common-prefix of consecutive canonical `messages` bytes.
   Today's expectation: tick sessions score low (per-pack evidence
   early — §4 L3 fault 2 documents why); post-§5.5 sessions must clear
   **0.9** same-type consecutive reuse. The detector asserts this the
   way `--audit` asserts receipt truth.

### 6.3 Explicitly unverified (probe before trusting)

- Whether Strata/hipfire exposes `cached_tokens` at all, and under which
  usage flag.
- Whether `temperature`/`max_tokens`/`response_format`/
  `reasoning_effort` join the cache key (design assumes worst case and
  pins them anyway).
- Tokenizer identity between client bytes and server tokens (client
  measures bytes, server measures tokens — the 4,096-byte bar is the
  honest bridge, not an equality claim).
- pi-agent's tool-result serialization order (L6) — readable by diffing
  consecutive recorded `messages` in `model_calls.jsonl`.

### 6.4 Cost of being wrong

At ch30's measured 1,981 tok/s prefill, every unnecessary 12K-token
recompute costs ~6 s of GPU wall clock — the measured scoring-call
latency (6.3 s) is nearly all of it. Seventeen acted ticks a day per
principal (ch43 §7 envelope math) at one cold 12K prefill each is ~100 s
of avoidable prefill daily per principal today, scaling linearly with
tick rate and prompt size. The hourly cadence (ch43 §7: 24 runs covering
all three principals) is what makes the prefix *worth* keeping: the same
session is revisited every hour, so a preserved prefix pays off 17× a
day and a thrashed one charges 17×.

---

## 7. Non-goals and open questions

- Cross-advert prefix sharing in the matcher (L1): adverts differ, so
  the head to share is the template, not the content. Batching same-type
  calls into one session is future work, not promised here.
- GPU-sharing with LJ's interactive daemon (ch30 §9 caveat: 99% VRAM
  held) is out of scope — this chapter assumes the tick's session
  survives between ticks; if the server evicts it, `prefix_hit_rate`
  will say so and §6.2 step 2 is the witness.
- The ChEMBLdb-query grounding stays open: if that tree surfaces, its
  frozen-context passages should be cited here the way the hipfire
  passage is in §2.
