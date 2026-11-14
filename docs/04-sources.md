# Chapter 04 — Sources

> **Audience:** builders; operators who want to tune what gets crawled.
> **See also:** [`../DECISIONS.md`](../DECISIONS.md) section 3 (input source coverage table) and D11/D12.

## The data sources

Sources build the list of orgs (or adverts) that enter the pipeline. Each is a
module under `sources/` with a uniform `run(crawler, matcher, registry) ->
List[WikiEntry]` shape.

| # | Source | What it crawls | Live? | Module |
|---|--------|----------------|-------|--------|
| 1 | **research_orgs** | 19 hardcoded frontier-research orgs | web | `research_orgs.py` |
| 2 | **neolabs** | `neolab-and-emerging-ai-lab-tracker.txt` (cleverhack list) | web | `neolabs.py` |
| 3 | **neoclouds** | `neocloud-and-compute-tracker.txt` (repo-owned; @jessiedong_ neocloud taxonomy — CoreWeave/Nebius/Lambda, Crusoe/IREN/nscale, Runpod/Vast/Akash, Prime Intellect/Together, Groq/Cerebras, Gimlet) | web | `neoclouds.py` |
| 4 | **hf_startups** | `AI-for-HF-startup-tracker/` (Alex Izydorczyk list) | web | `hf_startups.py` |
| 5 | **company_pages** | LJ's curated `company_pages.yaml` (per-entry cadence) | web | `company_pages.py` |
| 6 | **hn_hiring** | "Ask HN: Who is hiring?" monthly thread (Algolia API) | live API | `hn_hiring.py` |
| 7 | **hn_jobs** | `news.ycombinator.com/jobs` (21-day recency filter) | live HTML | `hn_jobs.py` |
| 8 | **gmail_<principal>_jobs** | Gmail `<P>-jobs` label → LinkedIn, JobServe, Lensa / Lensa Aggregated, Johnson Jobs, Totaljobs, CWJobs, TalentSource, and Rec-London alerts | live API | `gmail_lj_jobs.py` |
| 9 | **linkedin_related** | LinkedIn seeds -> related jobs (JSON-LD) | live HTML | `linkedin_related.py` |
| 10 | **harnham** | LJ Harnham search URLs (`profiles/example/harnham_searches.yaml`) | web | `harnham.py` |
| 11 | **digitaljobs** | Digital Jobs MCP API (`search_jobs` + `list_ai_opportunities`, anonymous JSON-RPC) | live API | `digitaljobs.py` |
| 12 | **jackandjill** | Jack & Jill's published featured-role XML feed (Jill's employer mandates) | live feed | `jackandjill.py` |
| 13 | **speech_companies** | `speech-and-voice-ai-companies-tracker.txt` (repo-owned speech/voice-AI stack: TTS/STT, voice-agent platforms, realtime infra, arenas) — LJ only, like the other curated trackers | web | `speech_companies.py` |
| 14 | **market_players** *(per-principal)* | `profiles/<p>/market_tracker.txt` — the principal's own list of companies to watch; appended by `sources_for_profile` when the file exists, skipped otherwise | web | `market_players.py` |
| 15 | **cheminfo_companies** *(KJ)* | `cheminfo-companies-tracker.txt` (repo-owned: cheminformatics, computational chemistry, molecular modelling, AI-for-drug-discovery). Skips any company KJ's market tracker already covers, so no careers page is crawled twice | web | `cheminfo_companies.py` |
| 16 | **compbiojobs** *(KJ)* | CompBioJobs board — `GET /jobs/cheminformatics` and `/jobs/ai-drug-discovery`, then each job's schema.org `JobPosting` JSON-LD (real `datePosted`, org, location). Ads the board marks closed are skipped | live HTML | `compbiojobs.py` |

Rows 1–13 are `pipeline.ALL_SOURCES`; **row 14 is per-principal** and **rows 15–16
are KJ's own** (`pipeline.KJ_EXTRA_SOURCES`), so the resolved source count differs per
principal (2026-10-11: **lj 14, kj 7, vj 6, pk 6** — the others come from
`VJ_SOURCES`, the generalist base, and `consumes:` delegation).
Resolve, never quote from memory:

```bash
../venv/bin/python3 -c "import principal_runtime as pr, pipeline; \
  print(pipeline.sources_for_profile('<p>', pr.resolve_principal_runtime('<p>')))"
```

`hn_common.py` is shared by the two HN sources (HTTP, title parser, comment
parser, companion "Who wants to be hired?" discovery).

## The input pipelines

