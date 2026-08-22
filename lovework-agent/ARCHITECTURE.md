# Architecture

> Design document for the LoveWork personal job discovery agent.
> Read [`../README.md`](../README.md) first for the project overview.

This document covers:
1. Mission & ideology
2. High-level architecture
3. The LLM/agent call sites (where and why)
4. The 8 tools
5. The DSPy integration
6. The sandboxed Python REPL
7. Non-LLM services (Firecrawl, Gmail, filesystem)
8. Per-run cost
9. Future: pi-agent, RLM, hosting, consulting

---

## 1. Mission & Ideology (why this exists)

**LoveWork** is the mission. **LoveWork** is the product.

> *Work that you love, so you never work a day in your life.*

All humans are capable of this. Only they don't discover enough — or they run out of time.
We are on a mission to speed that up 1000×. Humans are built to do work they love.

We were not created by work, despite what hare-brained ideologues claim. And neither were
we created by fire, opposing thumbs, or speech and language — although all these things
are part of us. Humanity is created by **sexuality** — facilitating the greatest collaboration
ever, between the female and the male halves of our species.

However, humans are **destroyed by lack of meaningful work**. We can't just stare at the
wall or watch the grass grow. Unlike a cat that never tires of watching guard — we grow
bored. And we move on.

The personal Work agent is a small step in that direction.

---

## 2. High-level architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         lovework-agent/                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│   ┌──────────┐    ┌──────────────┐    ┌──────────────┐              │
│   │ sources/ │───▶│ SmartCrawler │───▶│ JobRegistry  │              │
│   └──────────┘    │  (LLM-guided) │    │  (SQLite)    │              │
│        │          └──────┬───────┘    └──────┬───────┘              │
│        │                 │                   │                      │
│        │                 ▼                   │                      │
│        │          ┌──────────────┐           │                      │
│        │          │  JobMatcher  │           │                      │
│        │          │ (LLM-scored) │           │                      │
│        │          └──────┬───────┘           │                      │
│        │                 │                   │                      │
│        ▼                 ▼                   ▼                      │
│   ┌─────────────────────────────────────────────────┐              │
│   │           WikiStore (markdown output)           │              │
│   └─────────────────────────────────────────────────┘              │
│                                                                     │
│   ┌─────────────────────────────────────────────────────────────┐    │
│   │  Agent layer (pi-agent + DSPy)                              │    │
│   │                                                              │    │
│   │  8 tools: crawl_org, match_profile, search_jobs,             │    │
│   │           check_history, fetch_url, update_wiki,             │    │
│   │           registry_stats, run_python                         │    │
│   │                                                              │    │
│   │  2 prompt implementations:                                   │    │
│   │    - legacy (LLMClient.structured + hand-written prompts)   │    │
│   │    - DSPy (typed signatures, compileable, optimisable)      │    │
│   │                                                              │    │
│   └─────────────────────────────────────────────────────────────┘    │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

Three LLM call sites in the **crawler/matcher pipeline**, plus one in the **agent** layer:

| # | Where | Purpose | Calls/run |
|---|-------|---------|-----------|
| 1 | `crawler._ask_decision()` | Decide where to crawl next | ~400 |
| 2 | `crawler._extract_jobs_from_page()` | Pull structured listings | ~200 |
| 3 | `matcher.match()` | Score job against profile | ~50 |
| 4 | `agent.run()` (ReAct loop) | LLM-driven tool orchestration | per-query |

Plus **non-LLM services**:

| # | Service | Where | What |
|---|---------|-------|------|
| A | **Firecrawl** | `crawler.fetch_page()` | JS-rendered markdown extraction |
| B | **Gmail API** | `history.scan_gmail()` | Prior correspondence with orgs |
| C | **Filesystem** | `history.scan_applications()` | `applications/` dir scan |

---

## 3. LLM client (transport layer)

**File:** `llm_client.py`

Single point of contact with the LLM provider. The whole agent goes through here.

### API call shape

```python
# Transport: HTTPS POST to LLM_BASE_URL/chat/completions
# Auth: Bearer token via LLM_API_KEY
# Default base URL: https://api.deepseek.com/v1
# Default model: deepseek-chat (DeepSeek V3-class; "V4-Flash" is target)

from openai import OpenAI
client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

resp = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "..."}],
    temperature=0.3,
    max_tokens=4096,           # 8192 for extraction
    response_format={"type": "json_object"},   # for structured()
)
content = resp.choices[0].message.content
```

### Public methods

