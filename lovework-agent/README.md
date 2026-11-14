# lovework-agent

The package. Implements the LoveWork personal job discovery agent.

See [`../README.md`](../README.md) for the project overview and
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the detailed design.

## CLI

### Autonomous mode (cron, default)

```bash
../venv/bin/python3 main.py --profile lj --role general --source all --report
../venv/bin/python3 main.py --profile lj --role cofounder --source neolabs --report
../venv/bin/python3 main.py --profile vj --role data-statistics-pricing --source all
../venv/bin/python3 main.py --list-profiles
../venv/bin/python3 main.py --registry-stats
../venv/bin/python3 main.py --dspy --profile lj --role general  # use DSPy
```

### Interactive mode (CLI / Hermes)

```bash
../venv/bin/python3 agent_main.py --profile lj --role general          # REPL
../venv/bin/python3 agent_main.py --query "Find UK-based AI jobs"       # one-shot
../venv/bin/python3 agent_main.py --autonomous --profile lj            # cron mode
../venv/bin/python3 agent_main.py --list-profiles
```

## Flags

### `main.py` (autonomous)
- `--profile {lj,vj,kj,pk}` — which principal profile
- `--role ROLE` — specific role file under `profiles/<name>/roles/`
- `--source {all,research_orgs,neolabs,neoclouds,hf_startups,hn_hiring,hn_jobs,gmail_lj_jobs,linkedin_related,company_pages,harnham,digitaljobs}` — data source
- `--report` — generate markdown report
- `--json` — output findings as JSON
- `--dry-run` — skip wiki writes
- `--dspy` — use DSPy typed signatures
- `--list-profiles` — show available profiles/roles
- `--registry-stats` — print job registry stats

### `agent_main.py` (interactive)
- `--profile {lj,vj,kj,pk}` — which principal profile
- `--role ROLE` — specific role file
- `--query QUERY` — single query, non-interactive
- `--autonomous` — run the full pipeline (same as `main.py`)
- `--source` — source for autonomous mode

## Configuration

Environment variables (in `.env` or shell):

| Variable | Default | Description |
|----------|---------|-------------|
| `DEEPSEEK_API_KEY` / `LLM_API_KEY` | — | LLM API key (required) |
| `EXA_API_KEY` | — | Page fetcher, primary (Exa.ai); also resolvable from `~/.lovework/credentials/search/exa_api_key` |
| `FIRECRAWL_API_KEY` | — | Page fetcher, paused 2026-10-05 — kept wired, re-enabled via `LOVEWORK_FETCH_PROVIDERS` |
| `LOVEWORK_FETCH_PROVIDERS` | `exa,http` | Fetch backends in preference order (`exa`, `firecrawl`, `http`) |
| `LLM_MODEL` | `deepseek-v4.1-flash` | Model to use |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | API base URL |
| `LLM_TEMPERATURE` | `0.3` | Sampling temperature |
| `LLM_MAX_TOKENS` | `131072` | Max output tokens (a ceiling, deliberately generous — the model stops when done) |
| `MAX_PAGES_PER_ORG` | `4` | Crawl depth per org |
| `MAX_DEPTH` | `2` | Max link-follow depth |
| `MAX_JOB_AGE_WEEKS` | `4` | Filter out jobs older than this |
| `LOVEWORK_HOME` | `~/.lovework` | User-level config dir |
| `LOVEWORK_WIKI` | `wiki` (in agent dir) | Wiki output dir |
| `LOVEWORK_CACHE` | `cache` (in agent dir) | Cache dir (job registry, page cache) |
| `LOVEWORK_DATASET` | `dataset` (in agent dir) | Append-only historical dataset ledgers |
| `LOVEWORK_APPLICATIONS_DIR` | `<lovework>/applications` | Prior applications |
| `LOVEWORK_HF_TRACKER_DIR` | `<lovework>/AI-for-HF-startup-tracker` | HF tracker |
| `LOVEWORK_NEOLAB_TRACKER` | `<lovework>/neolab-and-emerging-ai-lab-tracker.txt` | Neolab tracker |
| `LOVEWORK_DIGITALJOBS_MAX_SEARCHES` | `10` | Digital Jobs MCP searches per run |
| `LOVEWORK_DIGITALJOBS_MAX_PAGES` | `4` | Cursor pages per search |
| `LOVEWORK_DIGITALJOBS_PAGE_SIZE` | `50` | Results per page (server caps anonymous at 50) |
| `LOVEWORK_DIGITALJOBS_TIMEOUT` | `30` | Per-request timeout (s) |
| `LOVEWORK_DIGITALJOBS_REQUEST_DELAY` | `1.0` | Seconds between MCP requests (anonymous rate limit) |
| `LOVEWORK_DIGITALJOBS_MAX_RETRIES` | `3` | Attempts per request when the API returns HTTP 429 |
| `LOVEWORK_DIGITALJOBS_RETRY_BACKOFF` | `10.0` | Base seconds between 429 retries (linear; `Retry-After` wins) |
| `LOVEWORK_DIGITALJOBS_MAX_RETRY_WAIT` | `90` | Ceiling for a single 429 wait |
| `LOVEWORK_DIGITALJOBS_MCP_URL` | `https://www.digitaljobs.com/mcp` | Endpoint override |

