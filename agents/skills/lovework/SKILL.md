---
name: lovework
description: "Run the LoveWork personal job discovery agent — autonomous cron pipeline (crawl org career pages → track in SQLite registry → score against the principal profile → wiki), an interactive REPL, or the dashboard web UI. Use when the user wants to find jobs, crawl careers pages, check the job registry, query the agent, or watch live crawl state. Multi-principal (lj, kj, vj, pk)."
version: 2.4.0
author: LJ
license: proprietary
platforms: [macos, linux]
prerequisites: "Python 3.11+, uv. API keys DEEPSEEK_API_KEY + FIRECRAWL_API_KEY in ~/Documents/LJ-work-2026/lovework/lovework-agent/.env"
tags: [jobs, career, multi-principal, job-discovery, ai-ml, crawling, agent]
related_skills: [career-ops, work-crawler]
---

# LoveWork — Personal Job Discovery Agent

Mission: **LoveWork.** *Work that you love, so you never work a day in your life.*

LoveWork crawls organisation career pages (LLM-guided), tracks every job ever seen in a
SQLite registry (lifecycle: `new` / `still_open` / `long_lasting` / `disappeared`), scores
each against the principal profile (LLM, 0–10, GO/MAYBE/FLAG/DROP) using prior-contact
context from `applications/` + Gmail, and writes findings to a local markdown wiki.

## Status

**Supersedes `work-crawler`** — LoveWork is the second-generation replacement. It adds 8
sources (vs 4), interactive REPL, importable pipeline core, DSPy, org-level re-apply
cooldown, work-authorization hard-kill, Gmail LinkedIn alert integration, incremental
crawl mode, a dashboard web UI, and proper testing (200+ tests). The old work-crawler
launchd agent should be unloaded once the LoveWork cron is confirmed running.

**Running on:** macbook2 (via launchd, Mon/Wed/Fri 09:00).
**Target for:** gigul2 (24/7 Linux box) — see `references/cron-migration-guide.md`.

## Start here

Before running anything, read these two files — they are the source of truth:

1. **`~/Documents/LJ-work-2026/lovework/MANUAL.md`** — operator's manual: latest
   greatest, refresh commands, run log, cross-check log, pointers. Re-runs
   `build_manual.py` to regenerate from live state.
2. **`~/Documents/LJ-work-2026/lovework/DECISIONS.md`** — 17 architectural
   decisions with the *why* (Python over TypeScript, work-auth hard-kill,
   org-level re-apply cooldown, 3-layer profile model, etc.). Read this when
   you need to understand a design choice, not to run things.

## Location

`~/Documents/LJ-work-2026/lovework/lovework-agent/`

## Git topology — one working tree, three repositories

LoveWork deliberately maintains three independent Git histories over the same
files. The `.git` entry is only a convenience symlink to whichever history LJ
is working in at the moment; **never assume it identifies the intended
repository**.

| Git directory | Purpose | Publication boundary |
|---|---|---|
| `.git-private/` | Full working history for LJ and the family. | Private. May contain profiles, cases, reports, crawled material, and operational context. |
| `.git-public/` | Sanitised family/internal subset. | Private GitHub sharing among LJ, PK, VJ, and KJ. |
| `.git-lovework-public/` | Public `ljubomirj/lovework-public` repository. | Public. Documentation, examples, and deliberately selected engine code only. |

Use the target Git directory explicitly whenever inspecting, staging, committing,
or pushing, for example:

```bash
git --git-dir=.git-lovework-public --work-tree=. status --short
git --git-dir=.git-lovework-public --work-tree=. add lovework-agent/*.py
git --git-dir=.git-lovework-public --work-tree=. diff --cached --check
```

For the public repository, treat the following as excluded by default:

- `.env`, credentials, tokens, local Hermes configuration, and host-specific
  runtime state;
- `profiles/`, `applications/`, `README.LJ`, personal logs, Gmail-derived
  material, reports, registry/cache/database files, and dataset ledgers;