```python
class LLMClient:
    def chat(messages, response_format=None, temperature=None, max_tokens=None) -> str
    def structured(messages, schema: Type[BaseModel], temperature=None, max_tokens=None) -> BaseModel
```

`structured()` is a thin wrapper that:
1. Appends the Pydantic JSON schema to the user message as a hint
2. Forces `response_format={"type": "json_object"}`
3. Strips markdown code fences
4. Validates with `schema.model_validate_json()`
5. Retries 3× with exponential backoff

### Why OpenAI-compatible?

Because it lets us swap providers without code changes:
- **DeepSeek** (default — cheap, fast, V3-class)
- **OpenAI** (o3, gpt-4o — when we need reasoning depth)
- **Anthropic** (via openai-anthropic proxy)
- **Ollama** (local models — privacy)
- **OpenRouter** (model routing)
- **ds4** (local Metal inference engine — see Future)

---

## 4. The LLM call sites in detail

### 4.1 `crawler._ask_decision()` — "where to crawl next?"

**File:** `crawler.py:325-371`

**What:** Given a page's markdown, decide if jobs are there, and which links to follow.

**Input:** org_name, url, content (markdown, 12K chars), goal
**Output:** `CrawlDecision(found_jobs, job_listings, next_urls, confidence, reasoning)`

**Called:** Once per page crawled. ~4 pages per org × ~100 orgs = **~400 calls/run**.

**DSPy equivalent:** `dspy_signatures.CrawlDecision` + `SmartCrawlerDSPy.decide_next`

---

### 4.2 `crawler._extract_jobs_from_page()` — "pull the listings"

**File:** `crawler.py:373-415`

**What:** Extract structured job listings from a page known to have jobs.

**Input:** Same as 4.1
**Output:** `List[ExtractedJob]` with title, team, location, url, snippets, etc.

**Called:** ~2 pages per org × ~100 orgs = **~200 calls/run**. `max_tokens=8192`.

**DSPy equivalent:** `dspy_signatures.ExtractJobs` + `SmartCrawlerDSPy.extract_jobs`

---

### 4.3 `matcher.match()` — "is this a fit?"

**File:** `matcher.py:74-115`

**What:** Score a job 0-10 against the principal's profile, with prior-contact and lifecycle context.

**Input:** profile, job_title, job_description, org_name, job_url, additional_context
**Output:** `MatchResult(score, decision, reasoning)`

**Called:** Once per surviving job. ~50 calls/run.

**Special logic:**
- **Auto-KILL** if same role + same org + applied within 6 months
- **Score lowered 1-2** for `long_lasting` jobs (open >30 days)
- **Context injected** into LLM prompt: registry status + prior contact summary

**DSPy equivalent:** `dspy_signatures.MatchJob` + `JobMatcherDSPyAdapter`

---

### 4.4 `agent.run()` — "do what the user asks"

**File:** `agent.py:122-148`

**What:** The ReAct loop. The LLM decides which tools to call, calls them, observes results, and iterates until it has an answer.

**Input:** A user message (string)
**Output:** The agent's final response (string)

**Called:** Once per interactive query. Each query may produce 1-10+ LLM calls (one per turn).

**Mechanism:**
- pi-agent's `agent_loop` runs the ReAct loop
- We pass a `stream_fn` that wraps our `LLMClient`
- We collect all events and return the last assistant text

**Safety:** `max_turns=20` cap (counts LLM calls, not streaming deltas).

---

## 5. The 8 tools

**File:** `tools.py`

Each tool is a thin wrapper around an existing module, exposed as a `pi_agent.AgentTool`.

| # | Tool | Wraps | Purpose |
|---|------|-------|---------|
| 1 | `crawl_org` | `SmartCrawler.crawl_org()` | Crawl a company site for jobs |
| 2 | `match_profile` | `JobMatcher.match()` | Score a job 0-10 |
| 3 | `search_jobs` | `JobRegistry` | Query the registry |
| 4 | `check_history` | `history.scan_history()` | Prior contact with an org |
| 5 | `fetch_url` | `crawler.fetch_page()` | Read any web page as markdown |
| 6 | `update_wiki` | `WikiStore.update_org_page()` | Record a finding |
| 7 | `registry_stats` | `JobRegistry.stats()` | Quick counts |
| 8 | `run_python` | `sandbox.run_python_tool_factory()` | Sandboxed Python REPL |

### Tool signature (per pi-agent 0.1.0)

```python
async def execute(
    tool_call_id: str,
    params: Mapping[str, Any],
    abort_event: asyncio.Event | None = None,
    on_update: AgentToolUpdateCallback | None = None,
) -> AgentToolResult
```

