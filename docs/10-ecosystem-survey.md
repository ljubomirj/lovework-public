# Chapter 10 — Agentic Job-Tool Ecosystem Survey

> **Audience:** builders deciding what LoveWork should integrate or re-implement.
> **Method:** read each tool's docs + source, summarise its approach, compare to LoveWork, identify borrowable ideas.
> **Date surveyed:** 2026-07-06

Four agentic job-search / career tools live alongside LoveWork under
`~/LJ-work-2026/`. This chapter surveys each one, contrasts it with LoveWork's
design, and catalogues concrete ideas that could improve LoveWork.

---

## 10.1 career-ops

**Origin:** [santifer/career-ops](https://github.com/santifer/career-ops) —
built and used by Santiago Santifer to evaluate 740+ offers, generate 100+
tailored CVs, and land a Head of Applied AI role.

**Runtime:** Claude Code (Node.js + Playwright + YAML + Markdown).

### What it does

- **Auto-pipeline** — paste a JD URL, get a full structured evaluation +
  tailored ATS PDF + tracker entry in one command.
- **6-block scoring** — Role summary, CV match, level strategy, compensation
  research, personalisation, interview prep (STAR+R). Each block is a scored
  dimension, not a flat number.
- **ATS PDF generation** — keyword-injected, Puppeteer-rendered CVs from an
  HTML template with Space Grotesk + DM Sans typography.
- **Portal scanning** — 45+ pre-configured companies across Greenhouse, Ashby,
  Lever, Wellfound; custom query language for title/keyword filtering.
- **Batch processing** — parallel evaluation with `claude -p` sub-agents.
- **Pipeline tracker** — `data/applications.md` with merge/dedup/normalise
  scripts (`merge-tracker.mjs`, `dedup-tracker.mjs`, `normalize-statuses.mjs`).
- **Dashboard TUI** — terminal browser for pipeline state.
- **Interview story bank** — accumulated STAR+Reflection stories across
  evaluations; 5–10 master stories answer any behavioural question.

### Compare to LoveWork

| axis | career-ops | LoveWork |
|------|-----------|----------|
| **Phase** | Post-discovery (you already picked the role) | Pre-discovery (finds roles you didn't know about) |
| **Crawl** | Manual URL paste or portal scan | LLM-guided crawler across 9 sources |
| **Registry** | Flat tracker (`applications.md`) | Lifecycle: new → still_open → long_lasting → disappeared |
| **Profiles** | Single-user, CV + profile.yml | Multi-principal (lj/kj/vj/pk), 3-layer model + work_auth + possibilities |
| **Emergent profession** | Not supported | Native for vj/pk |
| **PDF generation** | First-class (HTML → Puppeteer → PDF) | None (`cases.py` creates only a README) |
| **Scoring** | 6-block structured eval | Flat 0–10 score + reasoning + GO/MAYBE/FLAG/DROP |
| **MCP / API** | None (Claude Code only) | MCP JSON-RPC + HTML dashboard, same process |

### Ideas for LoveWork

| Idea | Value | Effort |
|------|-------|--------|
| **6-block structured evaluation** — replace flat score with (role summary, CV match, level strategy, comp research, personalisation, interview prep) | High | Medium |
| **ATS PDF generation in `cases.py`** — when a GO is pursued, render a tailored CV + cover letter PDF | High | Medium |
| **STAR+R story bank** — accumulate interview stories across evaluations; pre-extract relevant ones per GO | Medium | Low |
| **Playwright portal scanner** — supplement Firecrawl with a Playwright backend for JS-heavy portals (Greenhouse, Lever, Ashby) | Medium | Medium |

---

## 10.2 ai-job-search

**Origin:** [MadsLorentzen/ai-job-search](https://github.com/MadsLorentzen/ai-job-search).
Built for the Danish market but the workflow pattern is language-agnostic.

**Runtime:** Claude Code (LaTeX + Python + Bun shell tools).

### What it does

- **`/setup`** — three onboarding paths: import from `documents/` folder,
  paste a CV, or walk through an interview.
- **`/scrape`** — searches job portals (Jobindex, Jobnet, LinkedIn, etc.),
  deduplicates, presents sorted by fit rating.
- **`/apply <url>`** — the core workflow:
  1. Parse the job posting
  2. Evaluate fit against profile (skills, experience, culture, location, career alignment)
  3. Draft a tailored LaTeX CV and cover letter
  4. **Spawn a reviewer agent** that researches the company and critiques the drafts
  5. Revise based on reviewer feedback
  6. Compile and inspect both PDFs (lualatex / xelatex) — iterate on LaTeX until
     the CV is exactly 2 pages and the cover letter is exactly 1 page
  7. Present final output with a verification checklist
- **`/expand`** — enriches profile by scanning GitHub repos, portfolio site,
  Kaggle, Google Scholar; adds discovered competencies with source tags.
- **`/upskill`** — gap analysis between profile and tracked jobs; produces a
  prioritised heatmap of skill gaps and a learning plan.
- **`/add-template`** — register custom LaTeX templates; stores them with
  `[PLACEHOLDER]` tokens (safe to commit).

### Key innovation: drafter-reviewer separation

Two agents: the drafter writes; a second Claude agent, spawned with a fresh
context, researches the company and critiques the drafts. The drafter then
revises. Catches missed keywords, weak framing, and generic language that a
single pass often leaves in.

### Compare to LoveWork

| axis | ai-job-search | LoveWork |
|------|--------------|----------|
| **Phase** | Application production (post-match) | Discovery + tracking (pre-application) |
| **Crawl** | Portal search via CLI tools | LLM-guided crawler across 9 sources |
| **Registry** | Simple CSV tracker | Full lifecycle in SQLite/CSV |
| **PDF** | First-class (LaTeX, compile+inspect loop) | None |
| **Review** | Two-agent drafter-reviewer | Single-agent scoring |
| **Profiles** | Single-user, CLAUDE.md + skill files | Multi-principal, 3-layer (bio-long/cv-short/possibilities) |
| **/setup** | Structured onboarding with 3 paths | Manual file creation |
| **/expand** | Competency discovery from online sources | Not supported |
| **/upskill** | Gap analysis + learning plan | Not supported |

### Ideas for LoveWork

| Idea | Value | Effort |
|------|-------|--------|
| **Drafter-reviewer workflow** — when a GO is found, spawn a second agent to research the org and critique the match reasoning before presenting it | Medium | Low (pi already has subagents) |
| **Relevance-weighted CV cutting** — when a CV overflows, score each line by (a) relevance to JD, (b) uniqueness, (c) cover-letter dependency; cut lowest first | Medium | Low |
| **Skill gap analysis** (`/upskill`) — compare principal profile against top GOs and produce a bridge plan | High | Medium |
| **Competency expansion** (`/expand`) — scan GitHub, LinkedIn, publications, course syllabi for skills not in the profile; inject into `possibilities.md` | Medium | Medium |
| **Structured onboarding** — a guided `/setup` flow for new principals instead of manual file creation | Medium | Medium |

---

## 10.3 Resume-Matcher

**Origin:** [srbhr/Resume-Matcher](https://github.com/srbhr/Resume-Matcher).
A web app (17k+ stars) that tailors resumes against job descriptions. Runs
fully local with Ollama or with any LLM API.

**Runtime:** Python (FastAPI) + Node.js (frontend) + PostgreSQL (optional).

### What it does

- Upload master resume (PDF or DOCX), paste a job description
- AI-suggested improvements tailored to the role
- **Resume scoring** against the JD with keyword highlighting — visual match
  score, colour-coded keyword presence/absence
- **Cover letter generator** based on resume + JD
- **4 PDF templates** (classic/modern × single/two column)
- Drag-and-drop section re-ordering
- Multi-language UI (EN, ES, ZH, JA, PT-BR)
- Docker deployment

### Compare to LoveWork

| axis | Resume-Matcher | LoveWork |
|------|---------------|----------|
| **Phase** | Resume optimisation (post-match) | Discovery + tracking (pre-application) |
| **Scoring** | Resume-vs-JD keyword match | Principal-profile-vs-job LLM score |
| **Crawl** | None (manual JD paste) | LLM-guided crawler across 9 sources |
| **Registry** | None | Full lifecycle + wiki |
| **PDF** | 4 templates, multi-language | None |
| **Local-first** | First-class (Ollama) | API-only (DeepSeek) |
| **Multi-principal** | Single-user | 4 principals |

### Ideas for LoveWork

| Idea | Value | Effort |
|------|-------|--------|
| **Keyword highlighting + match visualisation** — when lovework surfaces a GO, show which keywords matched and which didn't, with colour-coded diff | Medium | Low |
| **Multi-template PDF export** — add a PDF generation step to `cases.py` using configurable templates | High | Medium |
| **Local-LLM fallback** (Ollama support) — let the matcher run against a local model for development, preview, or when API credits run low | Low | Medium |

---

## 10.4 hiring-agent

**Origin:** [interviewstreet/hiring-agent](https://github.com/interviewstreet/hiring-agent)
by HackerRank. A resume-to-score pipeline designed for **recruiters evaluating
principals**, not for principals finding work.

**Runtime:** Python (PyMuPDF + Jinja + LLM providers).

### What it does

1. **PDF extraction** — PyMuPDF converts PDF pages to markdown-like text.
2. **Section parsing** — per-section LLM calls (basics, work, education,
   skills, projects, awards) → structured `JSONResume` object (Pydantic).
3. **GitHub enrichment** — finds GitHub in the resume, fetches profile + repos,
   classifies projects as `self_project` or `external_contribution`, asks the
   LLM to select the top 7 with a minimum commit threshold.
4. **Evaluation** — 4 category scores:
   - `open_source` (0–35) — contributions to others' projects
   - `self_projects` (0–30) — own project complexity and impact
   - `production` (0–25) — real-world work experience
   - `technical_skills` (0–10) — breadth and depth
   - Plus bonus points (max 20) and deductions → total out of 100.
5. **Fairness constraints** — the evaluation prompt explicitly forbids scoring
   based on name, gender, institution name, GPA, or location. The only signals
   allowed are technical skills, project complexity, open-source contributions,
   and production-level experience.

### Compare to LoveWork

| axis | hiring-agent | LoveWork |
|------|-------------|----------|
| **Perspective** | Employer scoring a principal | Principal scoring a role |
| **Input** | Resume PDF → structured JSON | Profile files (soul, cv, possibilities) |
| **Output** | 0–100 score with 4 category breakdown + evidence | 0–10 score + GO/MAYBE/FLAG/DROP + reasoning |
| **Enrichment** | GitHub profile + repos, classified | None |
| **Fairness** | Explicit forbidden-signal categories | Implicit (no guardrails) |
| **Crawl** | None | Full LLM-guided crawler |
| **Multi-principal** | Single-resume | 4 profiles |

### Ideas for LoveWork

| Idea | Value | Effort |
|------|-------|--------|
| **Structured multi-axis scoring** — replace lovework's flat 0–10 with a breakdown (e.g. role fit, culture fit, growth potential, risk, comp alignment) | High | Medium |
| **Fairness guardrails** — add explicit forbidden signals to the matcher prompt so a role at a prestigious lab doesn't get an automatic score bump unless the profile values it | Low | Low |
| **GitHub enrichment of case dirs** — when a GO is pursued, fetch the principal's GitHub projects relevant to the role and add them to the case dir | Low | Low |

---

## 10.5 Synthesis: what LoveWork should steal

Ordered by estimated value-to-effort ratio for the current Phase 2.

### Quick wins (low effort, medium-high value)

| Idea | From | What it replaces / adds |
|------|------|------------------------|
| **Fairness guardrails in the matcher** | hiring-agent | Adds explicit forbidden-signal rules to the match prompt |
| **GitHub enrichment of case dirs** | hiring-agent | Fetches relevant repos when a GO is pursued |
| **STAR+R story bank** | career-ops | Accumulates interview stories across evaluations |
| **Keyword highlighting on wiki reports** | Resume-Matcher | Visual match diff on GO listings |
| **Drafter-reviewer workflow for agent REPL** | ai-job-search | Spawns a second agent to critique match reasoning |

### Medium projects (medium effort, high value)

| Idea | From | What it replaces / adds |
|------|------|------------------------|
| **Structured multi-axis scoring** | hiring-agent, career-ops | Replaces flat 0–10 with a breakdown across dimensions |
| **Skill gap analysis** (profile vs top GOs) | ai-job-search `/upskill` | New report type |
| **ATS PDF generation in `cases.py`** | career-ops, Resume-Matcher | Turns a case dir from a README into a complete application packet |
| **Competency expansion into `possibilities.md`** | ai-job-search `/expand` | Scans GitHub, publications, syllabi for hidden skills |
| **6-block structured evaluation** | career-ops | Structured output replacing flat reasoning |
| **Relevance-weighted CV cutting** | ai-job-search | Smarter truncation when generating PDFs |

### Longer-term (medium effort, foundational)

| Idea | From | What it replaces / adds |
|------|------|------------------------|
| **Structured onboarding /setup** | ai-job-search | Guided setup for new principals |
| **Playwright portal scanner** | career-ops | Fallback for JS-heavy career portals |
| **Local-LLM fallback (Ollama)** | Resume-Matcher | Offline/dev mode for the matcher |

### The highest-leverage single idea

Replacing LoveWork's flat 0–10 score with a **structured multi-axis breakdown**
(role fit, culture fit, growth potential, risk, comp alignment — or similar)
plus **ATS PDF generation in `cases.py`** when a GO is pursued. These two
together turn a case dir from a README into a complete application packet
ready for LJ's review, and give actionable signal instead of a single number.

---

*This survey was conducted 2026-07-06 by reading each tool's README, CLAUDE.md,
source code, and key configuration files. Tools live at:
`~/LJ-work-2026/career-ops/`, `~/LJ-work-2026/ai-job-search/`,
`~/LJ-work-2026/Resume-Matcher/`, `~/LJ-work-2026/hiring-agent/`.*