- generated crawl output and anything whose provenance or consent is unclear.

Gmail source credentials are always host-local and never live in this
worktree or any LoveWork Git history:
`~/.lovework/credentials/gmail/<hostname>/<credential-key>/`. One visible
`~/.lovework/` root works on every host; the hostname is a child path, not a
host-suffixed directory selected through a symlink. The active Hermes profile's
token is a separate runtime credential. Do not copy or refresh either
credential through LoveWork Git or a cross-host project merge. LJ may
personally track his host-local `.lovework/` in his private `githome`, but that
is a human-owned, installation-specific choice: agents must not assume,
create, stage, or sync such a repository.

Engine Python, source adapters, tests, generic shell wrappers, public docs,
and anonymised examples are eligible for intentional publication. Before a
public commit, inspect the staged patch for secrets and personal data, run the
relevant tests, and report the exact staged scope. Never commit or push unless
LJ has explicitly asked for that repository action.

When asked whether files are suitable for the fully public
`.git-lovework-public/` repository—or to prepare/review a public release—read
[`references/public-release-review.md`](references/public-release-review.md)
before staging. It defines the evidence-based review process, decision labels,
and the distinction between source code, synthetic examples, live personal
data, and host-specific operational artifacts.

## Profiles — multi-principal, 3-layer model (D17)

LoveWork serves four principals, each with a profile under `profiles/<name>/`. LJ is patient #0: the developer of the system and also the first person to whom it was applied.

