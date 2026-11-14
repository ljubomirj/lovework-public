# Chapter 02 — Architecture

> **Audience:** everyone; especially builders.
> **See also:** [`../lovework-agent/ARCHITECTURE.md`](../lovework-agent/ARCHITECTURE.md) for the deeper engine design (call sites, LLM client internals, DSPy wiring, sandbox).

## The pipeline in one diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         lovework-agent/                             │
│                                                                     │
│   ┌──────────┐    ┌──────────────┐    ┌──────────────┐              │
│   │ sources/ │───▶│ SmartCrawler │───▶│ JobRegistry  │              │
│   │  (13+1)  │    │  (LLM-guided)│    │  (SQLite/CSV)│              │
│   └────┬─────┘    └──────┬───────┘    └──────┬───────┘              │
│        │                 │                   │                      │
│        │                 ▼                   │                      │
│        │          ┌──────────────┐           │                      │
│        │          │  JobMatcher  │           │                      │
│        │          │ (LLM-scored) │           │                      │
│        │          └──────┬───────┘           │                      │
│        │                 │                   │                      │
│        ▼                 ▼                   ▼                      │
│   ┌─────────────────────────────────────────────────┐               │
│   │           WikiStore (markdown output)           │               │
│   │   reports/  ·  orgs/  ·  index.md  ·  sources.md │              │
│   └─────────────────────────────────────────────────┘               │
│                                                                     │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │  Cross-principal source ownership (2026-09-12):             │   │
│   │   one owner crawls a shared source → live corpus            │   │
│   │   (registry + cached advert evidence, read-only)            │   │
│   │        │                                                    │   │
│   │        └─▶ consume.py → per-principal scoring → that        │   │
│   │            principal's registry / wiki / ledger             │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │  Two faces of the same process (D18):                       │   │
│   │   GET  /        → dashboard HTML (human)                    │   │
│   │   POST /mcp     → MCP JSON-RPC (agent, 9 tools)             │   │
│   └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

## The data flow

1. **Sources** build a list of orgs (or job adverts) to look at. Twelve sources
   exist in total — research orgs, neolabs, neoclouds, HF startups, company
   pages, HN "Who is hiring?", HN /jobs, Gmail alerts, LinkedIn related-ads,
   Harnham, Digital Jobs, Jack & Jill — but each principal runs only its own
   subset (`pipeline.sources_for_profile`). See [chapter 04](04-sources.md).
2. **SmartCrawler** walks each org's careers page, LLM-guided: a first call
   decides where to crawl next; a second call extracts structured job listings
   when a page has them. Firecrawl handles JS-rendered pages; httpx is the
   fallback.