Each tool is registered with:
- `name` — short identifier
- `label` — human-readable
- `description` — what the LLM sees (used for tool selection)
- `parameters` — JSON schema for the params dict

### Agent's system prompt

`agent.py:_system_prompt()` builds the system prompt that tells the LLM:
- Who the principal is (profile)
- What tools are available
- The mission (LoveWork)
- The heuristics (UK-based, recent, no re-apply within 6 months)

---

## 6. The DSPy integration

**File:** `dspy_signatures.py`

### Why DSPy?

The hand-written prompts in `crawler.py` and `matcher.py` work but are hard to:
- **A/B test** — no way to compare two prompt versions
- **Optimise** — no automatic tuning against a metric
- **Port across models** — DeepSeek vs Claude vs GPT need different prompt phrasings
- **Compose** — can't easily chain signatures or share components

DSPy signatures unlock all of these. Future work: write a metric, gather a
labelled set, run a DSPy optimizer (BootstrapFewShot, MIPRO, GEPA).

### Signatures

| Signature | Replaces | Module |
|-----------|----------|--------|
| `CrawlDecision` | `crawler._ask_decision()` | `SmartCrawlerDSPy.decide_next` |
| `ExtractJobs` | `crawler._extract_jobs_from_page()` | `SmartCrawlerDSPy.extract_jobs` |
| `MatchJob` | `matcher.match()` | `JobMatcherDSPyAdapter.match` |

Each has:
- Typed input fields
- Typed output fields
- A docstring (the task description)
- Field-level descriptions

### Configuration

```python
from dspy_signatures import configure_dspy
configure_dspy()  # uses config.LLM_MODEL, LLM_API_KEY, LLM_BASE_URL
```

### Usage

```bash
# CLI
../venv/bin/python3 main.py --dspy --profile lj --role general

# Programmatic
agent = LoveWorkAgent.from_profile("lj", role="general", use_dspy=True)
```

### Verified consistent with legacy

| Test case | Legacy | DSPy |
|-----------|--------|------|
| AI Scientist @ Poetiq | 9.0 GO | 9.0 GO |
| Senior ML Engineer @ Hugging Face | 8.5 GO | 8.5 GO |
| Quant Researcher @ Citadel | 4.0 FLAG | 2.0 KILL |
| Frontend Developer @ Stripe | 0.0 KILL | 0.0 KILL |

DSPy is consistent. The Citadel case is stricter (LJ explicitly avoids pure
quant per the soul).

---

## 7. The sandboxed Python REPL (`run_python` tool)

**File:** `sandbox.py`

The LLM can write Python code in a sandboxed subprocess to explore data,
batch-process, or compose operations. This is the "RLM pattern" — instead of
fixed tools like `web_search(query)`, give the LLM a REPL.

### What the LLM can do

```python
# Access the registry
jobs = registry.all_jobs()
print(f"{len(jobs)} jobs in registry")

# Custom filters
long_jobs = [j for j in jobs if j.status == "long_lasting"]
for j in long_jobs:
    print(f"  {j.org}: {j.title} (open {j.age_days}d)")

# Persistent variables across calls
my_filter = lambda j: j.age_days > 30 and "research" in j.title.lower()
matches = [j for j in jobs if my_filter(j)]
```

### How it works

- Runs in a subprocess: `python -I -c <script>`
- venv's site-packages added to PYTHONPATH
- 30-second timeout
- Pre-imports: `config`, `JobRegistry`, `JobRecord`, `scan_history`, `PriorContact`, `WikiEntry`, `WikiStore`
- Top-level variables persist via JSON file in `cache/_sandbox_vars.json`

### Why this is powerful

Instead of:
- `web_search(string)` → dumb
- `find_in_files(string)` → dumb

The LLM can:
- Compose: filter + aggregate + transform in one call
- Iterate: refine a query based on intermediate results
- Branch: try multiple analyses in parallel
- Be creative: anything that can be written in Python

This is the "More power 1" lever from the original LLM_CALLS doc.

---

## 8. Non-LLM services

### 8.1 Firecrawl — web scraping

**File:** `crawler.py:38-86`

```python
from firecrawl import FirecrawlApp
app = FirecrawlApp(api_key=FIRECRAWL_API_KEY)
result = app.scrape(url, formats=["markdown"])
md = result.markdown
```

**Why not just httpx?** Many career pages render jobs client-side with JS. Firecrawl runs a real
headless browser, waits for content, and returns clean markdown. Fallback (`httpx`) only
works for server-rendered pages.

**Cost:** ~$0.001 per page. A full run = ~$0.50.