| Profile | Principal | Mode | Primary roles |
|---------|-----------|------|---------------|
| `lj` | Ljubomir | targeted match (patient #0) | general, contract-ai, cofounder, ai-finance |
| `vj` | Vedar | **statistics/data/pricing/actuarial + sports-analytics job search** | data-statistics-pricing (primary), general; platform-sre/ml-ai are historical only |
| `kj` | Kalen | targeted match — already chose chemist as profession; chemistry+ML differentiator | cheminf, ai-drug-discovery, general |
| `pk` | Petroula | **third profession** (after ArHi, TA) | digital-art, art-research, general |

Every profile uses the **3-layer model**:

1. **`bio-long.md`** — Layer 1 (long path): full past→present CV. Loaded on demand by `load_bio()` (token-cost guard).
2. **`cv-short.md`** — Layer 2 (tip): current highest-SNR short CV. Always loaded.
3. **`possibilities.md`** — Layer 3 (branching): future directions, each with an explicit `matcher signal:` line. Always loaded. When a role aligns with a branch, the matcher adds +1 and names the branch.

Plus: `soul.md` (identity, wants, avoids), `work_auth.md` (visa/location rules driving a pre-LLM hard-kill), `roles/*.md` (role-specific criteria).

**Operating modes matter for interpretation:**
- **lj, kj (targeted match):** score listings against a known profession; optimise for the best fit. KJ is Kalen, a chemist by profession.
- **vj, pk (emergent / third profession):** the profession is *not yet chosen*. VJ is Vedar, the first non-LJ user; he is looking for both a profession and a job, with profession discovery as the higher-order task. The matcher leans toward MAYBE over KILL to grow a wide thicket of plausible listings; clusters of fit in the thicket are *principal professions*. Read their `possibilities.md` framing before interpreting their reports.

Always pass `--profile <name> --role <role>` to the CLI. Default examples below use `lj`/`general`; swap for the principal you're working with.

## Setup (one-time)

### Hermes profile

LoveWork always uses a Hermes profile as its runtime root. Defaults are
`gigul2 → hermel` and `macbook2 → hermeo`. Set `LOVEWORK_HERMES_HOME` to an
explicit profile path, or set `LOVEWORK_HERMES_BASE` and
`LOVEWORK_HERMES_PROFILE`. Unknown hosts require the profile setting when
multiple profiles are present. Reports, logs, dashboard health, and email
notifications display the active profile.

```bash
cd ~/Documents/LJ-work-2026/lovework/lovework-agent
uv venv
env -u VIRTUAL_ENV uv pip install --python ../venv/bin/python3 -e ".[dev]"
```

**API config** — create `.env` with an OpenAI-compatible key and endpoint.
The shared Hermes key works (it uses the same OpenCode Go relay):

```bash
# .env — use the Hermes OpenCode Go key
LLM_API_KEY=sk-...          # Same as OPENCODE_GO_API_KEY in Hermes config
LLM_BASE_URL=https://opencode.ai/zen/go/v1
LLM_MODEL=mimo-v2.5
FIRECRAWL_API_KEY=sk-...    # Optional, for JS-rendered pages
```

Without `.env`, the agent picks up `DEEPSEEK_API_KEY` from the environment.
The `.env` file is critical for launchd cron — launchd doesn't load shell
config files like `~/.zshrc`, so the key must be in `.env`.

## When the user says "find me jobs" / "run lovework" / "what's the latest"

Use this decision tree:

| Intent | Command |
|---|---|
| "what's the latest" / "show me the dashboard" | `cat ~/Documents/LJ-work-2026/lovework/MANUAL.md` (or open the live dashboard — see below) |
| "find new jobs" / "refresh" / "run a crawl" | `crosscheck.py` first (5s, no LLM); then `incremental_crawl.py` (3-15 min) |
| "I just applied to X, did they reply?" | `crosscheck.py --org X` |
| "I applied to X, did I get rejected?" | add the rejection to the .txt file: `echo "\nRejection received $(date +%Y-%m-%d): …" >> applications/YYYYMMDD-X-…/*.txt` |
| "prepare the GO leads for review" | normal non-dry LJ crawls make `*-LoveWork/` PREPARED case packs; use `prepare_cases.py --report ... --dry-run` to preview/backfill an old report |
| "prepare an agent-to-agent interview" | use `prepare_agent_interview.py --dry-run` first; ATA packs end `-LoveWork-ATA` and remain non-applications until the provider confirms start |
| "show me the registry" | `main.py --registry-stats` |
| "I want to chat with the agent" | `agent_main.py --profile lj --role general --query "..."` |
| "find jobs for Kalen/Vedar/Petroula" | same commands with `--profile kj`/`vj`/`pk` and the role from their profile |

**Always read the MANUAL first** — it has the most current "what's the latest
greatest" section. The wiki index.md is also a good fallback.

## Quick reference

### Refresh + read (no LLM, ~5s)
```bash
cd ~/Documents/LJ-work-2026/lovework/lovework-agent
../venv/bin/python3 crosscheck.py                          # cross-check the latest report
../venv/bin/python3 crosscheck.py --org Poolside           # specific org
../venv/bin/python3 build_manual.py                        # regenerate MANUAL.md
```

### Run an incremental crawl (background + log)

Launch the crawl as a background Hermes process so you can monitor
progress and keep working:

```bash
cd ~/Documents/LJ-work-2026/lovework/lovework-agent && \
../venv/bin/python3 incremental_crawl.py 2>&1 | \
  tee logs/incremental-$(date +%Y%m%d-%H%M%S).log
```

From Hermes, use `terminal(background=true, notify_on_complete=true)` with
the command above, then:

- **Monitor progress**: `process(action='poll', session_id='<id>')` or
  `process(action='log', session_id='<id>')` to see accumulated output.
- **View live log file**: `tail -f ~/Documents/LJ-work-2026/lovework/lovework-agent/logs/incremental-*.log`
- **Intervene early** (kill): `process(action='kill', session_id='<id>')`
  if the crawl is taking too long or hitting too many API errors.
- **Check report**: `wiki/reports/YYYY-MM-DD-lj-incremental.md` after completion.

### Full pipeline (what cron runs)
```bash
cd ~/Documents/LJ-work-2026/lovework/lovework-agent
../venv/bin/python3 main.py --profile lj --role general --source all --report
```

### Reassess existing leads after a profile change (no crawl)

Use the cached-evidence replay when a principal changes profession, location
preferences, or other matcher-relevant profile material. It reads only that
principal's registry and saved primary advert text, then writes a separate
dated reassessment report; it never fetches the web, reads Gmail, changes
lifecycle state, or overwrites historical reports.

```bash
cd ~/LJ-work-2026/lovework/lovework-agent
../venv/bin/python3 reassess.py --profile vj --role data-statistics-pricing
../venv/bin/python3 reassess.py --profile vj --role data-statistics-pricing --dry-run
```

The normal `rescore.py` command is intentionally narrower: it only reapplies
cheap rule-based reapply/work-authorisation kills to historical wiki entries.
It cannot recompute profile fit. Cached-evidence reassessment marks a record
`UNSCORED` when its primary advert was not retained, rather than fetching or
inventing evidence. Assessment cache namespaces include a fingerprint of the
full effective profile, so profile edits cannot reuse old scores by accident.

### Scheduled-run evidence and watchdog

Do not treat Hermes's `nohup` launch acknowledgement as crawl completion. For
scheduled runs, the worker records its own start, terminal status, report path,
active Hermes profile, and Gmail sent-message ID under
`lovework-agent/cache/runs/`. When asked whether a scheduled crawl ran or why
no result arrived, inspect this evidence before guessing:

```bash
cd ~/LJ-work-2026/lovework/lovework-agent
ls -lt cache/runs/
../venv/bin/python3 run_watchdog.py \
  --run-type full --weekday 6 --time 09:00 \
  --grace-minutes 15 --max-runtime-minutes 330
ls -lt cache/incidents/
```

On gigul2, HermeL runs principal-owned full sweeps and watchdogs: LJ on Sunday
and VJ on Saturday, each checked at 13:30 and 15:00 on its own sweep day.
It has no LLM cost: silence means the 09:00 crawl has a complete terminal
record and a Gmail message ID; output means a Telegram-visible incident needs
investigation. Five minutes later, a wake-gated HermeL investigator receives a
new incident and may repair only runtime/notification/observability code with
tests; credentials, personal data, sources/scoring, Git, and schedules remain
outside that authority. Read `docs/14-operational-meta-loop.md` for the fixed
expectation → observation → investigation → regression-tested repair loop.

### Interactive mode (the agentic part)
```bash
../venv/bin/python3 agent_main.py --profile lj --role general --query "..."
```

## Key design decisions (read DECISIONS.md for the full set)

- **Org-level re-apply cooldown** (default 18 months): any role at an org where
  LJ was rejected within the last 18 months is auto-DROPPED. Catches "Poolside
  rejected Evaluations 2 months ago — don't apply to Poolside Pre-training
  today, even though the role is different." Configurable via
  `LOVEWORK_REAPPLY_ORG_COOLDOWN_MONTHS=0` to disable.
- **Same-role cooldown** (default 6 months): same role + recent rejection →
  DROP. Catches re-applying to the exact same role.
- **Work-authorization hard-kill**: US citizen / no visa sponsorship → DROP
  before the LLM call. Per-principal rules in `profiles/<name>/work_auth.md`.
- **Lead → case terminology**: a "lead" is a scored finding. When LJ
  decides to pursue it, `cases.slug_for(date, org, role)` returns a
  `YYYYMMDD-Company-Role` slug; `cases.make_case_dir(slug, ...)` creates
  the case directory under `applications/`.
- **GO review packs (LJ)**: the normal pipeline creates
  `YYYYMMDD-Company-Role-LoveWork/` packs for new actionable GO leads. Their
  `.txt` starts `LoveWork status: PREPARED — not submitted`; this means
  *research workspace only*, and `history.py` deliberately ignores it. Only
  change the marker to `SUBMITTED` after LJ has actually applied. The pack
  holds crawl provenance, advert evidence, assessment, and later diligence.
  Existing/recent applications and malformed crawler titles are reported, not
  duplicated. Use `prepare_cases.py --report <report> --dry-run` before a
  manual historical backfill.
- **Agent-to-agent interview packs**: public protocol discovery and local
  preparation use `YYYYMMDD-Company-Position-LoveWork-ATA/`. The marker
  `LoveWork ATA status: PREPARED — interview not started` is ignored by
  history/reapply logic. Preparation may fetch public role/API/SDK
  documentation but may not authenticate, connect external accounts, start,
  message, upload, commit, push, or submit. Runtime identity is host-specific:
  preferred `macbook2 → hermeo → hermeo_lj_bot`, with
  `gigul2 → hermel → hermel_lj_bot` allowed. Read
  `docs/16-agent-to-agent-interviews.md` before advancing a case.

## Architecture (Phase-3-ready)

- **`pipeline.py`** — `run_pipeline(profile, role, source, *, registry, llm, wiki, …)`
  is the importable core. The CLI, the agent, the dashboard, and a future FastAPI
  service (lovework.be) all call it directly. Per-user registry/wiki collaborators
  are injectable for multi-tenant.
- **3 LLM call sites**: `crawler._ask_decision`, `crawler._extract_jobs_from_page`,
  `matcher.match`.
- **Matcher reads the 3-layer profile** (D17): `config.load_profile_text()` combines
  `soul` + `work_auth` + `cv-short` (Layer 2) + `possibilities` (Layer 3) + `role`,
  and the matcher prompt adds +1 when a role aligns with an explicit branching
  possibility. `load_bio()` reads Layer 1 on demand only (token cost).
- **8 agent tools**: `crawl_org`, `match_profile`, `search_jobs`, `check_history`,
  `fetch_url`, `update_wiki`, `registry_stats`, `run_python`.
- **Sources** (`sources/`): `research_orgs`, `neolabs`, `hf_startups`,
  `hn_hiring` (live HN monthly thread), `hn_jobs` (live `/jobs` page),
  `gmail_lj_jobs` (LinkedIn alerts), `linkedin_related` (auth-walled),
  `company_pages` (LJ's curated list with per-entry cadence), `harnham` (LJ-maintained Harnham recruiter search URLs).
- **Dashboard + MCP server** (`dashboard_server.py` + `mcp_server.py`): one
  process, two faces — single-page HTTP UI for humans, JSON-RPC MCP endpoint
  for agents (D18). 9 tools exposed; Hermes connects via the `mcp_servers:`
  config (see Dashboard section above).
- DeepSeek (OpenAI-compatible) for the LLM; Firecrawl for JS-rendered scraping.

## Monitoring & intervening during a crawl

When a crawl is launched via Hermes as a background process:

| Action | Command |
|---|---|
| Check if still running | `process(action='poll', session_id='<id>')` |
| Read accumulated output | `process(action='log', session_id='<id>')` |
| Tail live log file | `tail -f ~/.../lovework-agent/logs/incremental-*.log` |
| Kill a stuck crawl | `process(action='kill', session_id='<id>')` |
| Wait for completion | Hermes auto-notifies on `notify_on_complete=true` |

The log file captures every LLM call with a context label so you can
see what each API request is for:

```
LLM call: [Antim Labs] decision: https://www.antimlabs.com/
LLM ok: [Antim Labs] decision: https://www.antimlabs.com/ — 312 chars
LLM call: [Poolside] match: Member of Engineering Evaluations
LLM ok: [Poolside] match: Member of Engineering Evaluations — 89 chars
```

The three call sites are:
- `[org] decision: <url>` — LLM deciding if a page has jobs and where to go next
- `[org] extract: <url>` — LLM extracting structured job listings from a page
- `[org] match: <job-title>` — LLM scoring a job against the principal profile

The raw httpx `200 OK` lines are noise from the HTTP library — ignore them
or toggle with `LOG_LEVEL=WARNING` to suppress.

## Reading results after a crawl

The incremental crawl produces a report at:
`wiki/reports/YYYY-MM-DD-lj-incremental.md`

Run the cross-check to update prior-contact context:
```bash
../venv/bin/python3 crosscheck.py
```

Then regenerate the manual:
```bash
../venv/bin/python3 build_manual.py
```

## Dashboard + MCP server (one process, two faces — D18)

The same Python HTTP server (`dashboard_server.py`) serves **both** the
human-facing dashboard and an MCP tool endpoint that any MCP-speaking agent
(Hermes, Claude Code, Codex, OpenCode) can drive in-process. One process, one
port, one source of truth.

```bash
cd ~/Documents/LJ-work-2026/lovework/lovework-agent
../venv/bin/python3 dashboard_server.py [--port 8765]
```

**Face 1 — dashboard HTML (human):** open `http://localhost:8765`. The root
is a master LAN index for published principal results, logs, run records,
incidents, project docs, and APIs; the detailed live dashboard is
`/dashboard/`. Its sections are Runs / Jobs / Profiles / Entities / Sources /
Reports / Config / System. Watch a crawl progress in real time (refresh the
page), see latest GOs, or browse per-org history without grepping the wiki.

**Principal results on the LAN:** browse the deliberately narrow published
views at `/principals/lj/wiki/reports/` and `/principals/vj/wiki/reports/`
(for gigul2: `http://192.168.1.251:8765/...`).  `state/` itself is not served:
only each principal's generated `wiki/`, wrapper `logs/`, compact run records,
and incident records are exposed. This keeps source caches, Gmail-derived
data, profiles, and datasets local.

**Face 2 — MCP JSON-RPC (agent):** `POST http://127.0.0.1:8765/mcp` with a
JSON-RPC 2.0 body. Implements `initialize` / `tools/list` / `tools/call`.
Exposes 9 tools: `crawl_org`, `match_profile`, `search_jobs`, `check_history`,
`fetch_url`, `update_wiki`, `registry_stats`, `run_python`, `run_pipeline`.
Per-profile params (`profile_name` + `role`) so one server serves all four
principals. `run_pipeline` is long-running — set client timeout ≥600s for a
full run. No `mcp`/`fastmcp` SDK dependency; JSON-RPC framing is hand-rolled
in `mcp_server.py` (keeps the server launchd-safe).

**Activate in Hermes** (per-host, when ready): install the optional MCP client
dep (`pip install mcp`), then add to `~/.hermes-<host>/config.yaml`:
```yaml
mcp_servers:
  lovework:
    url: "http://127.0.0.1:8765/mcp"
    timeout: 600
```
Restart Hermes. Tools auto-register as `mcp_lovework_*` (e.g.
`mcp_lovework_run_pipeline`) and appear in every conversation alongside
built-ins like `terminal`. See the `native-mcp` Hermes skill for the full
client reference.

**Path probe:** `LOVEWORK_ROOT` honors an env var first; otherwise probes
`~/Documents/LJ-work-2026/lovework` → `~/LJ-work-2026/lovework` →
`/opt/ljubomir/LJ-work-2026/lovework` (gigul2). No manual env setup needed on
any of LJ's machines.

## Costs

Single LLM call ~$0.002 · incremental crawl ~$0.05 · full run ~$0.15 ·
cron Mon/Wed/Fri x 4 weeks ~$2/month.

Uses OpenCode Go relay (`https://opencode.ai/zen/go/v1`) — same key and
rate limits as Hermes itself.

## Pitfalls

### Stale/placeholder API key in .env

The `.env` at `lovework/lovework-agent/.env` must contain a real `DEEPSEEK_API_KEY`.
A placeholder (e.g. 6 chars like `sk-...`) passes `os.getenv()` checks but returns
401 on every LLM call, silently killing the crawl — every page returns
`found_jobs=false`, every job scores 0.

Verify before assuming the agent is ready:

```bash
cd ~/Documents/LJ-work-2026/lovework/lovework-agent
../venv/bin/python3 -c "
from dotenv import load_dotenv; load_dotenv()
import os; k = os.getenv('DEEPSEEK_API_KEY', '')
print(f'Set: {bool(k)}, length: {len(k)}')
# Real keys are 30+ chars. If length < 20, it's placeholder.
"
```

### launchd doesn't source shell config

The launchd agent runs in a non-interactive session and does NOT source
`~/.zshrc`. It loads variables strictly from the `.env` file in the working
directory. If `.env` has a placeholder, the cron job fails silently — check
`logs/stderr.log` for 401 errors.

### launchd plist must be installed explicitly

The plist file exists at `lovework-agent/com.lj.lovework.plist` but is NOT
automatically in `~/Library/LaunchAgents/`. After copying and loading, verify:

```bash
launchctl list | grep com.lj.lovework
```

A `-` in the first column means it's loaded but hasn't run yet; the number is
the PID if currently running.

## Ad-Hoc Listing Triage

When LJ drops a single job URL into conversation, use the browser toolset
to extract the listing without signing in:

1. `browser_navigate(url)` with clean job URL (strip tracking params)
2. Dismiss sign-in modal (ref `@e1`)
3. Accept/reject cookie banner if present (`@e26`/`@e27`)
4. `browser_snapshot(full=True)` to read the accessibility tree

Triaged against: listing status (closed->kill), employment type
(part-time->flag), role domain (quant finance->kill), location,
company quality, seniority. Verdict: **Kill** / **Maybe** / **Pursue**
with 2-4 lines of reasoning.

**Manual LinkedIn browsing:** To filter search results by recency, append
`&f_TPR=r172800` to the URL. See `references/linkedin-recency-url-param.md`.

## Schedule

launchd `com.lj.lovework.plist` — Mon / Wed / Fri 09:00. Install:

```bash
cp com.lj.lovework.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lj.lovework.plist
launchctl list | grep com.lj.lovework
```

**Requires `.env` with valid API keys.** launchd does not source `~/.zshrc`,
so the API key must be in `lovework-agent/.env`. Without it, all LLM calls
fail silently.

**Migration to gigul2:** See `references/cron-migration-guide.md`.

## Refreshing this skill

This skill (`agents/skills/lovework/SKILL.md`) is the canonical source. The
in-repo `.claude/skills/lovework/` and `.codex/skills/lovework/` are
directory-level symlinks to it (auto-resolve). The per-host Hermes copies at
`~/.hermes-<host>/skills/productivity/lovework/SKILL.md` are plain copies that
must be re-synced after any edit:

```bash
cd ~/Documents/LJ-work-2026/lovework
bash sync_skills.sh            # re-copy canonical → all Hermes per-host paths
bash sync_skills.sh --symlink  # alternative: symlink the Hermes paths too
```

## Files the agent should always check

| File | What it has |
|---|---|
| `~/Documents/LJ-work-2026/lovework/MANUAL.md` | Operator's manual (regenerated from live state) |
| `~/Documents/LJ-work-2026/lovework/DECISIONS.md` | 17 decisions, full architecture |
| `~/Documents/LJ-work-2026/lovework/lovework-agent/wiki/index.md` | All GO/MAYBE/FLAG findings |
| `~/Documents/LJ-work-2026/lovework/lovework-agent/wiki/reports/` | Per-run reports |
| `~/Documents/LJ-work-2026/lovework/lovework-agent/wiki/orgs/` | Per-org history |
| `~/Documents/LJ-work-2026/lovework/lovework-agent/cache/jobs.db` | Job registry SQLite |

## Roadmap

- **Phase 1** ✓: personal Hermes agent (cron + interactive).
- **Phase 2** (in progress): 3-layer profile model (D17) ✓, dashboard + MCP
  server (D18) ✓, GEPA-optimize matcher, RLM for long pages, ATS JSON sources
  (Greenhouse/Lever).
- **Phase 3**: public web product at lovework.be (FastAPI + React) — the MCP
  server's tool layer becomes the backend's tool layer for free.
