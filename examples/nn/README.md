# The example principal — N.N.

`NN` is *nomen nescio* — "I do not know the name", the Latin placeholder for an
unnamed person. LoveWork's public story needs a worked example that can be read
and shown without exposing a real person's record, so this directory carries one.

**What is here.** A complete profile contract — `profiles/nn/` — and the shape
of the state it produces (`index.md`): 10 GO and
20 MAYBE listings from LoveWork's own packs and leads.

**What is withheld, and why.** The profile is built from a real principal's
files with the person renamed, the firms replaced by stable pseudonyms, and
contact details replaced with `example.invalid`. The live case material — the
dossiers, advert text, tailored CVs and letters, and the decision ledgers — is
not published: it is third-party text and one person's live career record.
The example keeps the *contract* and the *method*, not the evidence.

**What it demonstrates.**

- the three-layer profile (`bio-long.md`, `cv-short.md`, `possibilities.md`)
  plus `soul.md`, `work_auth.md`, `voice.md` and role criteria;
- a search configuration a principal actually edits — company list, market
  tracker, job-board queries, LLM choice — with no secrets in it;
- the verdict distribution produced by a real run, including the honest DROPs.

Rebuild procedure: `scripts/build_example_profile.py` in the engine (private).
It is deterministic: the same corpus gives the same pseudonyms, so a diff is a
diff in the world, not in the mapping.

_No file here was written by hand except this one and the pack illustration._
