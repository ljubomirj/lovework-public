# Chapter 06 — Dashboard + MCP Server

> **Audience:** agents (the MCP half) and operators (the dashboard half).
> **See also:** [`../DECISIONS.md`](../DECISIONS.md) D18; [`../agents/skills/lovework-hydrate/SKILL.md`](../agents/skills/lovework-hydrate/SKILL.md) "Dashboard + MCP server" section.

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
cd ~/lovework-engine/lovework-agent
../venv/bin/python3 dashboard_server.py [--port 8765]
```

The `LOVEWORK_ROOT` path is resolved by probing principals, preferring the
canonical `lovework-engine/` inside the workspace (all machines; the ~/Documents location
was retired 2026-09-20 — quota'd iCloud+iDrive sync roots), then
`/srv/lovework-dev/lovework-engine` (host-a dev), honoring an env
var first. No manual setup needed on any of LJ's machines.

**Principal boundary (2026-09-21; narrowed again 2026-10-08).** The browser
surface publishes only the allow-listed operator-facing areas (`wiki`, `logs`,
`cache/runs`, `cache/incidents`) — **not `cache` as a directory**: a bare
`cache` entry published the registry, downloaded source pages and enrichment,
contradicting the boundary's own stated rule, and was removed.

**The case directories are published too (2026-10-09).** `applications` joins the
list so a GO's application pack is openable from the wiki ("when applying, the
principal should not start from Zero" — LJ). Each pack carries a fixed-name
`application_pack.md` (advert + fetched content, the application form, the
tailored CV and letter, the company/hiring background, and the assessment), and
the wiki index links it from every GO listing. **The boundary holds where it
matters:** LJ's *manual* cases are symlinks out of the state tree, and the
serving layer refuses a path that resolves outside its root — measured: a
LoveWork pack page → 200, a symlinked manual case → 404.

**The one exception, narrowed by name (2026-10-08).** `cache` is published again
so that a report's `- **Cached advert**:` line can be opened — and it is
narrowed to the fetch cache's own advert pages by `AREA_FILE_ALLOW`, a filename
allow-list (`^page_[0-9a-f]{16}\.md$`) applied before serving. Everything else
under `cache/` still 404s, including a *directory* request, so the registry
(`jobs.csv`), the ledgers, the enrichment caches, `search/`, `archive/` and
`receipts/` are unreachable. Verified end to end: a real advert page → 200;
`jobs.csv`, the cache listing, and the historical 64-hex names → 404. The
publication is what makes the evidence trail *usable*; the complementary fix is
on the writing side (`enrichment.LeadEnricher._remember_in_fetch_cache`), since
only the provider fallback used to write the file the link names.

Read bindings share one authorization helper
(`principal_runtime.authorize_principal`): a binding serves its own principal
unless others are explicitly allowed, unknown names are refused, and a refusal
never returns data. The MCP surface exposes `asof_explain` (principal-scoped,
bounded, cited).

## Security

The dashboard and the MCP face read every principal's wiki, reports and logs,
and MCP exposes `run_python`, so the server is **loopback-only by default**
(`--host` defaults to `127.0.0.1`). LAN access is explicit: `--host 0.0.0.0`.

Authentication turns on when a token is set, via `--token <secret>` or the
`LOVEWORK_DASHBOARD_TOKEN` environment variable:

```bash
../venv/bin/python3 dashboard_server.py --host 0.0.0.0 --token "$LOVEWORK_DASHBOARD_TOKEN"
```

With a token set, **every route** requires it — `/`, `/dashboard/`, `/health`,
`/api/*`, `/docs/*`, `/principals/*` and `POST /mcp`:

| Client | Form |
|--------|------|
| API / MCP | `Authorization: Bearer <token>`, or `?token=<token>` |
| Browser | `/?token=<token>` on the first load; the server replies with an HttpOnly, `SameSite=Lax` `lovework_token` cookie, and later requests need only the cookie |

Missing or wrong credentials get `401` with a short plain-text body that never
echoes the token. The comparison uses `hmac.compare_digest`, and the token is
never logged (`?token=…` is redacted to `token=REDACTED` in the request line).
An MCP client talking to a token-protected server must send the token too; the
Hermes `url:` form can carry it as `http://127.0.0.1:8765/mcp?token=<token>`.

**Refusal rule:** a non-loopback bind with no token is a startup error, not a
warning. The process exits non-zero (2) *before* opening a socket and names
both fixes: `--host 127.0.0.1`, or `--token` / `LOVEWORK_DASHBOARD_TOKEN`.

Startup logs one line with the effective URL, whether auth is on, and whether
the bind is network-reachable:

```
INFO: LoveWork dashboard: http://127.0.0.1:8765  auth=on  bind=LAN-exposed (0.0.0.0)
```

### `--no-auth` — internal-trial LAN mode (deliberate opt-out)

```bash
../venv/bin/python3 dashboard_server.py --host 0.0.0.0 --no-auth
LOVEWORK_DASHBOARD_NO_AUTH=1 ../venv/bin/python3 dashboard_server.py --host 0.0.0.0
```

`--no-auth` (or `LOVEWORK_DASHBOARD_NO_AUTH=1`) means **authentication is off
for this process**: no token is required, every route — `/`, `/dashboard/`,
`/health`, `/api/*`, `/docs/*`, `/principals/*` and `POST /mcp` — answers
credential-free requests, and the MCP `run_python` tool is reachable without
credentials too.

The flag **is** the explicit operator consent: it is the only thing that
bypasses the refusal rule, and the bypass is loud on purpose:

```
WARNING: AUTH DISABLED (--no-auth / $LOVEWORK_DASHBOARD_NO_AUTH): every route on
         this process, including POST /mcp and its run_python tool, is served
         without credentials
INFO: LoveWork dashboard: http://127.0.0.1:8765  auth=off  bind=LAN-exposed (0.0.0.0)
      (internal trial) — any exposure beyond the trusted home LAN must use --token
```

If both `--no-auth` and a token are given, the token is **ignored** (with a
warning that names it as ignored); `--no-auth` wins. The token value itself is
never logged.

**When it must not be used.** Only for the internal R&D trial where LJ is
patient#0 using his own data and persona on the trusted home LAN (only lj, pk,
vj, kj at home). Do **not** use `--no-auth` when the process is reachable from
anything beyond that LAN — a café/hotel/office network, a VPN, a container
network, a cloud host, a port-forward, a reverse-proxy tunnel, or any bind on a
machine that is not on the home LAN. In all of those cases use
`--host 0.0.0.0 --token "$LOVEWORK_DASHBOARD_TOKEN"` (or keep the loopback
default and tunnel with credentials). Because the MCP face exposes
`run_python`, an unauthenticated non-home-LAN bind is remote code execution,
not just a data leak.

Safety that is **unchanged**: without `--no-auth` (and without a token) a
non-loopback bind still refuses to start with exit 2, and the default bind is
still loopback `127.0.0.1`.

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

### Documentation surface (shared, not per-principal)

Documentation is the one surface on this process that does not belong to a
principal, so it is served from the repo, not from `state/`:

- `GET /docs/` → 302 to **`/docs/html/`**, the docsify site root (sidebar,
  in-page TOC; the client fetches each chapter as raw markdown).
- `GET /docs/<chapter>.md` → the chapter **rendered server-side** (same renderer
  as reports: breadcrumb, link rewriting, `?raw` for the source).
  `/docs/00-index.md` is the documented entry point, linked from the root index
  and the navbar.
- `GET /docs/html/<file>` → plain static files for the SPA (`index.html`, the raw
  `.md` it fetches, `lovework-agent/*.py`, …).

`docs/html/` is a farm of relative symlinks, so this subtree is authorised by the
**resolved** path rather than the URL prefix: `README.LJ`, `profiles/<principal>/`,
`state/`, `applications/`, `credentials/`, dotfiles, and anything resolving
outside the repo answer **404**, even when a farm link points at them
(`/docs/html/README.LJ`, `/docs/html/profiles/example/soul.md`,
`/docs/html/../README.LJ` are covered by tests). `profiles/example/` is allowed.
A *public* copy is a different job: `./serve-docs.sh build` dereferences the farm
into a static tree and excludes the same private set.

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
an incremental crawl takes 45–90 min and a full sweep 1.8–8.8 h (measured;
[chapter 30](30-inference-chain-and-data-map.md) §4). A full sweep must **not** be
driven through this blocking call: an 8.8 h HTTP request is unusable and will be
cut off long before the run ends. Run sweeps through the wrapper/cron path
(`lovework-crawl.sh`, which detaches and lets the dashboard show progress) and
reserve `run_pipeline` for bounded single-source work.

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
   pip install mcp   # not currently installed on host-b
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
