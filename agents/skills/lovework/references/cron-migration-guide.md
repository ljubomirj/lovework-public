# Cron migration guide — LoveWork from macbook2 → gigul2

**Status:** Complete! gigul2 (HermeL) runs LoveWork on cron as of 2026-06-25.

**Setup steps completed:**
- [x] Data already present at `/opt/ljubomir/LJ-work-2026/lovework/` (rsync from macbook2)
- [x] `venv` at `lovework/` root with uv — project installed as editable
- [x] `.env` with `LLM_API_KEY` (OpenCode Go), `LLM_BASE_URL`, `LLM_MODEL=mimo-v2.5`
- [x] Symlinks for `applications/`, `AI-for-HF-startup-tracker/`, `neolab-and-emerging-ai-lab-tracker.txt` point to `../` — single source of truth
- [x] `gmail_accessor.py` path fixed: now uses `HERMES_HOME` env var (works on both macbook2 and gigul2)

**Cron jobs (via Hermes cron, not systemd):**
- [x] `lovework-incremental` — Tue/Thu 09:00 (`0 9 * * 2,4`)
      Runs via `~/.hermes-gigul2/profiles/hermel/scripts/lovework-incremental.sh`,
      which detaches `lovework-crawl.sh incremental` and returns immediately to Hermes.
      Sources: neolabs (all 61 orgs) + Gmail LJ-jobs + hn_hiring + hn_jobs
      Cost: ~$0.05-0.20 per run
- [x] `lovework-full-sweep` — Sunday 09:00 (`0 9 * * 0`)
      Runs via `~/.hermes-gigul2/profiles/hermel/scripts/lovework-full.sh`,
      which detaches `lovework-crawl.sh full` and returns immediately to Hermes.
      Sources: all 8 (research_orgs, neolabs, hf_startups, hn_hiring, hn_jobs, gmail_lj_jobs, linkedin_related, company_pages)
      Cost: ~$0.15-0.50 per run

**Delivery:** Results fan out to all connected channels via Hermes gateway (Telegram).

**Gmail cross-check:** Not yet authenticated on gigul2. The `gmail_accessor.py` gracefully returns
None when Google APIs aren't available, so the crawler works without it. To enable, either:
- Install `google-api-python-client` and `google-auth-oauthlib` in the lovework venv
- Or set up the `gws` binary (from the Hermes google-workspace skill)

**Scripts on gigul2:**
- `~/.hermes-gigul2/profiles/hermel/scripts/lovework-incremental.sh` —
  authoritative HermeL incremental launcher
- `~/.hermes-gigul2/profiles/hermel/scripts/lovework-full.sh` —
  authoritative HermeL full-sweep launcher
- `~/LJ-work-2026/lovework/lovework-crawl.sh` — shared background worker;
  owns the lock, detailed log, MANUAL regeneration, and success email