**Alternatives:** ds4-agent browser, camofox-browser, Firecrawl web agent.

### 8.2 Gmail API — prior correspondence

**File:** `history.py:scan_gmail()`

```python
gapi = Path.home() / ".hermes-macbook2/skills/productivity/google-workspace/scripts/google_api.py"
subprocess.run(["python3", str(gapi), "gmail", "search", query, "--max", "10"])
```

**Query:** `label:LJ-Jobs ("poetiq" OR "poetiq.ai" OR "poetiq ai")`

**Why:** Detect prior applications, rejections, interview invites. Feeds the re-apply rule.

### 8.3 Filesystem — applications/ history

**File:** `history.py:scan_applications()`

Walks `~/LJ-work-2026/applications/YYYYMMDD-Company-Role/` dirs.
Parses the dir name (date + company + role), reads the `.txt` file looking for rejection markers.

**Why:** Same — feeds the re-apply rule. Also surfaces in the wiki report as "Prior contact".

---

## 9. Per-run LLM cost

At DeepSeek pricing (~$0.14/M input, $0.28/M output), per full run:
- ~500 calls × ~2K input + ~500 output = ~1M input + ~250K output
- **~$0.15 per run**
- 3 runs/week = **~$0.45/week** = **~$2/month**

At OpenAI o3 pricing (~$10/M input, $40/M output):
- Same volume: ~$10 + ~$10 = **~$20 per run** = **~$240/month**

**Recommendation:** Default to DeepSeek for autonomous mode (cheap), use a stronger model
(Claude, o3) for interactive mode when the user is asking nuanced questions.

---

## 10. Call-site index

| File | Function | Calls | Cost/run |
|------|----------|-------|----------|
| `llm_client.py` | `chat()` | (wrapper) | — |
| `llm_client.py` | `structured()` | (wrapper) | — |
| `crawler.py` | `_ask_decision()` | openai.chat | ~400 |
| `crawler.py` | `_extract_jobs_from_page()` | openai.chat | ~200 |
| `matcher.py` | `match()` | openai.chat | ~50 |
| `agent.py` | `run()` (ReAct) | openai.chat | per-query |
| `dspy_signatures.py` | `*` | same as above, via dspy | same |
| `crawler.py` | `firecrawl.scrape()` | Firecrawl | ~600 pages |
| `history.py` | `_gapi_path` | `google_api.py gmail search` | Gmail | ~100 |
| `job_registry.py` | — | SQLite | local | free |
| `wiki_store.py` | — | fs write | local | free |
| `sandbox.py` | `execute()` | subprocess | per-call | free |

---

## 11. Future

### 11.1 Pi-agent: the same name, different project

There are two `pi-agent`-related projects we may encounter:

- **PyPI `pi-agent` 0.1.0** — Python reimplementation by Aniket Maurya. Alpha. This is what we use.
- **`~/LJ-AI-agents/pi-mono`** — TypeScript monorepo (Pi Agent Harness) by earendil-works. v0.79.8. Different project.

They share a name but are different codebases. The TS one is what OpenClaw/Hermes/etc. are built on. We could theoretically use Python bindings of the TS one, but for now the Python one gives us what we need.

### 11.2 RLM (Recursive Language Models)

Currently `run_python` is a single-pass subprocess. We could evolve it to a full
RLM pattern (see `~/LJ-RLM-memory/fast-rlm/`):
- LLM writes code that *recursively* calls sub-agents on chunks of context
- Sub-agent responses are returned as symbols/variables, not loaded into the parent's context
- This lets the LLM handle much longer contexts (full job descriptions, full org histories)

### 11.3 DSPy optimisation

The DSPy signatures are unoptimised. With a metric and a labelled set, we can:
- Run `BootstrapFewShot` to automatically find good few-shot examples
- Run `MIPRO` or `GEPA` to optimise the prompt instructions
- This would be the next big quality improvement

### 11.4 Hosting & consulting

If open-sourced:
- `ljubomir/lovework` on GitHub (or whatever name we settle on)
- `pip install lovework` on PyPI
- **Hosted service** — managed cron, hosted LLM proxy, dashboard, $20-50/mo per principal
- **Profile consulting** — help principals write their `soul.md` (the art is in the profile)
- **Custom sources** — LinkedIn API, Greenhouse webhooks, etc.
- **Application drafting** — auto-generate cover letters, tailored CVs (next step after "find")

---

## 12. See also

- [`../README.md`](../README.md) — project overview
- [`README.md`](README.md) — package-specific docs
- [`../profiles/example/`](../profiles/example/) — anonymised profile template