`ALL_SOURCES`' 13 sources map onto six conceptual input channels; the
per-principal `market_players` list and KJ's `cheminfo_companies` ride channel 3
alongside them, and KJ's `compbiojobs` board rides channel 6 (a structured
job-board API/feed):

1. **Gmail `<P>-jobs` inbox** (LinkedIn, JobServe, Totaljobs, CWJobs,
   TalentSource, and Rec-London alerts) → source 8.
2. **Follow LinkedIn links → related adverts** → source 9 (auto-seeded by 8).
3. **Re-visit / crawl career sites for new openings** → sources 1–5, source 13
   (speech/voice-AI list) and the per-principal `market_players` list when
   `profiles/<p>/market_tracker.txt` exists (this is
   LoveWork's core; the LLM-guided crawl + registry lifecycle).
4. **HN "who's hiring"** → sources 6 + 7. (Reddit was considered and dropped —
   too noisy per LJ.)
5. **Recruiter search pages** → source 10. Harnham cannot be driven by a useful
   email-alert registration flow, so LoveWork crawls LJ-maintained Harnham
   search-result URLs directly.
6. **Structured job-board API** → source 11 (LJ and VJ; each with its own query file, `profiles/<principal>/digitaljobs_searches.yaml`). The Digital Jobs MCP endpoint
   (`https://www.digitaljobs.com/mcp`) serves anonymous JSON-RPC `search_jobs`
   and `list_ai_opportunities` — no scraping, no auth, structured records.
   Queries live per principal in `profiles/<principal>/digitaljobs_searches.yaml`
   (auto-seeded; LJ and VJ have lists, others are skipped);
   records carry no posting date, so freshness is handled by the registry
   lifecycle, not the board.

7. **Employer-side agent feed** → source 12 (LJ only). Jack & Jill's
   featured-role feed is what companies briefed an AI recruiter to fill — the
   demand side stated by the buyer, not by a board. One XML GET per run; see
   [`24-jack-and-jill.md`](24-jack-and-jill.md) for the permissions, the
   robots boundary and the weekly letter this source feeds.

All seven channels are wired.

## Digital Jobs MCP — measuring the agent-friendliness claim (2026-09-12)

The provider advertises an agent-first server (public, anonymous, structured
records, cursor pagination, documented tools). That is a claim about what using
it as a machine is like, so every crawl that runs the source now measures it:
`sources/digitaljobs.py` instruments every JSON-RPC call (status, latency,
429s, Retry-After, retries, `tools/list`, response keys, per-record field
presence), and `digitaljobs_report.py` renders a verdict plus the evidence.

Output is a **report, not an email**: `state/<p>/logs/digitaljobs-friendliness-<ts>.md`
(and `-latest.md`), written by `run_pipeline` right after the source loop when
`digitaljobs` ran. Verdicts score 0–10 across availability, tool surface, schema
fidelity, record usability, pagination, rate-limit behaviour and field richness —
each component named in the report so the number can be argued with.

First live measurement (VJ, 2 searches): **excellent (9.9/10)** — `tools/list`
offered all 7 documented tools without credentials, all 10 documented response
keys appeared, and 100% of records carried a URL, title, company, location,
work_mode, employment_type, skills, categories and company_industries. The two
honest gaps: `description` is never populated (the API returns compact records
by design) and `salary_text` appears on 60% of records.

## Non-public hosts in provider records (2026-09-13)

The Digital Jobs API returned 28 records pointing at `digitaljobslive.local:55021`
— their staging host — mixed in with real `digitaljobs.com` URLs in the same
response (LJ 2026-09-12; VJ saw 8 the next day). Every one of them failed
enrichment with *"Temporary failure in name resolution"* and `link_check` marked
it unreachable, but they had already reached the registry, the org pages, the
wiki index and the reports. A well-formed-URL check passes `https://x.local:55021/…`;
only a **public-host** check rejects it.

Two changes:

- `sources/digitaljobs.py::public_url` rejects `.local`, `localhost`, `.internal`,
  `.lan`, `.home.arpa`, `.test`/`.invalid`/`.example` and private IP ranges. The
  **lead is kept and only the URL is dropped** — the role is real, the link is not
  — and the rejection is logged, never silent.
- `digitaljobs_report.py` treats unusable hosts as a named defect (deduction only,
  so the 10-point scale is unchanged) and lists the offending hosts with counts.

The live endpoint was re-probed the same day and returned `digitaljobs.com` hosts
for all 100 records of a comparable search, so this was transient provider data
rather than a local rewrite or a client-side bug.

## Cross-principal source ownership

Fetching a career page is principal-neutral work; scoring it is not. A principal
may therefore declare that some sources are crawled **on its behalf** by another
principal (the *owner*) in `profiles/<principal>/sources.yaml`:

```yaml
consumes:
  - source: neolabs
    owner: lj
```

- `pipeline.sources_for_profile()` removes delegated sources from that
  principal's own crawl set, so the pages are fetched once, on the owner's
  schedule.
- `consume.py` ingests the owner's **live** corpus (adverts the owner has not
  marked `disappeared`) from its registry plus its cached advert evidence, and
  scores each advert with the consumer's own profile. The owner's state is
  read-only; scoring, registry, wiki and dataset ledger stay with the consumer.
