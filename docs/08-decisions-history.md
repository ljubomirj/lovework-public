# Chapter 08 — Decisions & History

> **Audience:** builders; anyone who needs the *why*.
> **See also:** [`../DECISIONS.md`](../DECISIONS.md) (the live, full-length decision log — this chapter condenses it).

## The 18 decisions, condensed

Each decision is "what we chose + one-line why". For the full reasoning, read
the corresponding D# entry in [`../DECISIONS.md`](../DECISIONS.md).

| # | Decision | One-line why |
|---|----------|--------------|
| **D1** | **Python** (not TypeScript) | DSPy/GEPA optimisation is Python-only; that's the differentiating ambition. TS would lock it out or force an interop boundary. |
| **D2** | **Merge basis: work-like-agent** (superset); work-crawler archived | Diffing the files showed work-crawler was a strict, inferior subset. work-like contributes nothing unique. |
| **D3** | **Location: `lovework/lovework-agent/`** | Mirrors the proven work-like layout; shares profiles/applications/Gmail wiring with `LJ-work-2026/`. |
| **D4** | **Trajectory: Phase 1 personal → Phase 2 mature → Phase 3 public web** | Explicit user direction. Phasing de-risks: ship value fast, mature the ML, multi-tenant web last. |
| **D5** | **Integration: Hermes skill + CLI + launchd** (not an in-process subagent) | A true Hermes subagent must be Python and is spawned dynamically; the lowest-risk working integration is a skill + cron. (MCP server added later in D18.) |
| **D6** | **Extract `pipeline.run_pipeline()` now** | Main.py baked the pipeline into argparse; lifting it into an importable function with injectable collaborators prevents a Phase-3 rewrite. |
| **D7** | **Retain wiki + cache + profiles verbatim** | The crawl history and wiki are the asset. The wiki shape is the Phase-3 frontend's data model — treat it as a stable contract. |
| **D8** | **Archive, don't delete** | House rule + safe rollback. Predecessors (work-crawler, work-like, old skills) stay until LoveWork is confirmed live. |
| **D9** | **Provider-agnostic LLM** (DeepSeek default, OpenAI-compatible) | Any provider swaps via env — no DeepSeek-specific calls anywhere. DeepSeek stays default: cheap and already wired. |
| **D10** | **Work-authorization hard-kill** (pre-LLM) | US-only / no-visa roles are a deal-breaker that wastes an LLM call. Regex on location + description; profile owns the rule. |
| **D11** | **HN Algolia API** for both HN sources | No auth, no rate-limit pain, clean JSON. HTML parsing only for /jobs (which the API doesn't cover). |
| **D12** | **Per-entry re-crawl cadence** in `company_pages.yaml` | A single global cadence is wrong — past employers warrant fortnightly, YC AI companies monthly, watchlist weekly. Decision at cron time, persisted in the same YAML. |
| **D13** | **Lead → case** with `YYYYMMDD-Company-Role` slug | Mirrors LJ's existing `applications/` convention; slots into the workflow without a second naming scheme. |
| **D14** | **Org-level re-apply cooldown** (18 months, configurable) | Same-role cooldown misses "different team, same company". The org-level kill closes that door for N months after *any* rejection at the org. |
| **D15** | **Two-layer manuals** (`MANUAL.md` + `DECISIONS.md` + skills) | Operational state (regenerated) separate from architectural rationale (changes only on a deliberate decision). One file for the agent's first read, the other for context. |
| **D16** | **Single canonical skill + per-agent symlinks** | Multiple agents each look for SKILL.md in their own path. One canonical home + symlinks stops the copies drifting. |
| **D17** | **3-layer profile model** (long path / tip / branching possibilities) | The flat profile described past + present but not *where the principal is going*. Layer 3 carries explicit `matcher signal:` lines so the scorer weights future-direction roles correctly. **D17-amend:** no symlinks in profiles — they don't version cleanly in the double-git workflow; all CV files are plain copies. KJ/VJ/PK built out on the same model. |
| **D18** | **Dashboard + MCP server merged** (one process, two faces) | DECISIONS D5 called for an MCP server; the dashboard already runs as a small Python web server reading live state. Merging delivers D5 without a second process fighting over the registry. HTTP transport, no SDK dep, 9 tools. |

## Where the decisions live

- **Full-length rationale:** [`../DECISIONS.md`](../DECISIONS.md) — append-only, the source of truth.
- **Operator-facing summary:** [`../MANUAL.md`](../MANUAL.md) (regenerated).
- **Agent-facing bootstrap:** [`../agents/skills/lovework/SKILL.md`](../agents/skills/lovework/SKILL.md).

When a decision changes, update `DECISIONS.md` (the canonical), then propagate
to MANUAL/SKILL via `build_manual.py` / `sync_skills.sh`. This chapter is a
condensation; do not edit it to override a decision — edit `DECISIONS.md`.

## JOURNAL (live, dated events)

[`../JOURNAL.md`](../JOURNAL.md) — one-line dated events. Currently:

- **2026-06-26** Audited `git-ls-files-public` (720 files) for secrets before
  first push to private GitHub repo. No hardcoded API keys, passwords, tokens,
  private keys, or credential-bearing URLs found. All secrets are env-var
  sourced; `.env` is excluded from the public list.

## LEARNINGS (patterns + fixes)

[`../LEARNINGS.md`](../LEARNINGS.md) — recurring patterns worth remembering.

### Secret-scanning before public-repo push

- **Pattern:** When splitting a repo into `.git-private` + `.git-public`,
  always audit the public file list for secrets before the first push.
- **Root cause:** Cached web pages (`page_*.md`) contain third-party CDN tokens
  (e.g. `hibob.com/image/...?token=...`). Not your secrets, but they trigger
  regex scans.
- **Fix:** Distinguish third-party tokens in scraped content from actual
  hardcoded credentials. Source code loads all keys via `os.getenv()` /
  `python-dotenv`; `.env` is correctly excluded from the public list.

### Double-git workflow

- `.git-private` holds full history. `.git-public` is empty and receives only
  the files listed in `git-ls-files-public`.
- `.env` (containing `DEEPSEEK_API_KEY` + `FIRECRAWL_API_KEY`) is NOT in
  `git-ls-files-public` — correct.

## Predecessors (archived, not deleted — D8)

LoveWork is the second-generation. The two predecessors live untouched in
`~/LJ-work-2026/` for safe rollback:

- **`work-like/`** — the merge basis. Has the agent loop, 8 tools, DSPy
  signatures, sandbox, and tests. `work-like-agent/` was copied to
  `lovework-agent/` (code + cache/jobs.db + cached pages + wiki), and
  `work-like/profiles/` to `lovework/profiles/`.
- **`work-crawler/`** — the older, inferior subset. Identical core modules but
  none of work-like's extras. Contributes nothing unique; kept for rollback.

The rebrand (Phase 1) touched every product-name string: `WorkLike→LoveWork`,
`work-like→lovework`, `WORK_LIKE_*→LOVEWORK_*`, `work-like.be→lovework.be`,
`work_like_agent→lovework_agent`, `work-crawler→lovework` (incl. the
easy-to-miss "Work Crawler" space variant and the `worklike` PyPI name).
Historical `wiki/` footers were intentionally left.

## Build history (the conversation that built it)

[`../README.LJ`](../README.LJ) — very large file; read sections with
offset/limit. It is the agent turn log from the multi-session build. Not
authoritative for current state (the docs/ folder is), but invaluable for
*why something was done a certain way* — search it when a decision's full
rationale isn't enough.

## What's next

- [`00-index.md`](00-index.md) — back to the top.
- [`../DECISIONS.md`](../DECISIONS.md) — the full decision log.
