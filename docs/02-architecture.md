# Chapter 02 — Architecture

> **Audience:** everyone; especially builders.
> **See also:** [`../lovework-agent/ARCHITECTURE.md`](../lovework-agent/ARCHITECTURE.md) for the deeper engine design (call sites, LLM client internals, DSPy wiring, sandbox).

## The pipeline in one diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         lovework-agent/                            │
│                                                                     │
│   ┌──────────┐    ┌──────────────┐    ┌──────────────┐              │
│   │ sources/ │───▶│ SmartCrawler │───▶│ JobRegistry  │              │
│   │  (8)     │    │  (LLM-guided)│    │  (SQLite/CSV)│              │
│   └────┬─────┘    └──────┬───────┘    └──────┬───────┘              │
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
│   │   reports/  ·  orgs/  ·  index.md  ·  sources.md │              │
│   └─────────────────────────────────────────────────┘              │
│                                                                     │
│   ┌─────────────────────────────────────────────────────────────┐    │
│   │  Two faces of the same process (D18):                       │    │
│   │   GET  /        → dashboard HTML (human)                    │    │
│   │   POST /mcp     → MCP JSON-RPC (agent, 9 tools)             │    │
│   └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

## The data flow

1. **Sources** build a list of orgs (or job adverts) to look at. Eight sources:
   research orgs, neolabs, HF startups, company pages, HN "Who is hiring?",
   HN /jobs, Gmail LinkedIn alerts, LinkedIn related-ads. See [chapter 04](04-sources.md).
2. **SmartCrawler** walks each org's careers page, LLM-guided: a first call
   decides where to crawl next; a second call extracts structured job listings
   when a page has them. Firecrawl handles JS-rendered pages; httpx is the
   fallback.
3. **JobRegistry** upserts every job into SQLite/CSV with lifecycle tracking:
   `new → still_open → long_lasting → disappeared`. At the end of each run,
   jobs from the sources that *did* run get marked disappeared if absent.
4. **JobMatcher** scores each surviving job against the principal's profile
   (0–10, GO/MAYBE/FLAG/DROP), with prior-contact context from `applications/`
   and Gmail. Pre-LLM hard-kills (work-auth, reapply cooldowns) drop obvious
   non-fits cheaply. A branching-possibilities bonus adds +1 when a role aligns
   with an explicit future direction. See [chapter 05](05-matcher.md).
5. **WikiStore** writes the findings: per-org history pages, a dated report per
   run, an index, and the sources reference. The wiki *is* the principal's
   memory; the report is the run's headline.

The whole flow is one importable function: `pipeline.run_pipeline(profile, role,
source, *, registry, llm, wiki, …)`. The CLI, the agent, the dashboard's MCP
`run_pipeline` tool, and the future FastAPI service all call it. Collaborators
are injectable for multi-tenant isolation (Phase 3).

## Module map

| Module | Purpose |
|--------|---------|
| `pipeline.py` | Core `run_pipeline()` — the importable orchestration. **Phase-3-ready.** |
| `main.py` | CLI wrapper (cron, argparse). Calls `run_pipeline`. |
| `agent_main.py` | Interactive REPL entrypoint. |
| `agent.py` | `LoveWorkAgent` — the ReAct loop over the 8 tools (pi-agent). |
| `tools.py` | The 8 tools wrapped as `pi_agent.AgentTool` (used by the REPL). |
| `mcp_server.py` | MCP JSON-RPC dispatcher — the 9 tools over `POST /mcp`. |
| `dashboard_server.py` | HTTP server: `GET /` HTML + `POST /mcp` → `mcp_server`. |
| `crawler.py` | `SmartCrawler` — LLM-guided page traversal; 2 of the 3 LLM call sites. |
| `matcher.py` | `JobMatcher` (legacy) + `JobMatcherDSPyAdapter`. The 3rd LLM call site. |
| `dspy_signatures.py` | Typed DSPy signatures: `CrawlDecision`, `ExtractJobs`, `MatchJob`. |
| `job_registry.py` | SQLite/CSV lifecycle tracking. |
| `history.py` | Prior-contact scanner: `applications/` + Gmail. |
| `wiki_store.py` | Markdown wiki output. |
| `cases.py` | Lead → case slug (`YYYYMMDD-Company-Role`) + case dir helpers. |
| `sandbox.py` | Sandboxed Python REPL (`run_python` tool) — the RLM pattern. |
| `llm_client.py` | OpenAI-compatible LLM client (transport layer). |
| `llm_runtime.py` | Facade over pi-agent's streaming. |
| `config.py` | Configuration + `load_profile_text` / `load_bio`. |
| `crosscheck.py` | Append-only prior-contact check across reports (no LLM, ~5s). |
| `incremental_crawl.py` | Bounded incremental sweep (cost-capped, ad-hoc runs). |
| `build_manual.py` | Regenerates `MANUAL.md` from live state. |
| `rescore.py` | Re-runs pre-LLM kills over historical wiki entries. |
| `gmail_accessor.py` | Gmail API helper (used by sources/gmail_lj_jobs + history). |
| `sources/` | The 8 data-source modules. |
| `tests/` | pytest suite (223+ tests). |

## The three LLM call sites

| # | Where | Purpose | Calls/run |
|---|-------|---------|-----------|
| 1 | `crawler._ask_decision()` | Decide where to crawl next | ~400 |
| 2 | `crawler._extract_jobs_from_page()` | Pull structured listings | ~200 |
| 3 | `matcher.match()` | Score job against profile | ~50 |

Plus the agent's ReAct loop (`agent.run`) when used interactively — one LLM
call per turn, capped at `max_turns=20`.

## Two interfaces, one engine

LoveWork is driven three ways (all calling the same `pipeline.run_pipeline`):

| Interface | How | Audience |
|-----------|-----|----------|
| **CLI** (`main.py`, `incremental_crawl.py`) | Shell, launchd/Hermes cron | The principal (via scheduled runs) |
| **Dashboard** (`GET /`) | Browser, refresh to update | The principal watching a crawl |
| **MCP** (`POST /mcp`) | JSON-RPC, 9 tools | Any MCP-speaking agent (Hermes, Claude Code, Codex, OpenCode) |

See [chapter 06](06-dashboard-mcp.md) for the dashboard+MCP merge.

## What's next

- [`03-profiles.md`](03-profiles.md) — what gets fed into the matcher.
- [`04-sources.md`](04-sources.md) — how orgs enter the pipeline.
- [`../lovework-agent/ARCHITECTURE.md`](../lovework-agent/ARCHITECTURE.md) — for the deep engine internals.