- Lifecycle ownership stays with the owner: a later ingest retires exactly the
  adverts that dropped out of the owner's live corpus since the previous ingest
  (hashes recorded in `state/<principal>/cache/last_consume.json`). Adverts the
  consumer discovered independently are never retired this way.
- Ingestion is idempotent per owner run (`--owner-run-id`, marker-checked), and
  re-scoring is free when the assessment cache already holds the advert — a
  re-run costs no LLM calls.

KJ consumes `research_orgs`, `neolabs` and `hf_startups` from LJ (2026-09-12).
Deleting an entry — or the whole file — returns that source to KJ's own crawl.

Every run states its position in the log (and in `cache/last_run_context.json`)
so the policy is never implicit:

```text
Crawl context: principal=kj role=general sources=hn_jobs gmail_label=KJ-jobs
Source policy: crawling=[hn_hiring,hn_jobs,gmail_kj_jobs,linkedin_related]
               consumed=[hf_startups<-lj,neolabs<-lj,research_orgs<-lj]
               available-unused=[neoclouds,company_pages,harnham,digitaljobs]
```

(Another principal's mailbox source is excluded from `available-unused`: it is
not this principal's to use.)

## LinkedIn leads: fetch, liveness, resolution, harvest (2026-09-12)

Alert-email leads point at LinkedIn tracking URLs
(`/comm/jobs/view/<id>/?trackingId=…`). An anonymous fetch of those returns the
**sign-in interstitial**, so before this date every LinkedIn lead was scored on
its email snippet alone and the expiry path was structurally blind: 277 VJ leads
shared one 1,881-char "We're signing you in" page as their "advert evidence",
and sampling found **~65% of VJ's and ~62% of KJ's LinkedIn leads already
expired**.

Four steps now handle them:

1. **Fetch through the guest endpoint.** `enrichment.py` extracts the job id and
   fetches `https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<id>`, which
   serves the real posting (title, company, location, full text). The sign-in
   page is never accepted as evidence, in either the HTTP or Firecrawl path.
   `ENRICHMENT_VERSION` was bumped so poisoned cache entries are ignored.
2. **Liveness.** `EXPIRY_MARKERS` already covered "no longer accepting
   applications"; with real text reaching it, `EnrichingMatcher` now returns
   **score 0.0 / DROP** for closed roles. Expired evidence is never cached.
3. **Original advert.** LinkedIn keeps the apply flow on LinkedIn and the guest
   page carries no outbound link, so the original is found by **web search**
   (`web_search.py`; Exa primary, Brave fallback) for **GO/MAYBE leads only**,
   cached per (company, title). A result may only be the employer's own domain
   or a known ATS — boards, mirrors (`builtin*`, `wallstreetoasis`, …) and
   company-profile pages are rejected, because an advert we cannot attribute to
   the employer is worse than the LinkedIn link we already hold. The winner is
   split in two: an **individual advert** (`/careers/<role>`, `/jobs/<id>`, an
   ATS posting) becomes `WikiEntry.original_url` and renders as
   `- **URL**: <original> (via linkedin.com/comm/jobs/view/<id>)`, while a
   **careers index** (`/careers/`) is only a place to look, so the LinkedIn link
   stays the lead URL and the index feeds the harvest instead.
4. **Company harvest.** The same step adds the company to the principal's
   periodic-check list (`profiles/<p>/company_pages.yaml`, `reason:
   lead-detected`, 30-day cadence), idempotent by name and by host. That list's
   `_profile_dir` is now principal-parametrised — it was LJ-hardcoded, and only
   LJ has a starter template, so another principal's file is never seeded with
   his curated AI-lab targets.

Cost controls: `LOVEWORK_RESOLVE_ORIGINALS=0` disables the step;
`LOVEWORK_RESOLVE_MAX_SEARCHES` (default 25) caps searches per run.

## Per-source reporting

Every non-preview crawl writes `state/<p>/logs/sources-<ts>.md`: yield per
source (window vs previous window), lifecycle mix, verdict mix, and — for the
mailbox source — a per-site breakdown (LinkedIn, TotalJobs, CWJobs, JobServe,
Lensa, TalentSource, Rec-London, JohnsonJobs, CV-Library, and the Jack & Jill
drop). Silenced or collapsing sources are flagged. It costs no LLM calls. See
[`26-source-report.md`](26-source-report.md).

## The source interface

Every source follows the same shape — see any of them as a reference.
`pipeline.run_source(name, crawler, matcher, registry)` is the dispatcher:

```python
class SomeSource:
    def __init__(self, crawler, matcher, registry):
        self.crawler = crawler
        self.matcher = matcher
        self.registry = registry

    def run(self) -> List[WikiEntry]:
        # 1. Build the list of orgs to look at (from a tracker file, an API,
        #    a hardcoded list, etc.)
        # 2. For each org: self.crawler.crawl_org(...) → jobs
        # 3. For each job: upsert into self.registry, then self.matcher.match(...)
        # 4. Collect WikiEntry objects and return them.
```

The gmail and linkedin sources skip the crawl step (jobs come straight from
email/JSON-LD) but still flow through `matcher.match`.

Every source's matcher is wrapped by the central primary-page enrichment
stage (`enrichment.py`). Given a direct advert URL, it follows one level,
extracts ordinary HTML plus Next.js `self.__next_f.push` content, and uses
Firecrawl only when the cheap HTTP extraction is inadequate. Evidence is
content-hashed and cached under `cache/enrichment/`.

Discovery provenance is distinct from the primary advert: registry, report,
org page, and assessment-ledger records carry `discovery_url` and
`discovery_date`. The HN parsers normalise both Algolia
(`objectID`/`author`/`created_at`) and Firebase (`id`/`by`/`time`) records.

Supported Gmail lead messages are marked read only after their parser extracts
at least one raw listing. A recognised alert whose provider has changed its
template remains unread and logs a warning, preventing silent lead loss. The
one exception is a bare "your job alert has been created" notice with no advert
block: it is archived, consumed, and yields no leads.

### Raw-email archive (replay without Gmail)

Every processed email body is archived verbatim under
`state/<principal>/sources/gmail/<label>/<msg_id>.json` (id, label, category,
from, subject, date, archived_at, body). The archive is the corpus the parsers
actually saw, so a template change can be diagnosed and a parser fix proven
with **no Gmail access at all**:

```python
from pathlib import Path
from sources.gmail_lj_jobs import iter_archived_emails, replay_archived_jobs

records = list(iter_archived_emails(Path("state/kj/sources"), "KJ-jobs"))
jobs = replay_archived_jobs(Path("state/kj/sources"), "KJ-jobs")
```

The run path archives only what it processes (`is:unread`). To fill the archive
with a label's existing history once, run the backfill (bodies only — it never
marks read, scores, or writes seeds; already-archived ids are skipped, so
re-running is free):

