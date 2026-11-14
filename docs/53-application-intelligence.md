# 53 — Application intelligence: what is general, and where the principal's data lives

**Status:** built 2026-11-10. The data lives with each principal
(`lovework-principal-<p>/knowledge/`); this chapter holds what is general — the
vocabulary, the patterns that hold for any principal, and the checks.

**The rule** (LJ, 2026-11-10): *data derived from a principal's record belongs to
that principal's repo; the method, schemas, prompts and checks are general and
belong to the engine.* A single-principal deployment holds everything about its
principal in one repo and nothing about them in the code.

---

## 1. The vocabulary

Every case carries exactly one outcome, and the outcome states what the evidence
**proves**, not what was attempted:

| Outcome | Proves | Does not prove |
|---|---|---|
| `applied` | a submission receipt, a confirmation page, a sent mail | that the form was filled |
| `rejected` | a rejection message from the employer or its ATS | that the application was read |
| `interview` | an invitation or a scheduled conversation | a warm "let's chat" from a contact |
| `offer` | offer language, a signed agreement | a submitted application |
| `prepared` | material exists (letter, tailored CV) | that anything was sent |
| `unknown` | nothing was recorded | a no |

A filled form with no receipt is `prepared`. Silence is `unknown`, never `no`.
One directory is a fixture when its name or company matches a test pattern, and a
fixture is excluded from every count (it stays in the data, flagged).

## 2. What holds for any principal

These are the patterns the 2026-11-10 extraction found across 669 real cases on
two hosts. They are principal-neutral: each is a property of the pipeline, not of
LJ.


### 2.1 The pipeline marks its own work

- Every real pack in the shard (59/59) still carries the '_not yet investigated_' placeholder in its Company & hiring reality block, while 49 of the 60 companion dossiers have a filled 'Status: investigated ...' section.

- 10 PREPARED cases have no retained advert text in the pack ('_No advert text was retained for this lead._'), including Anaplan, Checkout, Gradium, Neo4j, Cosine, Harnham and Monzo.

- All 10 later advert reviews in the shard moved the score down, from -0.5 to -3.5 (mean -1.64), and every one crossed a tier (GO to MAYBE or FLAG).

- Hard eligibility rules (on-site with zero remote, US-only, non-allow-listed country, staleness) are decisive in most reviews but are not enforced at scoring: Gradium stays GO 9.0 APPLY_NOW for a Paris on-site role, Cosine 7.6 for a five-day office role, R1 RCM 6.1 with material for a US-based position.

- The only real application outcome in the 60-case shard lives in a case .txt: an IRIS Principal Engineer application (2026-10-21) and rejection (2026-10-23), inside a case directory whose pack and dossier are about Dwelly and never mention IRIS.

- 51 of 59 packs cannot name the application route: the 'Application form' line says 'not named in the advert — apply from the advert link above'.

- 5 of 60 packs have no 'LoveWork status:' line, so a reader that matches the documented prefix sees no state at all.

- Two Dex cases a day apart share LinkedIn job id 4477627553: the 2026-10-11 copy holds the advert text but is CUT, while the 2026-10-12 copy has no advert text and is PREPARED with a cover letter.


### 2.2 The anti-patterns that waste the principal's time

- A job-alert digest was turned into a company and a case: the Gmail subject '30+ new jobs match your preferences' became the organisation and a job title was stored as the location; it received GO 9.0.

- Application case files can contain live credentials and private contract documents; an extraction or agent pass must never copy them into summaries, knowledge items or logs.

- Never let an agent invent or reconstruct a credential, PR number, metric or project internal to fill a CV gap.

- Outcome evidence must be about its own case: a filing page is not a receipt, a receipt must name its own organisation, a blank form line is not a value, an empty alias matches everything, and only an applied case can be rejected.

- Treating every directory under applications/ as an application is wrong; some contain only a saved advert.

- Generated letters can make first-person claims the principal never made: the Castaigroup letter says 'I edited it' and 'company checked' in a case whose pack block says the company was not investigated.


### 2.3 What a human agent would not do

The test that motivated this chapter (LJ, 2026-11-10): a human agent would not
spend the principal's time on a case that has no route to success. In the record,
the recurring shapes are:

- **Drafting against a closed or stale advert** — the closure is written into the
  dossier and the pack is built anyway;
- **Drafting a role the principal cannot take** — a country, an on-site week or a
  work-authorisation clause that the review noticed and the pack ignored;
- **Preparing material the principal never sends** — 410 prepared, 60 applied; the
  bottleneck is the send, not the supply;
- **Sending a letter with the agent's working notes inside** — the generator footer
  and the gaps annex travel with the sendable file;
