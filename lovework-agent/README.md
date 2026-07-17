# lovework-agent

The package. Implements the LoveWork personal job discovery agent.

See [`../README.md`](../README.md) for the project overview and
[`ARCHITECTURE.md`](ARCHITECTURE.md) for the detailed design.

## CLI

### Autonomous mode (cron, default)

```bash
../venv/bin/python3 main.py --profile lj --role general --source all --report
../venv/bin/python3 main.py --profile lj --role cofounder --source neolabs --report
../venv/bin/python3 main.py --profile vj --role platform-sre --source all
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
- `--profile {lj,vj,kj,pk}` — which candidate profile
- `--role ROLE` — specific role file under `profiles/<name>/roles/`
- `--source {all,research_orgs,neolabs,hf_startups,hn_hiring,hn_jobs,gmail_lj_jobs,linkedin_related,company_pages,harnham}` — data source
- `--report` — generate markdown report
- `--json` — output findings as JSON
- `--dry-run` — skip wiki writes
- `--dspy` — use DSPy typed signatures
- `--list-profiles` — show available profiles/roles
- `--registry-stats` — print job registry stats

### `agent_main.py` (interactive)
- `--profile {lj,vj,kj,pk}` — which candidate profile
- `--role ROLE` — specific role file
- `--query QUERY` — single query, non-interactive
- `--autonomous` — run the full pipeline (same as `main.py`)
- `--source` — source for autonomous mode

## Configuration

Environment variables (in `.env` or shell):

| Variable | Default | Description |
|----------|---------|-------------|
| `DEEPSEEK_API_KEY` / `LLM_API_KEY` | — | LLM API key (required) |
| `FIRECRAWL_API_KEY` | — | Web scraping API key (recommended) |
| `LLM_MODEL` | `deepseek-chat` | Model to use |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | API base URL |
| `LLM_TEMPERATURE` | `0.3` | Sampling temperature |
| `LLM_MAX_TOKENS` | `4096` | Max output tokens |
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
ValueError: Profile not found: lj (looked in /path/to/profiles/lj)
```
Make sure `profiles/lj/soul.md` exists. Run `--list-profiles` to see what's available.

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

## See also

- [`../README.md`](../README.md) — project overview
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — detailed design
- [`../profiles/example/`](../profiles/example/) — anonymised profile template
