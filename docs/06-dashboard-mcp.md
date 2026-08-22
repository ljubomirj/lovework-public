# Chapter 06 — Dashboard + MCP Server

> **Audience:** agents (the MCP half) and operators (the dashboard half).
> **See also:** [`../DECISIONS.md`](../DECISIONS.md) D18; [`../agents/skills/lovework/SKILL.md`](../agents/skills/lovework/SKILL.md) "Dashboard + MCP server" section.

## One process, two faces

`dashboard_server.py` is a single Python HTTP server with two faces, both on
`127.0.0.1:8765` (or `--port`):

| Face | Route | Consumer |
|------|-------|----------|
| **LAN master index** | `GET /` | Browser front door — links every published LoveWork surface |
| **Detailed dashboard HTML** | `GET /dashboard/` | Operator — watch a crawl, inspect live state |
| **MCP JSON-RPC** | `POST /mcp` | Any MCP-speaking agent — drives LoveWork in-process |

One process, one port, one source of truth. Read-only tools (`registry_stats`,
`search_jobs`) reuse the dashboard's `fetch_*` functions — single reader, no
parallel state to drift. The merge avoids spawning a second server, fighting
over the SQLite registry, or duplicating the read logic.

## Starting it

```bash
cd ~/LJ-work-2026/lovework/lovework-agent
../venv/bin/python3 dashboard_server.py [--port 8765]
```

The `LOVEWORK_ROOT` path is resolved by probing principals, preferring the
location-agnostic `~/LJ-work-2026/lovework` (works on all machines), then the
legacy `~/Documents/LJ-work-2026/lovework` (macbook2) and
`/opt/ljubomir/LJ-work-2026/lovework` (gigul2), honoring an env var first. No
manual setup needed on any of LJ's machines.

## The dashboard (human face)

Open `http://localhost:8765`. The root is a stable LAN master index: it links
every principal's published wiki, reports, logs, run records, incidents,
project documentation, APIs, and the detailed dashboard. Browse the detailed
auto-refreshing operational dashboard at `http://localhost:8765/dashboard/`.
Its sections are:

- **System** — paths, gateway state.
- **Registry** — live job counts by lifecycle (`new`/`still_open`/`long_lasting`/`disappeared`).
- **Live Run Progress** — if a crawl log was touched <5 min ago and doesn't
  end with a finished marker, shows the tail as "RUNNING".
- **Recent Runs** — the last 20 log files with kind/size/last-line.
- **Cron Schedule** — Hermes cron jobs.
- **Config** — active Hermes profile's model/delegation/fallbacks.
- **Profiles** — which profiles exist, their soul.md + roles.
- **Applications** — count + recent application dirs.
- **Entities** — orgs that originated jobs, most-recently-touched first.
- **Sources** — the canonical `wiki/sources.md`.
- **Reports** — past run reports with decision counts.
- **Top Jobs** — top GO/MAYBE from the latest report.

Plus JSON endpoints: `GET /health`, `GET /api/registry`, `GET /api/progress`.

## The MCP face (agent face)

`POST /mcp` accepts a JSON-RPC 2.0 body. Three methods:

### `initialize`

```bash
curl -X POST http://127.0.0.1:8765/mcp -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```

Returns `{protocolVersion, serverInfo:{name:"lovework",version}, capabilities:{tools:{}}}`.

### `tools/list`

Returns the 9 tool schemas. Names: `crawl_org`, `match_profile`, `search_jobs`,
`check_history`, `fetch_url`, `update_wiki`, `registry_stats`, `run_python`,
`run_pipeline`. Each has an `inputSchema` (JSON Schema object).

### `tools/call`

`params: {name, arguments}`. The result is wrapped in the MCP content envelope:
`{content: [{type:"text", text: <json-string>}], isError: false}`. Errors come
back as standard JSON-RPC errors (`-32601` unknown tool, `-32602` invalid
params, `-32603` internal).

## The 9 tools

| # | Tool | What it does |
|---|------|--------------|
| 1 | `crawl_org` | Crawl one org's site for job listings |
| 2 | `match_profile` | Score a job against a profile (per-principal `profile_name`+`role`) |
| 3 | `search_jobs` | Query the registry by status/org |
| 4 | `check_history` | Prior contact with an org (`applications/` + Gmail) |
| 5 | `fetch_url` | Fetch a URL as markdown (Firecrawl + cache) |
| 6 | `update_wiki` | Write a finding to the wiki |
| 7 | `registry_stats` | Lifecycle counts |
| 8 | `run_python` | Sandboxed Python REPL with the LoveWork modules in scope |
| 9 | `run_pipeline` | **Trigger a full pipeline run** — long-running, blocks until done |

`match_profile` and `run_pipeline` take `profile_name` + `role` so one server
serves all four principals. `run_pipeline` is the "go find me jobs" button as
a tool: it crawls, scores, updates registry+wiki, writes a dated report, and
returns `{entries, disappeared, report_path, gos, maybes}`. **Long-running** —
incremental crawl 3–15 min, full run 20–30 min. The MCP call blocks; the
client timeout must be ≥600s for full runs.

## Why no `mcp` / `fastmcp` SDK dependency?

The JSON-RPC framing is hand-rolled (~150 lines in `mcp_server.py`). Two
reasons: (1) keeps the server launchd-safe with stdlib + httpx only; (2)
avoids dragging a churning SDK into the venv. We implement the stable core
(`initialize` / `tools/list` / `tools/call`) that every MCP client speaks
today. If MCP grows features we need, we'll reconsider.

## Activating in Hermes

Hermes' `native-mcp` skill documents first-class support for `url:`-configured
servers with auto-tool-discovery. Tools register as `mcp_lovework_<tool>` and
appear in every conversation alongside built-ins like `terminal`.

1. Install the optional MCP client dep on each host where it should be active:
   ```bash
   pip install mcp   # not currently installed on macbook2
   ```
2. Add to `~/.hermes-<host>/config.yaml`:
   ```yaml
   mcp_servers:
     lovework:
       url: "http://127.0.0.1:8765/mcp"
       timeout: 600   # full pipeline can take >120s
   ```
3. Restart Hermes. Tools auto-discover: `mcp_lovework_crawl_org`,
   `mcp_lovework_match_profile`, …, `mcp_lovework_run_pipeline`.

See `~/.hermes-<host>/skills/mcp/native-mcp/SKILL.md` for the full Hermes
client reference (transports, security, troubleshooting).

## Tests

`tests/test_dashboard_mcp.py` — 15 tests across two layers:
- **Pure JSON-RPC semantics** (9 tests): initialize, tools/list, tools/call
  envelope, unknown tool, missing params, unknown method, parse error,
  notification→None, protocol version.
- **Full HTTP layer** (6 tests): real `DashboardHandler` on an ephemeral port —
  initialize, tools/list, tools/call, notification→202, **GET / still renders
  HTML (regression guard)**, non-MCP POST→404.

## What's next

- [`07-operations.md`](07-operations.md) — running everything, including the dashboard.
- [`02-architecture.md`](02-architecture.md) — where the dashboard+MCP sit in the system.