- **Treating volume as progress** — a pack count is not a job.

### 2.4 The audit that motivated the last ten checks

LJ, 2026-11-10: *"an HI would not waste principal's time … with some of those
examples, as not being conducive to any chance of success."* The 48 GO cases in
lj's wiki were read the way a human agent would read them — pack, dossier and
advert, against the principal's own soul, work-auth, CV and possibilities:

| Verdict | Cases |
|---|---:|
| DROP | 30 |
| HOLD | 10 |
| CONSOLIDATE | 4 |
| SEND | 4 |

**44 of 48 GOs were not worth the principal's time** (=30 DROP + 10 HOLD + 4
CONSOLIDATE), and the failures were not scoring errors: 36 had no usable
compensation band, 27 had a pack that could not be sent, 18 had an advert that was
closed, stale or not an advert, 16 were ineligible on location or authorisation,
14 named a company that was an ATS slug or an agency. The audit's per-case verdicts
live with the principal (`lovework-principal-example/knowledge/audits/`); the rules they
produced are C36–C45 above. The checkable subset is `go_sanity.py`.

## 3. The checks

Each row is a predicate a program can evaluate, the module that would run it and
the point at which it must stop and hand the decision to the principal. They are
the general form of the lessons; the per-principal data is the evidence they were
derived from.