```python
GmailJobsSource(label="KJ-jobs", source_name="gmail_kj_jobs",
                credential_home=<path>, sources_dir=<state>/sources,
                max_emails=500, mark_read=False, capture_seeds=False).backfill_archive()
```

### Re-ingesting the archive (no Gmail)

`--replay-archive` runs the *whole* pipeline path over the saved emails: same
parsers, noise gates, registry upserts, matcher and report — but nothing is
fetched from Gmail and nothing is marked read. Use it to land a parser fix in
the principal's registry and report without waiting for the next crawl, or to
replay a corpus whose mail has already been consumed:

```bash
../venv/bin/python3 main.py --profile kj --role general \
    --source gmail_kj_jobs --replay-archive --report
```

It requires an explicit Gmail source (not `--source all`), and the run ledger
records it as `mode=replay` so replayed assessments are distinguishable from
live ones.

These are the principal's private messages: the archive directory writes its
own `.gitignore` (`*`) so the mail can never be committed, because `state/` is
otherwise partially tracked.

Lensa and Johnson Jobs are retained as low-SNR, primarily US-oriented discovery
feeds. Their URLs are provenance, not proof: primary-page enrichment and LJ's
work-authorisation hard-kills determine whether a lead is real and eligible.

### Per-source env tunables

These cap cost and rate; all default to safe values.

