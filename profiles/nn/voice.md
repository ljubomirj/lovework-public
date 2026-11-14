# Voice — how NN writes when he writes (application letters and CVs)

> Evidence base: the 2026-10-16 Kestrel Partners Systems A/B (case
> `20261011-Kestrel Partners Systems-Technical_CSMs_Product_Managers_AI_Research_Engineer-LoveWork`).
> LoveWork produced a formal ~600-word letter and a thematically re-grouped CV; the
> principal produced the final versions. The principal judged his own better. The diffs
> below are the lessons. This file is operative: an agent drafting application material
> for lj follows these rules BEFORE its defaults.

## Letters

1. **~150 words, one screen.** The CV carries the credentials; the letter carries the
   person. Nothing in the letter restates the CV.
2. **Open with why-them, in plain words** ("you looked like makers/doers"), never with
   a restatement of the advert or a thesis about fit.
3. **One serendipity hook from the case's own provenance** — "LoveWork found you while
   crawling (source: HN)". Read the dossier's crawl-provenance block: the discovery
   story is a fact only this principal can claim, because his own product did the
   finding. Use it when it's true.
4. **Name zero past employers.** At most one concrete current-work fact, with model and
   infrastructure named ("adapting Qwen3-Omni-30B-A3B on a Modal box"). "A tiny UK
   startup" beats the brand name — self-deprecation reads as confidence.
5. **First person, contractions, short sentences, one joke at most.** Personal anchors
   (an all-time favourite tech talk, the car he'll have to replace) are welcome when
   true — mine them from `soul.md` / `README.NN` / conversation; never invent biography.
6. **No gap-confession paragraphs.** Honesty lives in the pack dossier (the "Named
   honestly" block) where the screener's trust questions are answered; the letter
   trusts the reader. Defensiveness is a cost the letter should never pay.
7. **End with genuine, specific curiosity** about what the team is actually building,
   naming their stated areas — curiosity, not a proposal deck.
8. **Ground every reference in THIS advert.** A personal tangent may stay only if
   bridged to something the advert actually says (robocars → their "self-driving
   product"). Unbridged tangents read as a letter written for someone else.

## CVs

9. **Reverse-chronological, one continuous section.** No thematic re-groupings ("the
   foundation years", "recent: models and agents") — the principal flattened them.
10. **No meta-commentary in the artifact:** no italic provenance notes, no "named
    honestly, not asserted" blocks, no generator footers. Meta belongs to the pack
    files, never the artifact a human reads.
11. **Concrete over polished:** real tool inventories including the unfashionable ones
    ("ancient assembly"), real hardware ("dedicated datacenter boxes"), and numbers the
    principal stands behind ("PhD + 25 years" — his arithmetic, not the agent's).
12. **Reuse the principal's own sentences verbatim where they exist** (from
    `cv-short.md` / `CVs/cv*.md`). In the A/B, what survived his edit was what was
    already his phrasing; what he cut was agent-added structure. Every rewritten
    sentence is a downgrade risk.

## Deliverable shape

13. **Ship the pair:** `cover-letter.md` (the short, in-voice letter) and
    `cover-letter-long.md` (the argument map: requirement→evidence, gaps, location).
    The long one is interview preparation, not a submission. Drafting both costs
    nothing and the principal chooses.
14. **Draft to be edited, not to be final.** The principal edits everything; the
    edit-diff is the training signal (see the diff-capture proposal, D-note in the
    pack). An agent's job is a strong first draft in-voice, not a finished artifact.

## Verbatim samples (Kestrel Partners Systems 2026-10-16 — the register to match)

> I apply b/c you looked like makers/doers. LoveWork (github.com/nn/lovework-public)
> - my current labour of love - found you while crawling (source HN) and cycling.
> Surprised you've got AI research. I've done speech (ASR, TTS) and language (NLP),
> curious to see what's vision like in ML/AI. [...] I'm currently doing ASR STT on
> in-house speech and text for a tiny UK startup (adapting Qwen3omni-30B-A3B on a Modal
> box). Like that but still curious what do you guys do.

Rendered final: `state/nn/applications/20261011-Kestrel Partners Systems-...-LoveWork/cover-letter.md`;
the losing formal draft is preserved as `cover-letter-long.md` in the same case.