## File layout

```
lovework-agent/
├── main.py              # CLI wrapper (argparse + rendering)
├── pipeline.py          # core run_pipeline() library (cron + agent + future API)
├── agent_main.py        # interactive CLI
├── agent.py             # LoveWorkAgent class
├── tools.py             # 8 tools wrapped for pi-agent
├── llm_runtime.py       # facade over pi-agent
├── dspy_signatures.py   # typed DSPy signatures
├── sandbox.py           # run_python tool (RLM-style REPL)
├── crawler.py           # LLM-guided web crawling
├── matcher.py           # job-to-profile matching (legacy + DSPy)
├── job_registry.py      # SQLite lifecycle tracking
├── history.py           # applications/ + Gmail scan
├── wiki_store.py        # markdown wiki output
├── llm_client.py        # OpenAI-compatible LLM client
├── config.py            # configuration + profile loader
├── sources/             # data source modules
│   ├── research_orgs.py
│   ├── neolabs.py
│   ├── hf_startups.py
│   └── hn_hiring.py
├── cache/               # runtime state (gitignored)
│   ├── jobs.db          # SQLite job registry
│   └── _sandbox_vars.json
├── dataset/             # append-only historical ledgers
│   ├── runs.jsonl
│   ├── assessments.jsonl
│   └── outcomes.jsonl
├── wiki/                # markdown output (gitignored)
│   ├── reports/
│   ├── orgs/
│   └── index.md
└── ARCHITECTURE.md      # detailed design
```

## Troubleshooting

### DSPy not available
```
ModuleNotFoundError: No module named 'dspy'
```
Install: `uv pip install dspy`

### Firecrawl 401 / 403
Missing or wrong `FIRECRAWL_API_KEY`. Check `.env`.

### DeepSeek rate limit
Switch models or wait. Or use a local model (e.g., Ollama) by setting:
```bash
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=llama3.3
```

### Agent loops forever
The `max_turns=20` safety cap should catch this. If not, kill the process and
check the logs. Likely a prompt issue or a confused LLM.

### Wiki writes fail
Check permissions on the wiki directory. The `wiki/` dir should be writable.

### Profile not found
```
ValueError: Profile not found: lj (looked in /path/to/profiles/example)
```
Make sure `profiles/example/soul.md` exists. Run `--list-profiles` to see what's available.

### Role not found
```
ValueError: Role 'foo' not found for profile 'lj'. Available: [...]
```
Run `--list-profiles` to see available roles.

## Development

### Adding a new tool

1. Add the tool factory in `tools.py`:
   ```python
   def my_tool_factory() -> AgentTool:
       async def execute(tool_call_id, params, abort_event=None, on_update=None):
           # ... your logic
           return _result_json(data)
       return AgentTool(name="my_tool", label="...", description="...", execute=execute, parameters={...})
   ```

2. Add to `build_tools()`:
   ```python
   def build_tools(...):
       tools = [...]
       tools.append(my_tool_factory())
       return tools
   ```

3. Document in the agent's system prompt (in `agent.py:_system_prompt`).

### Adding a new data source

1. Create `sources/my_source.py` with a `MySource` class that has a `run()` method.
2. Register in `pipeline.py:run_source()`.
3. Add to the `ALL_SOURCES` list in `pipeline.py`.

### Adding a new role type

Create `profiles/<name>/roles/my-role.md` with role-specific criteria. Run with
`--role my-role`.

## Point-in-time history (strata subsystem)

The `strata*` modules are the point-in-time history track (point-in-time
"what did we know on D" — spec: `../docs/26`, living plan + code map:
`../docs/28` §10). WP0, the standalone WP1a engine and WP2a readers are
shipped and fixture-tested; not yet wired into crawls (WP1b), with zero live
seals as of 2026-09-17. Development is on host-b; scheduled crawls and the
materialised live state remain on host-a:

```bash
../venv/bin/python3 history_inventory.py --profile lj   # provenance inventory
../venv/bin/python3 strata.py --profile lj status       # sealed bundles
../venv/bin/python3 strata.py --profile lj verify --deep
../venv/bin/python3 asof.py --profile lj runs           # point-in-time reads
../venv/bin/python3 asof.py --profile lj state --at 2026-09-14T12:00:00+00:00
```

Modules: `strata_manifest.schema.json` + `strata_contract.py` (frozen
contracts), `strata_fixture.py` (test corpus), `strata.py` (seal engine),
`run_context.py` (run identity/pinned inputs), `asof.py` (readers +
journal), `history_inventory.py`.
They deliberately import nothing from the crawl path.

## See also

- [`../README.md`](../README.md) — project overview
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — detailed design
- [`../profiles/example/`](../profiles/example/) — anonymised profile template
