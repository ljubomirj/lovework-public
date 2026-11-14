# Chapter 45 — The two closing piles: GONE and EXPIRED

> **Audience:** builders + LJ. **Status: BUILT 2026-10-14** (`retire.py`, wired
> into the tick's maintenance phase). LJ approved this division and answered the
> four open questions: a **45-day horizon with a 15-day half-life** (three
> halvings, value ⅛), **a GONE pack stays browsable**, **"applied" means a real
> submission record** (*"not a mere application pack that was never followed on"*
> — measured: 1 `SUBMITTED` against 452 `PREPARED`), and **the retire pass runs in
> the tick**. He also asked for the existing corpus to be culled: *"we don't want
> high number of GO-s or adds necessarily — we want high SNR, high quality adds"*.
>
> **One thing changed, and then changed back.** §3.4 proposed a move map. The
> first build left a **relative tombstone symlink** at the old path instead, on the
> grounds that a link cannot drift from the truth — and it broke the principal:
> **`git add` refuses a path beyond a symlink**, so his staging list died on the
> first retired pack (*"fatal: pathspec … is beyond a symbolic link"*, 2026-10-14).
> The built version therefore writes the move to `state/<p>/cache/moves.json` and
> the serving layer walks that map, keeping the tree ordinary. **A link is not a
> move, and a design is paid for by whoever uses the tree, not by whoever reads
> it.**
>
> **The first cull (2026-10-14):** lj 177 packs → `gone/`, 1,500 judged adverts →
> `expired/`; vj 60 / 540; kj 23 / 0. 1,677 tombstones, every moved pack intact,
> and 6,577 wiki entries (DROP 5,054, FLAG 876, MAYBE 343, GO 304) left the
> decision space for the piles' indices.
>
> **Companions:** [43](43-the-lovework-loop.md)
> (the loop), [26](26-asof-strata-history.md) (sealed history), [27](27-agent-ergonomics.md)
> (one authority per fact).

## 1. The question LJ asked

> *"All adverts at the end will end up in the EXPIRED pile — right? As they exist,
> we keep checking, whether we applied or not — they will expire. Either because
> they no longer are open, or the web page 404-s. Or even if that does not happen —
> but we track them for such a long time, they are open, then after some time we
> drop them as stale and no longer check them? How do we organise that?"*
>
> *"Ads that were never a GO — we check, they expire, we move them to the EXPIRED
> pile. Once in EXPIRED they stay in EXPIRED … the material we collected is not lost."*
>
> *"What should we do where we took effort, maybe collected data, maybe even applied
> — and they are no longer open? … How about having a separate category GONE — the
> GO-s that expired."*

Both instincts are right, and for the same reason: **what a reader wants from a
closed advert depends on what we did with it.** An evaluated lead that closed is a
record; a pack we prepared, investigated and possibly applied to is *history with
our own work in it*, and mixing the two destroys the only thing that makes the
second one worth keeping.

## 2. What the system already knows (measured 2026-10-14)

| Fact | Where it lives today |
|---|---|
| Is the advert still listed? | `state/<p>/cache/jobs.csv` lifecycle: `new`, `still_open`, `long_lasting` (>30 days), `disappeared`, `not_listed_in_scope` |
| Is the advert still reachable, and does it still say the same thing? | `state/<p>/cache/link_checks/*.json` — dated verdicts: `ok`, `live_unmatched`, `unreachable`, `expired`, `work_mode_conflict`, `skipped` |
| What did we decide? | the wiki org-page blocks: GO / MAYBE / FLAG / DROP |
| Where is the material? | `state/<p>/applications/` (GO packs, 637 lj / 245 vj / 74 kj) and `state/<p>/leads/` (judged adverts, 3,323 for vj) |
| What did the principal do? | `dataset/outcomes.jsonl`, the pack's own `LoveWork status: PREPARED / SUBMITTED` marker |

So the *signals* exist; what does not exist is a place for closed adverts and a
rule for when they go there.

## 3. Proposal

### 3.1 Two areas, one index each

```
state/<p>/gone/      # adverts that were a GO and are now closed: our own work
  index.md           # append-only; never re-processed
  <same dir name as the pack, moved whole>
state/<p>/expired/   # adverts that were never a GO and are now closed
  index.md
  <same dir name as the lead>
```

Both published on the dashboard beside `applications/` and `leads/`, both
append-only: once an advert is in a pile it is **never checked again** — that is
the whole point of the pile.

### 3.2 Which pile, and why

| The advert was… | Closed → | Why |
|---|---|---|
| **a GO** — we prepared material, maybe investigated, maybe applied | **`gone/`** | the material is the value, and "what happened to the ones we wanted?" is a question worth answering later |
| **MAYBE / FLAG / DROP** — we judged it and did not pursue | **`expired/`** | a record that we looked; nothing to revisit |

The rule is **provenance, not score**: it is the same distinction the wiki already
makes between a `PACK` (material exists) and a `LEAD` (we judged it).

### 3.3 When an advert closes

Two reasons, both recorded in the index row:

- **`closed`** — the link re-check says `expired`, `unreachable` (404/DNS), or the
  page no longer carries the role (`live_unmatched`); or the registry says
  `disappeared`. For the false-positive case — a site that blocks automation —
  the row records `unreachable (bot-blocked?)` and the advert stays for one more
  check before retiring, because "we could not read it" is not "it is gone".
- **`stale-but-open`** — the advert is still there after the horizon (proposed:
  **45 days** from first seen, against the matcher's 10-day decision freshness and
  the audit's 25-day drop) and we did not pursue it. It retires with that reason
  written down, and the index says so: *a still-open advert we chose to stop
  checking is a decision, not an observation.*

### 3.4 What the move looks like

1. **The whole directory moves**, as LJ asked: `applications/<pack>-LoveWork/` →
   `gone/<pack>-LoveWork/`, `leads/<slug>/` → `expired/<slug>/`. The dossier, the
   tailored material, the investigation and the draft all travel with it — nothing
   collected is lost, and no file is rewritten.
2. **A row is appended** to the pile's `index.md`: date retired, org, role, advert
   URL, why (`closed` / `stale-but-open`), and the provenance we already have
   (decision, score, whether material exists, whether the principal applied).
3. **A move map** (`state/<p>/cache/moves.json`, the same pattern as the
   org-rename map from 2026-10-07) records old path → new path, and the serving
   layer resolves it, so **every existing link keeps working** — the wiki's GO rows,
   the lead pages, a bookmark in the dashboard.
4. **The wiki stops showing them as available.** A closed GO leaves the GO
   listings and its count moves to a `GONE: N — [the index](gone/index.md)` line in
   the Counts block; the org page keeps the block it always had (the history is
   the org page's business) with its lifecycle marker saying `closed`.

### 3.5 One authority per fact, still

- The **registry** stays the authority for *what happened to the advert*.
- The **index** is the authority for *where the material went and why*.
- The **pile** is the authority for *what we no longer check*.
- Nothing new is invented: the retire pass only reads the registry and the
  link-check verdicts, and writes a move plus a row.

## 3.6 Consistency with what the house already says

The [advert storyboard](advert-lifecycle.html) already gives the two clocks this
chapter depends on, and the design uses its words rather than inventing new ones:

- **The advert's clock** — registry states `new` → `still_open` → `long_lasting` →
  `disappeared` (plus `not_listed_in_scope`, *absence as data*). `disappeared` is
  the primary `closed` signal, and `long_lasting` is why a long-lived advert is a
  signal *about the firm* rather than a recommendation.
- **The application's clock** — `quiet` (days 1–14) → `chase` (14–30) → `decayed`
  (30+), with the chance of success halving monthly after day 30. That vocabulary
  is about *the application*; this chapter is about *the material*, which is why
  it needs its own piles rather than another word in that scale.

The proposed 45-day retire horizon sits deliberately between the matcher's 10-day
decision freshness, the audit's 25-day drop, and the storyboard's day-30 decay
start: by then an advert we did not pursue has been judged twice and refused
once, and continuing to check it is cost without decision.

## 4. Open questions for LJ

1. **The stale-but-open horizon.** 45 days? Or should a still-open advert we did
   not pursue simply stay in MAYBE (and out of the checking set) instead of
   moving? I lean to moving it: one pile, one rule, and the index says why.
2. **Does a GONE pack stay browsable as a pack?** I propose yes — read-only, with
   its `application_pack.md` still served, because "what did I send them?" is a
   question that comes back.
3. **Applied vs prepared in GONE.** The pack already carries
   `LoveWork status: SUBMITTED <date>` when the principal applies; the index row
   should carry it too, so the pile answers "did I ever apply to this company?"
   at a glance.
4. **Where does the retire pass run?** I propose the hourly tick's maintenance
   phase — it is cheap (a registry read plus a link-verdict read) and idempotent,
   which is the same reasoning that moved the morning passes there today.

---

## 5. The third area: LEADS, review-ruled (2026-10-18)

LJ, 2026-10-18, after the first draining re-read sweep (45 reviews, 42 moved):
*"So DROP should be acted on and that lead moved from applications/ to leads/
and the index updated. No information should be lost. The application pack
stays, everything is preserved, nothing is removed. Only the entry — the
entire directory — moves from applications/ to leads. We are after increasing
SNR of the applications/, honing the applications to be maximally useful. One
can't easily apply to 10 places. … That we have to move a lead b/c it was
wrongly judged 7.7 whereas now it's judged 0.0 — that's a plus, not a minus."*

**The distinction that keeps the areas honest:** `gone` and `expired` close
because the **advert** closed or went stale — the world changed. A review-ruled
DROP closes because **our reading** changed: the re-read scored the advert
itself and the GO did not hold. `leads/` already held judged adverts (it is
where never-GO material lives, docs/45 §2); a re-judged GO is a judged advert
that happens to carry our own pack, so the directory moves there whole.

**What ships** (`retire.retire_review_ruled`, riding the retire pass the tick's
maintenance already runs hourly — no new job):

- **The authority is the folded review ledger** (`loop/reviews.jsonl`, newest
  row per pack). A row with `new_decision: DROP` and a material move is an
  enactment instruction; a newer row that says otherwise wins (the reversal
  path is a new review plus moving the directory back).
- **Only DROP moves.** A MAYBE/FLAG re-read is a downgrade, not a closure —
  the pack stays in `applications/` and the board carries the move.
- **The move is the same mechanics as the piles**: `os.rename` whole, a
  `moves.json` row (the serving layer resolves old paths), an append-only
  `leads/retired.jsonl` ledger row whose `closed` reason records the scores
  and the model, and a `leads/index.md` page. Nothing is deleted.
- **Retirement is real**: `_effective_rows` reads the `leads` ledger beside
  `gone`/`expired`, so `retired_keys` — and therefore the view, the index and
  every builder that asks — treats the advert as out of the decision space.
  The index is rebuilt after any enactment (the stale-index class the repairs
  closed on 2026-10-17).
- **The URL, not only the name**: the first enactment exposed that a pack
  directory whose name truncates a long role (`…Modelin`) folds to a different
  `(org, role)` key than the block's full title, so `current_view` now also
  drops URLs in `retire.retired_urls` — the identity the audit already used
  for exactly this mismatch.
- **The board owes the reader what left**: `loop/hone-board.md` derives an
  *Enacted* section (review rows whose pack now sits under `leads/`) — derived,
  never a second ledger.

CLI: `retire.py --profile <p> --review-only [--dry-run]`; the bare invocation
the maintenance pass runs enacts review-ruled DROPs beside the closed/stale
sweep. First live enactment (lj, 2026-10-18): **8 packs → leads/** (Cantina
7.7→0.0, Cohere 7.7→1.7, BOLD Lab 7.9→3.4, Metcor, Omnea, Charly, Planlab.ai,
Sundream Studio), GOs on file 46 → 38, both old and new dashboard paths 200.

## 5b. The third reason a case leaves `applications/`: it can never be prepared (2026-10-19)

LJ, 2026-10-19, on a vj GO whose pack front page said *"Tailored CV: not prepared
yet / Cover letter: not prepared yet"*:

> *"either application pack is created => remains on GO; or if we fail to create an
> application pack, we retry; but if not possible to create a pack b/c the advert is
> gone and we no longer have it => then this should not be in GO, should be in
> leads."*

**Three closing reasons now, and they are distinct** — this is the discipline the
whole chapter rests on:

| reason | who changed | implementation |
|---|---|---|
| `gone` / `expired` | the **world** (the advert closed, or went stale) | `retire.py` |
| review-ruled DROP | **our reading** (the re-read scored the advert itself) | `retire_review_ruled` |
| **unpreparable** | **nothing can be done** (no advert URL and no retained text) | `retire_unpreparable` |

**The condition is deliberately narrow**: no advert URL **and** no retained advert
text. A pack that still carries a link is *work* — the loop retries it every tick —
and a link that is dead is the link check's and the review's to judge. Moving those
here would retire adverts whose only problem is that nobody has fetched them yet.

Measured at birth (dry run, 2026-10-19): **vj 4, kj 10, lj 12** — 26 GOs whose packs
recorded `Primary advert URL: not retained` and could never be prepared.

It rides the same hourly retire pass as the review sweep (the bare `retire.py` the
tick's maintenance runs), so nothing depends on someone remembering — and
`restore()` still reverses it if a URL is recovered.

**The rule it serves, in one line**: a GO is a promise that prepared material is
waiting; when nothing can fulfil it, the honest row is a lead.

CLI: `retire.py --profile <p> --unpreparable-only [--dry-run]`.

**The redirect signal that feeds GONE (2026-10-18, same day).** The Together
AI case gave the closed pile its strongest input yet: a greenhouse advert that
closed **server-redirects to the employer's board with `?error=true`** — the
employer's own machine-readable claim, where the "no longer open" banner is
client-side and invisible to any text fetch. `link_check.closed_redirect`
recognises the signature (`ATS_CLOSED_REDIRECTS`, one row per ATS as the
conventions are met), `check_url` records the verdict `expired`, and the
retire pass's closed signals do the rest — GO → GONE with no human in the
loop. `review_go` asks the same question before re-scoring: a page a closed
redirect answers is the *board*, not the advert, and the first live encounter
measured exactly the trap — a confident 6.1 MAYBE scored on a 78-job index.
The board itself is not lost knowledge: the employer stays in the market
tracker with its `➟ Careers:` line, and `market_players` crawls that page
every sweep — the dead advert's redirect is, in effect, a pointer back to the
live source of future leads.
