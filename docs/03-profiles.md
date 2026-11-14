# Chapter 03 — Profiles

> **Audience:** everyone; especially anyone interpreting a report.
> **See also:** [`../DECISIONS.md`](../DECISIONS.md) D17 (3-layer model) + D17-amend (no symlinks).

## The 3-layer model

Every profile lives under `profiles/<name>/` and uses the same structure:

| Layer | File | Question it answers | Loaded when |
|-------|------|---------------------|-------------|
| **1 — Long path** | `bio-long.md` | "Who am I, start to present?" | `load_bio()` — on demand only (token-cost guard) |
| **2 — Tip** | `cv-short.md` | "What's my highest-SNR current CV?" | `load_profile_text()` — every match |
| **3 — Branching possibilities** | `possibilities.md` | "Where might I go next?" | `load_profile_text()` — every match |

Plus:
- **`soul.md`** — identity, what I am, what I want, what I avoid, role types, decision heuristics. Always loaded first.
- **`work_auth.md`** — visa/location rules; drives a **pre-LLM hard-kill** on US-only / no-visa roles.
- **`roles/<role>.md`** — role-specific criteria; one selected per run via `--role`.

`config.load_profile_text(name, role)` concatenates: soul → work_auth → cv-short → possibilities → role, with `# PRINCIPAL …` section headers. The matcher sees all of it on every score.

### Why three layers?

The original profile was a flat `soul.md / cv-short.md / bio-long.md` triple.
That describes *who the principal is* and *what they did*, but not *where
they're going*. Scoring against only the past and present systematically
under-weights roles that point at a direction the principal is actively
exploring. Layer 3 fixes that — and each branch carries an explicit
`matcher signal:` line so the LLM scorer has unambiguous instructions for
weighting roles that align with a given direction (see [chapter 05](05-matcher.md)).

### Layer files are plain copies (not symlinks)

A previous design (D17) used a relative symlink for LJ's `bio-long.md`. LJ
flagged that symlinks don't version cleanly in the double-git workflow
(`git-ls-files-public` curated list + multi-machine sync). All profile CV
files are now plain copies. The loader (`load_bio`, `load_profile_text`) just
calls `read_text()` — handles copies and symlinks identically.

## The four principals + the example template

| Profile | Principal | Mode | Primary roles |
|---------|-----------|------|---------------|
| `lj` | the principal (patient #0; developer and first applied subject of the system) | targeted match | general, contract-ai, cofounder, ai-finance |
| `vj` | V. (first non-LJ user) | **statistics/data/pricing/actuarial + sports analytics** | data-statistics-pricing (primary), general; platform-sre/ml-ai historical only |
| `kj` | K. | targeted match — already chose chemistry as profession; now chemistry+ML differentiator | cheminf, ai-drug-discovery, general |
| `pk` | P. | **third profession** (after Art Historian, TA) | digital-art, art-research, general |
| `example` | Alex Example | template | general, founding-engineer, healthcare |

## The two operating modes (read this before interpreting vj/pk reports)

**Targeted match (lj, kj):** the principal knows their profession. LJ is patient #0: both
the developer of LoveWork and the first person to whom LoveWork is applied. KJ
is K., and his profession is chemist; LoveWork is helping him find the best
chemistry/computational-chemistry/ML-for-science role, not choose a profession
from scratch. A GO means "apply today".

**Emergent profession (vj, pk):** the profession is *not yet chosen*. VJ is
V. and the first non-LJ user of the system. He is looking for a job, but the
higher-order task is discovering the profession that job should instantiate. The
branches in `possibilities.md` are *seed directions for the crawl*, not a fixed
target list. The goal is for LoveWork to grow a thicket of plausible listings
across these (and adjacent) directions; clusters of listings that fit the
principal — and that the principal fits — will emerge from the thicket. **Those
clusters are the principal professions.**

For vj/pk the matcher is told (via `soul.md` Decision Heuristics + Notes) to
lean toward MAYBE over KILL — a wider net produces better clusters at this
stage. A GO for vj/pk means "this is strong data about a possible profession",
not necessarily "apply today". Manual cluster review across many MAYBEs is the
intended workflow, not single-match optimisation.

This framing — *"find a profession, not a mere job"* — is the single most
important thing to internalise about vj and pk. For VJ specifically: he is
looking for a profession and a job; do not collapse the task to ordinary job
matching. It is stated at the top of
both their `soul.md` and `possibilities.md` files.

### Family context (why "profession" is the framing)

- **LJ** — by profession a computer scientist (CompSci), then quant research
  and trading (QR, QT), now changing back to artificial intelligence (AI/ML).
- **KJ / K.** — chose the profession of a chemist (Chem).
- **PK** — chose art historian (ArHi), then added teacher (TA), now exploring a third.
- **VJ / V. — ???** Profession still to be chosen; also looking for a concrete job while that profession emerges.

## What goes in each file (template)

See [`../profiles/example/`](../profiles/example/) for a working anonymised template. The shape every profile follows:

```
profiles/<name>/
├── soul.md            # identity, wants, avoids, role types, heuristics
├── work_auth.md       # visa/location rules → drives pre-LLM hard-kill
├── bio-long.md        # Layer 1 (long path): full past→present CV
├── cv-short.md        # Layer 2 (tip): current highest-SNR short CV
├── possibilities.md   # Layer 3 (branching): future directions + matcher signals
├── roles/*.md         # role-specific criteria (one selected per run)
└── (optional, lj-only)
    ├── company_pages.yaml      # curated keep-list with re-crawl cadence
    ├── harnham_searches.yaml    # Harnham recruiter search URLs
    ├── linkedin_seeds.md        # LinkedIn search URLs (auto-populated by gmail_lj_jobs)
    └── linkedin_needs_auth.md
```

### The `matcher signal:` convention

Every branch in `possibilities.md` ends with a line like:

```
**matcher signal:** GO for early-stage AI labs; MAYBE for generalist startups.
```

The matcher reads these as instructions, not prose. When a role aligns with a
branch, the matcher adds +1 to the score and notes the branch letter in the
reasoning (e.g. `"(a)"`, `"(g)"`). See [chapter 05](05-matcher.md).

## Adding a new principal

1. Copy `profiles/example/` → `profiles/<name>/`.
2. Replace the contents of each file with the principal's data. The 3-layer
   model is mandatory; `soul.md` and `work_auth.md` are mandatory.
3. Capture `possibilities.md` from the principal's own words (interview them,
   or paste their notes — the LJ/KJ/VJ/PK files all started as raw notes).
4. Add at least one `roles/<role>.md`.
5. Test it loads: `load_profile_text('<name>', '<role>')` returns a string with
   all 5 sections.
6. Add tests in `tests/test_config_profiles.py` mirroring the kj/pk ones.

## What's next

- [`05-matcher.md`](05-matcher.md) — how the profile drives a score.
- [`07-operations.md`](07-operations.md) — running with `--profile <name>`.

## Profile time and history (planned, 2026-09-13)

An AsOf judgment needs the effective profile the decision consumed, including
the selected role, assembled text and any consulted bio/history. A hash alone
cannot supply those bytes. [Chapter 26 §3](26-asof-strata-history.md#3-l0-hard-linked-strata-with-a-consistency-protocol)
pins run inputs before execution; later edits create new versions. A present-day
profile used on old adverts is an explicitly labelled counterfactual. Inferred
preferences remain evidence-linked proposals under [chapter 27 §6](27-agent-ergonomics.md#6-accretion-each-encounter-should-improve-the-next-one),
with principal scope and knowledge time; they never silently rewrite old inputs.