| ID | phase | predicate | owner | command or file | stop rule |
|---|---|---|---|---|---|
| C01 | after-sending | every case with `outcome == "applied"` carries at least one `outcome_evidence` quote matching `/(successfully applied\|thank you for applying\|application (was \|has been )?(received\|submitted))/i`; a case failing it is re-coded `unknown`. | agent | `outcome_join.py / snapshot.py` | never let a status line stand as an outcome |
| C02 | after-sending | a case whose only outcome evidence matches `/^(LoveWork status: )?(PREPARED\|CUT\|scaffold)/` contributes zero to `applied`, `rejected`, `interview` and `offer`, and its pack is reported as PREPARED only if the material pair exists, the pack has no generator footer and no gaps annex, and the advert is live; a pair failing any of those is reported as MATERIAL_DRAFTED, never PREPARED. | agent | `outcome_join.py / snapshot.py` | never let a status line stand as an outcome |
| C03 | after-sending | outcome labels validate against `{prepared, applied, rejected, interview, offer, unknown}`, and host-a's `rejection` label (16 of its 201 manual rows) is normalised to `rejected` before any ratio is computed. | agent | `outcome_join.py / snapshot.py` | never let a status line stand as an outcome |
| C04 | after-sending | a case with two differing run codings is emitted with both values and both quotes, and the section totals equal an exact count of `cases-final.jsonl` (410 prepared / 60 applied / 16 rejected / 2 interview / 2 offer / 179 unknown of 669), never a rounded or averaged one. | agent | `outcome_join.py / snapshot.py` | never let a status line stand as an outcome |
| C05 | after-sending | a case whose evidence contains a start date or a signed agreement is coded `offer`, not `interview`; an offer case records `accepted` or `declined`, and the record has exactly one accepted offer (Talk Machine) and one hire. | agent | `outcome_join.py / snapshot.py` | never let a status line stand as an outcome |
| C06 | before-scoring | the dossier's `Initial decision` / `Recommended action` is GO / APPLY_NOW only if no substring of its `Risks:` block matches the disqualifier set (workplace outside the allow-list, `location_type == "on-site"` with no remote/hybrid signal, citizenship or sponsorship required, advert age ≥ 25 days, level mismatch, absent advert text). | agent | `matcher.py pre-LLM hard-kill` | never rank a role whose own text excludes the principal |
| C07 | before-scoring | `outcome in {"applied","interview","offer"}` implies `eligibility == "pass"`, and `prepared` is exempt from it — the Gradium Paris pack is `prepared` on a failed country gate, which is the defect this section measures, so eligibility is an input to sending, not to drafting. Requires a new field `eligibility` (`pass` / `fail` / `unknown`) on the case row; no such field exists in the engine or in `cases-final.jsonl`. | agent | `matcher.py pre-LLM hard-kill` | never rank a role whose own text excludes the principal |
| C08 | before-scoring | `advert_text` matches `/authori[sz]ed to work\|right to work\|visa\|sponsorship/i` implies `answers.md` exists in the pack with a keyed answer to that question. | agent | `matcher.py pre-LLM hard-kill` | never rank a role whose own text excludes the principal |
| C09 | before-scoring | a case is queued for a new pack only if `case.date - max(outcomes[org_name == case.organisation].date)` exceeds 9 months, or 12 months when the role slug matches. | agent | `matcher.py pre-LLM hard-kill` | never rank a role whose own text excludes the principal |
| C10 | before-scoring | `location_type` is non-null and in `{"remote","hybrid","on-site"}` at both crawl and recheck, and the dossier's decision is not GO when it is `"on-site"` and the workplace is not in `{UK, MK}`. | agent | `matcher.py pre-LLM hard-kill` | never rank a role whose own text excludes the principal |
| C11 | before-drafting | no dossier carrying the `PREPARED` marker has an advert review of `gone`, `unmatched` or `bot-blocked` (10 of the 60 packs in host-a's audit violate). | agent | `link_check.py / retire.py` | never draft against a closed advert |
| C12 | before-drafting | every pack visible to the principal has `today - last_advert_check <= 10 days`, with `last_advert_check` read from `link_check`'s `checked_at` (`cache/link_checks/*.json`); at `> 25 days` the retire pass moves the pack from `applications/` to `leads/` and writes a `retired.jsonl` ledger row — the pack status stays `CUT`, `PREPARED` or `SUBMITTED`, because there is no `retired` status. | agent | `link_check.py / retire.py` | never draft against a closed advert |
| C13 | before-drafting | `advert_text_retained == false` implies the pack is marked `CUT`, never `PREPARED` (16 of the 23 such packs violate) and never decided `GO` (7 of the 23 violate); `PREPARED` is a status and `GO` a decision, so the two counts are separate, not 23 violations. | agent | `link_check.py / retire.py` | never draft against a closed advert |
| C14 | before-drafting | for every pack, the dossier's `Decision:` line and its advert-review verdict agree — a `link_check` verdict other than `ok` (`expired`, `live_unmatched`, `unreachable`) demotes the decision in the same record. This is proposed, not enforceable today: the verdict lives in `cache/link_checks/*.json`, the decision in the dossier, and no case field carries the join; supporting it needs an `advert_review_verdict` written into the case schema. | agent | `link_check.py / retire.py` | never draft against a closed advert |
| C15 | before-drafting | no case has registry status `still_open` while its two most recent `link_check` verdicts for the advert carry `status == 404` (`cache/link_checks/*.json`; `fetch_attempts.jsonl` records `ok`/error, not the HTTP code). | agent | `link_check.py / retire.py` | never draft against a closed advert |
| C16 | before-sending | the body of `cover-letter.md`, excluding any gaps/needs block and any footer, is ≤250 words — the defect line, not the ceiling: 251–550 words is legal but over `voice.md`'s ~150-word target, and a file over the 550-word cap (`pack_completion.py:236`, `COVER_LETTER_WORDS = 550`) is regenerated rather than shipped. | agent | `application_packs.pack_readiness` | never send a letter with an internal annex |
| C17 | before-sending | `cover-letter.md` contains no heading matching `^#{1,3} .*(Gaps\|Needs the principal\|Honest gaps)`. | agent | `application_packs.pack_readiness` | never send a letter with an internal annex |
| C18 | before-sending | `cover-letter.md` contains no line matching `Prepared by LoveWork\|Generated by\|Not submitted`. | agent | `application_packs.pack_readiness` | never send a letter with an internal annex |
| C19 | before-sending | `cover-letter.md` contains no employer name from the profile's past-employer list. | agent | `application_packs.pack_readiness` | never send a letter with an internal annex |
| C20 | before-sending | pack status is PREPARED only if the pack's material files exist and carry no generator footer and no gaps annex, and the advert is live; a pack that holds the pair but fails any of those is reported MATERIAL_DRAFTED, never PREPARED. The prose half is separate and conditional: where the pack retains advert text, the letter's first 100 words contain a contiguous 5-word phrase from it; where it retains none, the pack cannot be PREPARED and is closed as CUT (the advert section's rule, 03), not failed against text that is not there. | agent | `application_packs.pack_readiness` | never send a letter with an internal annex |
| C21 | before-sending | `MATERIAL_DRAFTED` is computed as `cover_letter_exists and cv_tailored_exists` (the file half read through `application_packs.pack_readiness(directory)`), and `PREPARED` only as `MATERIAL_DRAFTED and not generator_footer(cover-letter.md) and not gaps_annex(cover-letter.md) and advert_live`, with the footer read from `pack_completion.FOOTER` (`pack_completion.py:68`); the two are reported as separate counts and never substituted. In this slice the file test matches the label on 60 of 60 cases; the status test passes 0 of 60. | agent | `pack_completion.py` | never claim readiness from file existence |
| C22 | before-sending | a material file is not sendable when its text matches `/^(Gaps the material could not fill:\|Needs the principal:\|Prepared by LoveWork \(agent\))/m`; a distinct sendable copy needs a `sendable` boolean on the material record and a `*-sendable.md` sibling, and neither is in the engine or the case schema today — the `sendable` values in `05-materials-cv-cases.jsonl` are the audit's, not the engine's. All 35 letters and 35 CVs here match the pattern; the 10 records that carry an explicit verdict are all false (=0%). | agent | `pack_completion.py` | never claim readiness from file existence |
| C23 | before-sending | `word_count(letter_body)` is judged on the three-number ladder — over 150 is over target, over 250 is a defect, over 550 (`COVER_LETTER_WORDS`, `pack_completion.py:236`) is returned for regeneration. 16 of the 17 recorded counts in this slice are over 250 and none is over 550. | agent | `pack_completion.py` | never claim readiness from file existence |
| C24 | before-sending | `recommended_action == "APPLY_NOW"` implies `cover_letter_exists and cv_tailored_exists and advert_text_retained`; otherwise the case is queued for completion and not surfaced as actionable (3 of the slice's 13 APPLY_NOW cases have no material at all — Citadel, MeanderX, Veracity — and a fourth, Gradium's Software Engineer, retains no advert text, so 4 of 13 fail the conjunction). | agent | `pack_completion.py` | never claim readiness from file existence |
| C25 | before-sending | `outcome == "applied"` requires `submitted_at is not None or receipt_quote is not None`; a MATERIAL_DRAFTED pack without a receipt never enters a pipeline or a send total. | agent | `pack_completion.py` | never claim readiness from file existence |
| C26 | ledger-hygiene | a pack whose dossier contains "Status: investigated" never renders a §4 containing "_not yet investigated_"; 284 of 394 packs (=72%) violate in the three principal trees (lj 187 of 263, vj 75 of 98, kj 22 of 33). | agent | `application_packs.py / wiki_audit.py` | never overwrite a sealed report or a principal's file |
| C27 | ledger-hygiene | no directory is classified PREPARED while its newest advert review status is in {gone, unmatched, bot-blocked}; 10 of the 60 cases (=17%) violate. | agent | `application_packs.py / wiki_audit.py` | never overwrite a sealed report or a principal's file |
| C28 | ledger-hygiene | a case counted as PREPARED contains both `cover-letter.md` and `cv-tailored.md`, neither file contains the generator footer or the "Gaps the material could not fill" block, and its newest advert review is `live`; a case counted as MATERIAL_DRAFTED contains the two files and nothing more is claimed — 42 of 60 cases (=70%) pass MATERIAL_DRAFTED and 0 of 60 pass PREPARED. | agent | `application_packs.py / wiki_audit.py` | never overwrite a sealed report or a principal's file |
| C29 | ledger-hygiene | no pack says the application form is not named while its dossier matches an ATS host (ashbyhq, greenhouse, lever, smartrecruiters, workable, homerun); 50 of 60 packs (=83%) say the form is not named, and 19 of those 50 (=38%) hold an ATS URL. | agent | `application_packs.py / wiki_audit.py` | never overwrite a sealed report or a principal's file |
| C30 | ledger-hygiene | two pack directories with the same canonical advert identity — ATS job id with tracking parameters and a trailing "/application" stripped — resolve to one case; 12 of 60 directories (=20%) are duplicates or superseded (9 scaffolds, 3 complete packs). | agent | `application_packs.py / wiki_audit.py` | never overwrite a sealed report or a principal's file |
| C31 | after-sending | every row labelled `applied` cites, in its own `outcome_evidence`, a dated send signal — a confirmation string from a named organisation ("you have successfully applied", "application has been received", "submitted successfully"), a confirmation URL, or a `SUBMITTED` marker with a date; a filled form, an apply click or an "Applied N seconds ago" badge is a send, not a receipt, and leaves the row at send state `sent_unconfirmed`; 2 of the 60 `applied` rows carry only that badge today (Mekion, Olivier Bernard). | agent | `history.py / brief.py` | never read silence as a no |
| C32 | after-sending | a case whose only `SUBMITTED` token is the checklist line `- [ ] Submit (then replace the status line above with SUBMITTED and date)` is classified `prepared`, and that token is never read as a marker — 77 of the 82 rows containing the token are in exactly that position — while `NOT SUBMITTED` is read as its negation (1 row, Cato). | agent | `history.py / brief.py` | never read silence as a no |
| C33 | after-sending | the marker and the receipt are tested separately. `submitted_marked` is true only for a `SUBMITTED` status line carrying a date — 2 of 469 LoveWork cases (a16z, PostHog); `receipted` is true only for a message from the named organisation — 1 of those 2 is (PostHog, by its rejection) and 1 is not (a16z), and 1 further case is receipted without a marker (Dwelly/IRIS). A case `.txt` holding a receipt under another organisation is queued as misattributed, not relabelled. | agent | `history.py / brief.py` | never read silence as a no |
| C34 | after-sending | for every case whose two run labels differ (51 of 440 = 12%), a receipt re-read is recorded before the case enters a count, and both labels are kept. | agent | `history.py / brief.py` | never read silence as a no |
| C35 | after-sending | a case with a dated receipt and no reply 14 days later is queued `dormant` with that receipt date, and its outcome is left as the receipt supports; `unknown` is reserved for cases with no receipt at all — 179 of 669 today; a loose scan of their evidence for receipt language hits 13 rows, every hit an artefact (10 the un-ticked checklist line under a `CUT — scaffold only` status, 2 questions addressed to the principal, 1 an application deadline). | agent | `history.py / brief.py` | never read silence as a no |
| C36 | before-scoring | the organisation is the employer's own display name, not an ATS token: a name ending in a digit (`Addepar1`), carrying a domain (`Iwoca.Co.Uk`) or gluing a suffix (`Castaigroupinc`) fails before a case is minted; an aggregator slug must resolve to the hiring company named in the advert's own sentence | agent | `sources/digitaljobs.py` + `go_sanity.py` | never mint a case, pack or wiki page on a slug |
| C37 | before-scoring | case identity is normalised employer + title + location, not the org string: `Wiley` and `John Wiley Sons Inc`, or one opening's three requisition ids, collapse to one row | agent | `go_sanity.py` (duplicate pass) | never queue two GOs for one opening |
| C38 | before-scoring | more than one GO for the same employer within seven days is one approach, not N: emit CONSOLIDATE | agent | `go_sanity.py` | never let one employer see three parallel applications |
| C39 | before-scoring | a remote advert qualified by a place ("Remote Poland", "Remote - Americas") is relocation, not remote; parse the advert's own qualifier against the principal's allow-list | agent | `matcher.py` pre-LLM | never read "remote" as a free pass |
| C40 | before-scoring | a workplace in the United States ("Remote-<ST>-USA", "United States", a US city/state) is a hard kill for a UK+MK principal before scoring | agent | `matcher.py` work-auth kill | never score a US-sited role |
| C41 | before-scoring | an export-control clause requiring "a U.S. person" is an eligibility gate read from the advert body, not inferred from the employer's HQ | agent | `matcher.py` eligibility | never let a defence-adjacent role through on brand |
| C42 | before-drafting | a GO needs a stated band, or a recorded "no band published" with the principal's floor checked; "competitive", "generous", "TBC", "Not Disclosed" is the absence of a band, not a neutral fact | agent | `application_packs.py` / `go_sanity.py` | never draft against a missing band without raising it |
| C43 | before-drafting | a band that belongs to a mid-level req cannot support a principal-level SEND, however good the craft fit — cap at HOLD | agent | `matcher.py` / `decide.py` | never spend principal time on a level mismatch |
| C44 | before-drafting | the apply link must share a registrable domain with the employer (or its ATS) and carry a job path or requisition id; a bare domain root or another company's careers site makes the pack unusable | agent | `link_check.py` / `application_packs.py` | never treat a homepage as an advert |
| C45 | before-drafting | an unretained advert body makes the case HOLD-with-capture, never PREPARED: the pack may not claim requirements it never captured | agent | `pack_completion.py` | never mark prepared what was never read |


## 4. Where the data lives

| Principal | Data |
|---|---|
| lj | [`lovework-principal-example/knowledge/`](../lovework-principal-example/knowledge/) — 523 cases, 2,976 lessons, 967 guidance rows |
| vj | [`lovework-principal-vj/knowledge/`](../lovework-principal-vj/knowledge/) — 109 cases, 397 lessons |
| kj | [`lovework-principal-kj/knowledge/`](../lovework-principal-kj/knowledge/) — 37 cases, 121 lessons |

Each holds `cases.jsonl`, `lessons.jsonl`, `guidance.jsonl` (where the principal
stated one) and `outcomes.jsonl`, with a `README.md` and `stats.json`. The
schemas are in §1; the extraction procedure is
[chapter 54](54-knowledge-extraction.md).

## 5. What this chapter is not

It is not a scoring model: no pattern here tells the matcher what to rank. It is
the *evidence* a scoring model would be calibrated against, and the *checks* that
catch the failure modes before they cost the principal an evening.