| Source | Variable | Default | Purpose |
|---|---|---|---|
| `gmail_lj_jobs` | `LOVEWORK_GMAIL_MAX_EMAILS` | 40 | Cap emails processed per run |
| `gmail_lj_jobs` | `LOVEWORK_GMAIL_MARK_READ` | 1 | Mark processed emails read |
| `gmail_lj_jobs` | `LOVEWORK_LI_CAPTURE_GMAIL_SEEDS` | 1 | Capture LinkedIn search URL as a seed |
| `gmail_lj_jobs` | `LOVEWORK_GMAIL_ARCHIVE` | 1 | Archive raw emails under `state/<p>/sources/gmail/` |
| `hn_hiring` | `LOVEWORK_HN_HIRING_THREAD_ID` | "" | Pin a specific thread (skip auto-discovery) |
| `hn_hiring` | `LOVEWORK_HN_HIRING_MAX_COMMENTS` | 250 | Cap top-level comments per run |
| `hn_hiring` | `LOVEWORK_HN_HIRING_MAX_ENTRIES` | 200 | Cap entries recorded per run |
| `hn_jobs` | `LOVEWORK_HN_JOBS_MAX_LISTINGS` | 60 | Cap /jobs listings parsed per run |
| `hn_jobs` | `LOVEWORK_HN_JOBS_MAX_AGE_DAYS` | 21 | Recency filter |
| `linkedin_related` | `LOVEWORK_LI_SEEDS_MAX` | 10 | Seeds processed per run |
| `linkedin_related` | `LOVEWORK_LI_RELATED_MAX` | 25 | Related jobs harvested per seed |
| `harnham` | `LOVEWORK_HARNHAM_MAX_SEARCHES` | 10 | Harnham search URLs processed per run |
| `digitaljobs` | `LOVEWORK_DIGITALJOBS_MAX_SEARCHES` | 10 | MCP searches processed per run |
| `digitaljobs` | `LOVEWORK_DIGITALJOBS_MAX_PAGES` | 4 | Cursor pages per search (≤200 jobs/search) |
| `digitaljobs` | `LOVEWORK_DIGITALJOBS_PAGE_SIZE` | 50 | Results per page (server caps anonymous at 50) |
| `digitaljobs` | `LOVEWORK_DIGITALJOBS_TIMEOUT` | 30 | Per-request timeout (s) |
| `digitaljobs` | `LOVEWORK_DIGITALJOBS_MCP_URL` | `https://www.digitaljobs.com/mcp` | Endpoint override |
| `digitaljobs` | `LOVEWORK_DIGITALJOBS_REQUEST_DELAY` | 1.0 | Seconds between MCP requests (anonymous rate limit) |
| `digitaljobs` | `LOVEWORK_DIGITALJOBS_MAX_RETRIES` | 3 | Attempts per request when the API returns HTTP 429 |
| `digitaljobs` | `LOVEWORK_DIGITALJOBS_RETRY_BACKOFF` | 10.0 | Base seconds between 429 retries (linear; `Retry-After` wins) |
| `digitaljobs` | `LOVEWORK_DIGITALJOBS_MAX_RETRY_WAIT` | 90 | Ceiling for a single 429 wait |


## Adding a new source

1. **Create `sources/<name>.py`** with the `run(crawler, matcher, registry)`
   shape. Look at `hn_jobs.py` (live HTML) or `company_pages.py` (curated list)
   for the closest pattern.
2. **Register it** in `pipeline.ALL_SOURCES` and add a branch to `run_source()`.
3. **If it has env tunables**, document them in the table above and in the
   `lovework-agent/README.md` config table.
4. **Add tests** under `tests/test_<name>_source.py`. Mock the network; the
   existing HN tests are a good pattern (`tests/test_hn_sources.py`).
5. **Update coverage** in `DECISIONS.md` section 3.

## Curated org-list inputs (shared data, symlinked into the repo)

- `neolab-and-emerging-ai-lab-tracker.txt` — `~/applications/` (symlinked; third-party, refreshed externally).
- `neocloud-and-compute-tracker.txt` — repo-owned (LJ-curated neocloud/compute list; env `LOVEWORK_NEOCLOUD_TRACKER`).
- `AI-for-HF-startup-tracker/` — `~/applications/` (symlinked).
- `profiles/example/company_pages.yaml` — LJ's personal keep-list, auto-seeded on
  first run, updated with `last_checked`/`last_found` after each run.
- `profiles/example/harnham_searches.yaml` — LJ's Harnham search URLs, including
  `agentic engineer` and the London/contract filtered query.

## What's next

- [`05-matcher.md`](05-matcher.md) — what happens to each job a source produces.
- [`02-architecture.md`](02-architecture.md) — how sources plug into the pipeline.
