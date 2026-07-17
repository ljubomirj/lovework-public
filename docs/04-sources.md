# Chapter 04 — Sources

> **Audience:** builders; operators who want to tune what gets crawled.
> **See also:** [`../DECISIONS.md`](../DECISIONS.md) section 3 (input source coverage table) and D11/D12.

## The 9 data sources

Sources build the list of orgs (or adverts) that enter the pipeline. Each is a
module under `sources/` with a uniform `run(crawler, matcher, registry) ->
List[WikiEntry]` shape.

| # | Source | What it crawls | Live? | Module |
|---|--------|----------------|-------|--------|
| 1 | **research_orgs** | 19 hardcoded frontier-research orgs | web | `research_orgs.py` |
| 2 | **neolabs** | `neolab-and-emerging-ai-lab-tracker.txt` (cleverhack list) | web | `neolabs.py` |
| 3 | **hf_startups** | `AI-for-HF-startup-tracker/` (Alex Izydorczyk list) | web | `hf_startups.py` |
| 4 | **company_pages** | LJ's curated `company_pages.yaml` (per-entry cadence) | web | `company_pages.py` |
| 5 | **hn_hiring** | "Ask HN: Who is hiring?" monthly thread (Algolia API) | live API | `hn_hiring.py` |
| 6 | **hn_jobs** | `news.ycombinator.com/jobs` (21-day recency filter) | live HTML | `hn_jobs.py` |
| 7 | **gmail_lj_jobs** | Gmail `LJ-jobs` label → LinkedIn, JobServe, Lensa Aggregated, Totaljobs, CWJobs, TalentSource, and Rec-London alerts | live API | `gmail_lj_jobs.py` |
| 8 | **linkedin_related** | LinkedIn seeds -> related jobs (JSON-LD) | live HTML | `linkedin_related.py` |
| 9 | **harnham** | LJ Harnham search URLs (`profiles/lj/harnham_searches.yaml`) | web | `harnham.py` |

`hn_common.py` is shared by the two HN sources (HTTP, title parser, comment
parser, companion "Who wants to be hired?" discovery).

## The four input pipelines

The 9 sources map onto five conceptual input channels — the original vision
for coverage:

1. **Gmail LJ-jobs inbox** (LinkedIn, JobServe, Totaljobs, CWJobs,
   TalentSource, and Rec-London alerts) → source 7.
2. **Follow LinkedIn links → related adverts** → source 8 (auto-seeded by 7).
3. **Re-visit / crawl career sites for new openings** → sources 1–4 (this is
   LoveWork's core; the LLM-guided crawl + registry lifecycle).
4. **HN "who's hiring"** → sources 5 + 6. (Reddit was considered and dropped —
   too noisy per LJ.)
5. **Recruiter search pages** → source 9. Harnham cannot be driven by a useful
   email-alert registration flow, so LoveWork crawls LJ-maintained Harnham
   search-result URLs directly.

All five channels are wired. Phase 2 closed the original gap Phase 1 left;
Harnham adds a recruiter-search channel.

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
template remains unread and logs a warning, preventing silent lead loss.

## Per-source env tunables

These cap cost and rate; all default to safe values.

| Source | Variable | Default | Purpose |
|---|---|---|---|
| `gmail_lj_jobs` | `LOVEWORK_GMAIL_MAX_EMAILS` | 40 | Cap emails processed per run |
| `gmail_lj_jobs` | `LOVEWORK_GMAIL_MARK_READ` | 1 | Mark processed emails read |
| `gmail_lj_jobs` | `LOVEWORK_LI_CAPTURE_GMAIL_SEEDS` | 1 | Capture LinkedIn search URL as a seed |
| `hn_hiring` | `LOVEWORK_HN_HIRING_THREAD_ID` | "" | Pin a specific thread (skip auto-discovery) |
| `hn_hiring` | `LOVEWORK_HN_HIRING_MAX_COMMENTS` | 250 | Cap top-level comments per run |
| `hn_hiring` | `LOVEWORK_HN_HIRING_MAX_ENTRIES` | 200 | Cap entries recorded per run |
| `hn_jobs` | `LOVEWORK_HN_JOBS_MAX_LISTINGS` | 60 | Cap /jobs listings parsed per run |
| `hn_jobs` | `LOVEWORK_HN_JOBS_MAX_AGE_DAYS` | 21 | Recency filter |
| `linkedin_related` | `LOVEWORK_LI_SEEDS_MAX` | 10 | Seeds processed per run |
| `linkedin_related` | `LOVEWORK_LI_RELATED_MAX` | 25 | Related jobs harvested per seed |
| `harnham` | `LOVEWORK_HARNHAM_MAX_SEARCHES` | 10 | Harnham search URLs processed per run |

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

- `neolab-and-emerging-ai-lab-tracker.txt` — `~/LJ-work-2026/` (symlinked).
- `AI-for-HF-startup-tracker/` — `~/LJ-work-2026/` (symlinked).
- `profiles/lj/company_pages.yaml` — LJ's personal keep-list, auto-seeded on
  first run, updated with `last_checked`/`last_found` after each run.
- `profiles/lj/harnham_searches.yaml` — LJ's Harnham search URLs, including
  `agentic engineer` and the London/contract filtered query.

## What's next

- [`05-matcher.md`](05-matcher.md) — what happens to each job a source produces.
- [`02-architecture.md`](02-architecture.md) — how sources plug into the pipeline.