3. **JobRegistry** upserts every job into SQLite/CSV with lifecycle tracking:
   `new → still_open → long_lasting → disappeared`, plus
   **`not_listed_in_scope`** when the advert was absent from a source that
   enumerated its *whole* scope this run (§WP3b). Absence is never closure: a
   capped feed, a failed fetch or a changed query cannot produce even the
   qualified label, and a re-listing revives the advert. The registry also
   separates the three observation facts — **first mention**, **last listing**
   and **last verified primary openness** (`last_primary_seen`): only the hiring
   party's own publication (careers page, employer's post, employer-side feed)
   verifies openness, while a board or alert box merely relays it. The tier is
   **declared by each source class** (`primary`) and the registry stays
   tier-agnostic — one authority, audited by `tests/test_source_tiers.py`, so a
   new source cannot be classified in one place and missed in another. The
   cross-principal consume path **carries** the owner's `last_primary_seen`
   rather than re-dating it: the consumer never observed the primary page. Failed/partial
   traversals and cached results still require the qualified coverage model in
   [chapter 26 §5](26-asof-strata-history.md#5-observation-history-coverage-and-liveness).
4. **JobMatcher** scores each surviving job against the principal's profile
   (0–10, GO/MAYBE/FLAG/DROP), with prior-contact context from `applications/`
   and Gmail. Pre-LLM hard-kills (work-auth, reapply cooldowns) drop obvious
   non-fits cheaply. A branching-possibilities bonus adds +1 when a role aligns
   with an explicit future direction. See [chapter 05](05-matcher.md).
5. **WikiStore** writes the findings: per-org history pages, a dated report per
   run, an index, and the sources reference. The wiki *is* the principal's
   readable working memory; the report is the run's headline. Neither is
   currently an immutable evidence archive.
6. **Shared sources are fetched once.** A principal may delegate a source to
   another principal's crawl (`consumes:` in `profiles/<p>/sources.yaml`); the
   source then leaves its own crawl set and the owner becomes its only crawler.
7. **`consume.py` ingests the owner's corpus** for the delegated sources:
   the owner's registry supplies the adverts and its cached advert evidence
   supplies the text, while scoring, registry rows, wiki pages, report and
   dataset ledger stay with the consuming principal (`mode=consume`).
8. **Evidence is preserved behind every run (live since 2026-09-17/20).** The
   wrapper seals each crawl through `crawl_operation.py` → `run_operation`:
   D20's strata writer pins the start inputs and captures the state into
   `state/<p>/strata/<run_id>/`, and the AsOf readers (`asof.py`, `status.py`)
   read sealed evidence. The other entry points (direct `main.py`, consume,
   reassessment) and per-source coverage are still open — see the boundary
   section at the end of this chapter.
   [Chapter 26](26-asof-strata-history.md) specifies pinned start inputs,
   response-time capture, a shared writer protocol, and sealed per-run trees
   hard-linked only to previous sealed trees. File snapshots preserve state;
   typed observations supply time and liveness semantics. The shared system
   tower is evidence → identity/timeline → temporal views → judgment → action
   → evaluation, with new observations recorded back at the base. The operator
   contract is [chapter 27](27-agent-ergonomics.md).

The whole flow is one importable function: `pipeline.run_pipeline(profile, role,
source, *, registry, llm, wiki, …)`. The CLI, the agent, the dashboard's MCP
`run_pipeline` tool, and the future FastAPI service all call it. Collaborators
are injectable for multi-tenant isolation (Phase 3).

## Cross-principal source ownership (2026-09-12)

Fetching a career page is principal-neutral work; scoring it is not. Fetching is
therefore shareable, and a principal may declare that some sources are crawled
**on its behalf** by another principal — the *owner*:

```yaml
# profiles/kj/sources.yaml
consumes:
  - source: neolabs
    owner: lj
```

Three pieces implement it:

- `pipeline.load_source_policy()` / `consumed_sources()` read the declaration.
  `sources_for_profile()` subtracts delegated sources, so the owner is their
  only crawler. A principal with no file behaves exactly as before, and a row
  naming the principal itself as owner is refused (it would drop the source from
  its own crawl and then read its own registry as donor).
- `consume.py` ingests the owner's **live** corpus — the adverts the owner has
  not marked `disappeared` — reading its registry and its cached advert
  evidence, and scoring every advert with the *consumer's* profile through the
  same cached-evidence path that `reassess.py` uses for `--from-principal`.
- `JobRegistry` gained `upsert(first_seen=…)` (preserve the true discovery date
  rather than stamping the ingest date) and `retire_hashes()` (explicit
  retirement, so nothing is retired by whole-source heuristics).

Boundaries the design preserves:

| Property | Rule |
|---|---|
| Owner's state | read-only; an ingest never writes it |
| Scoring | always the consumer's profile, namespace and matcher chain |
| Registry, wiki, ledger | the consumer's; the ledger row carries `mode=consume` |
| Discovery date | the owner's `first_seen` is preserved, not re-stamped |
| Lifecycle | the owner owns it: an ingest retires exactly the adverts that dropped out of the owner's live corpus since the previous ingest (hashes recorded in `state/<p>/cache/last_consume.json`) — never adverts the consumer discovered itself |
| Idempotence | keyed on the owner's run id; re-scoring is free when the assessment cache already holds the advert |
| Consent | opt-in per source in the consumer's own profile file; no default sharing, no credential or source fallback |

Cost shape: `decide`/`extract` (the crawl) happens **once** for a shared source;
`match` (the score) still happens once per principal, which is unavoidable and
desirable. On 2026-09-12 KJ's first ingest of LJ's `research_orgs` + `neolabs` +
`hf_startups` corpus covered 174 live adverts for **zero** LLM calls (every
assessment was an assessment-cache hit) and surfaced 20 leads new to KJ.

KJ consumes those three sources from LJ today; deleting an entry — or the file —
returns the source to KJ's own crawl on its next run. See
[chapter 04](04-sources.md) and [chapter 15](15-principal-data-boundaries.md).

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
| `job_registry.py` | SQLite/CSV lifecycle tracking; `first_seen` on insert, `retire_hashes()` for consumer-driven retirement. |
| `history.py` | Prior-contact scanner: `applications/` + Gmail. |
| `wiki_store.py` | Markdown wiki output. |
| `cases.py` | Lead → case slug (`YYYYMMDD-Company-Role`) + case dir helpers. |
| `sandbox.py` | Sandboxed Python REPL (`run_python` tool) — the RLM pattern. |
| `llm_client.py` | OpenAI-compatible LLM client (transport layer). |
| `llm_runtime.py` | Facade over pi-agent's streaming. |
| `config.py` | Configuration + `load_profile_text` / `load_bio`. |
| `crosscheck.py` | Append-only prior-contact check across reports (no LLM, ~5s). |
| `incremental_crawl.py` | Bounded incremental sweep (cost-capped, ad-hoc runs); source-policy aware. |
| `consume.py` | Cross-principal ingest: scores an owner's live corpus for this principal (owner read-only). |
| `reassess.py` | Cached-evidence replay — own registry, or another principal's with `--from-principal` — into a dated report. |
| `build_manual.py` | Regenerates `MANUAL.md` from live state. |
| `rescore.py` | Re-runs pre-LLM kills over historical wiki entries. |
| `gmail_accessor.py` | Gmail API helper (used by sources/gmail_lj_jobs + history). |
| `sources/` | The 12 data-source modules. |
| `tests/` | pytest suite — 943 tests collected 2026-09-20 (the count grows with each suite; `../venv/bin/python3 -m pytest --collect-only -q` for the live number). |

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

## History integration boundary (live writer, 2026-09-20)

The owner run ID passed to `consume.py` is currently an idempotence marker;
it does not freeze the registry and evidence it reads. The target consumes a
sealed, explicitly allowed advert corpus and records owner observation time
separately from consumer availability. Profile/history inputs and actual model
responses must also be bound to each assessment. All entry points participate
in one run context; wrapper and pipeline IDs become one lineage. The capture,
query and control contracts are defined once in chapters 26–27 and reused by
CLI, dashboard and MCP.

**Status (2026-10-01):** the writer is live. WP1a's seal engine, WP2a's readers
and the WP1b-A/B kernels shipped, `lovework-crawl.sh` runs through
`crawl_operation.py` (pin inputs → run → seal), and sealed bundles exist for
lj/kj/vj under `state/<p>/strata/` with the first production seals on
2026-09-20. What is still open is the *rest of the boundary*: the other entry
points (direct `main.py`, consume, reassessment) do not yet seal, coverage is
recorded per crawl rather than per source (so absence cannot be qualified —
`status.py` says `unavailable: …` rather than guessing), and the remaining
`consume.py` semantics above are unchanged.
