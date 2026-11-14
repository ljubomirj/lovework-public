#!/usr/bin/env python3
"""pack_completion.py — the whole pack, in one context.

`tasks/2026-10-03-pack-completion-workflow.md` is the contract:

- **step 1** an advert review (the advert as it is now, drift, fit),
- **step 2** company/hiring diligence into the pack's reserved block,
- **step 3** tailored material — `cover-letter.md`, `cv-tailored.md`, and
  `answers.md` only when the advert asks screening questions,
- **the checklist bookkeeping**: steps 1–3 ticked, **step 4 (Submit) never**.

LJ, 2026-10-13: *"we should not stop at investigation and leave the tailoring for
later … once we have in context the investigation block is at hand — we should
continue to tailoring. It's a good time to do that then, when things are in
context."* So all three agent steps run in one action, on **two model calls**:
one for the review + diligence (same evidence, same context) and one for the
tailored material (same advert, same profile). The alternative — the 06:00 pack
job at two packs a run — would need weeks for the backlog.

Hard boundaries, from the same document: never submit, email, message or fill a
form; never change the `LoveWork status: PREPARED` marker or the
`Advert identity:` line; never edit outside the pack; never invent a fact about
the principal or the company — `unknown` and `needs the principal` are correct
answers. One earned transition was added 2026-10-18: a scaffold whose honest
marker is `CUT` becomes `PREPARED` when this workflow lands steps 1–3 — the
marker now means what it says (LJ: don't claim PREPARED where nothing was
prepared). SUBMITTED remains the principal's alone.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, Field

import config

logger = logging.getLogger(__name__)

#: The tailored material the workflow names, in its order.
MATERIAL_FILES = ("cover-letter.md", "cv-tailored.md", "answers.md")

#: The two structured calls' token ceiling. 16k was the LAN-model era's number;
#: on the relay with thinking at max (LJ's 2026-10-21 provider ruling) the
#: reasoning eats most of it and the material JSON arrives truncated mid-letter
#: (measured 2026-10-22: Deepgram's cover letter cut at EOF inside the JSON —
#: not empty, cut). 32k covers max-thinking plus a full letter+CV JSON.
PACK_MAX_TOKENS = int(os.getenv("LOVEWORK_PACK_MAX_TOKENS", "32000"))

#: The block the workflow replaces; `application_packs.uninvestigated_packs` gates
#: on the same string, so this module and the queue agree on what "done" means.
NOT_INVESTIGATED_MARKER = "Status: NOT YET INVESTIGATED"

#: The checklist lines this workflow owns (step 4 is the principal's).
#: The checklist lines this workflow owns, in both spellings the corpus uses: the
#: workflow document's wording ("Decide whether to commission …") and the packs'
#: own ("Investigate company/hiring reality …"). Step 4 — Submit — is the
#: principal's and is never matched here.
CHECKLIST_OWNED = ("review advert", "commission", "investigate company",
                   "hiring reality", "tailor cv", "tailor")

FOOTER = ("---\nPrepared by LoveWork (agent) {today} — for review. Not submitted; "
          "the principal\ndecides and sends.\n")

#: The profile files the letter and the CV may draw on, bounded: these are the
#: principal's own words, and nothing else is a legitimate source for material
#: that carries his name.
#: What the drafting prompt is given, **in this order**.
#:
#: `voice.md` leads (2026-10-16). It is the principal's own register, distilled from
#: an A/B on one case — same facts, same advert, two authors — and the file says of
#: itself: *"an agent drafting application material for lj follows these rules BEFORE
#: its defaults."* Leading also means it survives the character budget, because a
#: truncated register is a lost register.
#:
#: **It was not in this tuple until now**, so the automatic drafter never read it: the
#: file existed, AGENTS.md pointed at it, and the one consumer that matters — the
#: prompt that writes the letter — was never told. A profile file the prompt does not
#: read is decoration.
#: The principal's own material, in the order a writer needs it. `public_record.md` is
#: second because the pack material is where a claim about the principal's record gets
#: made: on 2026-10-20 the material step told LJ he had "no public OSS-contribution
#: record" and "no modern realtime/streaming evidence" — both false, written from a
#: bundle that never mentioned his repositories or his streaming work. The record is a
#: profile file now, with its provenance, so the claim has something to argue with.
PROFILE_FILES = ("voice.md", "public_record.md", "soul.md", "cv-short.md",
                 "work_auth.md")

PROFILE_CHARS = 24000


class Investigation(BaseModel):
    """Step 1 + 2: the advert as it is now, and the diligence behind it.

    Every field is optional with a default, for the same reason the tick's own
    investigation schema is forgiving: the LAN model has weaker JSON discipline
    than the relay, and a partial answer that lands its gaps in `unknown` is a
    valid answer — a retry storm is not.
    """
    advert_status: str = Field(default="", description="live | redirected | gone | bot-blocked | unmatched")
    advert_title_now: str = Field(default="", description="the title as it appears now")
    title_drift: str = Field(default="", description="none | what changed since capture")
    level_location_comp: str = Field(default="", description="as advertised, or 'not stated'")
    deadline: str = Field(default="", description="as advertised, or 'not stated'")
    fit: str = Field(default="", description="consistent with the assessment | weaker — why | stronger — why")
    entity: str = Field(default="", description="legal entity, size, stage")
    product: str = Field(default="", description="what the company sells")
    funding: str = Field(default="", description="funding/runway/ownership, or unknown")
    people: list[str] = Field(default_factory=list, description="named company people only")
    hiring_evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    collaborators: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    commission_diligence: str = Field(default="", description="'no — reason' or 'yes — what and why'")
    confidence: str = Field(default="low", description="low | moderate | high")


class Material(BaseModel):
    """Step 3: the tailored material, in the profile's own voice and facts."""
    cover_letter: str = Field(default="", description="≤350 words, addressed to the role")
    cv_tailored: str = Field(default="", description="the profile CV re-ordered and re-emphasised")
    answers: str = Field(default="", description="only if the advert asks screening questions")
    gaps: list[str] = Field(default_factory=list, description="what the material could not support")
    needs_principal: list[str] = Field(default_factory=list)


@contextmanager
def outer_run_id(run_id: str):
    """Set `LOVEWORK_OUTER_RUN_ID` for the model calls, and put it back after.

    Scoped, not a bare assignment: `writer_boundary` reads that variable to decide
    whether this process is the *child* of an operation, and a child skips the
    writer lease. It lives here now because the calls live here — a runner that
    forgot it produced unattributed rows (found 2026-10-13, one test later).
    """
    previous = os.environ.get("LOVEWORK_OUTER_RUN_ID")
    os.environ["LOVEWORK_OUTER_RUN_ID"] = run_id
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("LOVEWORK_OUTER_RUN_ID", None)
        else:
            os.environ["LOVEWORK_OUTER_RUN_ID"] = previous


def profile_bundle(principal: str, *, profiles_dir: Path | None = None) -> str:
    """The principal's own material, bounded — the only legitimate source."""
    profiles_dir = profiles_dir or config.PROFILES_DIR
    parts = []
    for name in PROFILE_FILES:
        path = profiles_dir / principal / name
        if path.is_file():
            parts.append(f"### profiles/{principal}/{name}\n"
                         + path.read_text(encoding="utf-8", errors="ignore"))
    return "\n\n".join(parts)[:PROFILE_CHARS]


def advert_review_block(investigation: Investigation, *, url: str, today: date,
                        verdict: dict | None, org: str = "",
                        principal: str = "lj") -> str:
    """Step 1, in the workflow document's own shape.

    `org` is passed so the review can say **who is advertising**: an advert published
    by a recruitment agency is a different proposition from the employer's own, and
    LJ asked for the distinction (2026-10-15: *"Enigma is a recruiter - not a company
    hiring itself … Presumably we need to distinguish between the org hiring and org
    recruiting?"*). `lead_identity.org_kind` is the classifier; this line is where a
    reader meets it.
    """
    from lead_identity import kind_for_org

    intermediary = ""
    if org and kind_for_org(principal, org, url) == "agency":
        intermediary = (f"Intermediary: **{org}** publishes this advert as a "
                        f"recruitment agency, not the employer — the hiring company "
                        f"may be unnamed, and your application goes through them.")
    fetched = ""
    if verdict:
        code = verdict.get("status")
        fetched = (f"{verdict.get('url') or url} (HTTP {code or 'n/a'}, "
                   f"{verdict.get('checked_at') or 'checked'} by "
                   f"{verdict.get('method') or 'the link check'})")
    else:
        fetched = f"{url or 'not captured'} (no link-check verdict on file)"
    return "\n".join([
        "Advert review — " + today.isoformat(),
        "",
        *([intermediary, ""] if intermediary else []),
        f"Status: {investigation.advert_status or 'unknown'}",
        f"Fetched: {fetched}",
        f"Title on page: \"{investigation.advert_title_now or 'unknown'}\""
        + (f"   vs captured: \"{investigation.title_drift}\""
           if investigation.title_drift and investigation.title_drift != "none" else ""),
        f"Level/location/comp: {investigation.level_location_comp or 'not stated'}",
        f"Deadline: {investigation.deadline or 'not stated'}",
        f"Drift: {investigation.title_drift or 'none'}",
        f"Fit: {investigation.fit or 'not judged'}",
    ])


def investigation_block(investigation: Investigation, *, today: date) -> str:
    """Step 2, in the workflow document's own shape."""
    def bullets(values, empty="unknown"):
        return "; ".join(v for v in values if v) or empty

    return "\n".join([
        f"Status: investigated {today.isoformat()} "
        f"(confidence: {investigation.confidence or 'low'})",
        f"Legal entity / size / stage: {investigation.entity or 'unknown'}",
        f"Product and market: {investigation.product or 'unknown'}",
        f"Funding / runway / ownership: {investigation.funding or 'unknown'}",
        f"People: {bullets(investigation.people)}",
        f"Hiring signals: {bullets(investigation.hiring_evidence)}",
        f"Risks: {bullets(investigation.risks)}",
        f"Likely day-to-day collaborators: {bullets(investigation.collaborators)}",
        f"Sources: {bullets(investigation.sources)}",
        f"Open questions: {bullets(investigation.unknown + investigation.questions)}",
        f"Commission further diligence: {investigation.commission_diligence or 'no — nothing further is worth buying'}",
    ])


#: The workflow caps the cover letter at 550 words. It was 350 until
#: 2026-10-14, when the first live completion produced a 515-word letter — the cap
#: was being hit, and LJ raised it rather than cut good material: "Where does 350
#: words letter cap come from? Let us put it up to 550 words cap. If we are
#: hitting it."
COVER_LETTER_WORDS = 550


def _words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def render_materials(material: Material, *, today: date, advert_asks: bool) -> dict[str, str]:
    """Step 3's files: the notes first, the workflow's footer **last**.

    The document says *"Every file ends with … Prepared by LoveWork (agent) …"*,
    and a note appended after it broke that (measured 2026-10-13). A file that
    breaks its own cap says so — the cap is a contract, and a 515-word letter
    presented as a compliant one is the kind of quiet drift this project fixes.
    """
    out: dict[str, str] = {}
    footer = FOOTER.format(today=today.isoformat())
    notes: list[str] = []
    if material.gaps:
        notes.append("Gaps the material could not fill: " + "; ".join(material.gaps))
    if material.needs_principal:
        notes.append("Needs the principal: " + "; ".join(material.needs_principal))
    if material.cover_letter.strip() and _words(material.cover_letter) > COVER_LETTER_WORDS:
        notes.append(f"Over the workflow's {COVER_LETTER_WORDS}-word cap for a cover "
                     f"letter ({_words(material.cover_letter)} words): shorten it before "
                     "sending.")
    body_notes = ("\n\n" + "\n".join(notes)) if notes else ""
    if material.cover_letter.strip():
        out["cover-letter.md"] = material.cover_letter.strip() + body_notes + "\n\n" + footer
    if material.cv_tailored.strip():
        out["cv-tailored.md"] = material.cv_tailored.strip() + body_notes + "\n\n" + footer
    if advert_asks and material.answers.strip():
        out["answers.md"] = material.answers.strip() + body_notes + "\n\n" + footer
    return out


def tick_checklist(dossier_text: str) -> tuple[str, int]:
    """The workflow's bookkeeping: tick steps 1–3, never step 4."""
    lines = dossier_text.split("\n")
    ticked = 0
    for index, line in enumerate(lines):
        if not line.strip().startswith("- [ ]"):
            continue
        lowered = line.lower()
        if any(token in lowered for token in CHECKLIST_OWNED) \
                and "submit" not in lowered:
            lines[index] = line.replace("- [ ]", "- [x]", 1)
            ticked += 1
    return "\n".join(lines), ticked


def replace_block(text: str, *, marker: str, body: str) -> str:
    """Replace a marker-led block up to the next section rule.

    The end is asked for through `application_packs.block_end` — one authority
    for both the markdown heading that ends a block now and the `=` rule that
    ended it in the `.txt` era. Looking only for the rule meant this function
    refused, *silently*, on every markdown dossier — a completion then reported
    an investigation it had not written (measured 2026-10-16). Refusing when the
    block truly has no end is still right: truncate the principal's dossier and
    you lose the rest of it (the 2026-10-13 lesson).
    """
    from application_packs import block_end

    start = text.find(marker)
    if start < 0:
        return text
    end = block_end(text, start)
    if end < 0:
        return text
    return text[:start] + body + text[end:]


#: The heading the pack writer puts over the investigation block, and the trailer
#: the tick closes its own investigation with — the two ends of the other shape
#: this block comes in.
INVESTIGATION_HEADING = "Company, hiring, and work reality"
TICK_TRAILER = "_Produced by the LoveWork tick."


def replace_investigation(text: str, *, body: str) -> tuple[str, bool]:
    """Put `body` where the pack's investigation block is — either shape.

    Two shapes hold it. The pack writer reserves an empty one
    (`Status: NOT YET INVESTIGATED`, which `application_packs.uninvestigated_packs`
    gates on) and the tick fills it with its own investigation when it runs
    `investigate_pack` (`## Entity` … `## Sources` under the section heading,
    closed by the tick's trailer). The *first* is replaced in place and the
    section heading is kept; the second is replaced with its subsections.

    The alternative — what the code did before 2026-10-16 — was to fall through
    to "insert the diligence after `Advert identity`", which left a pack with two
    investigations: the tick's (older, `confidence low`, its own open questions)
    and the workflow's, with nothing in the file to say which one is current
    (measured on the AMI Scientist and Elastic packs, 2026-10-16).
    """
    if NOT_INVESTIGATED_MARKER in text:
        replaced = replace_block(text, marker=NOT_INVESTIGATED_MARKER, body=body)
        if replaced != text:
            return replaced, True
    heading = text.find(INVESTIGATION_HEADING)
    if heading < 0:
        return text, False
    heading_end = text.find("\n", heading) + 1
    trailer = text.find(TICK_TRAILER, heading_end)
    if trailer < 0:
        return text, False
    # …and replace through the trailer's own line, keeping whatever follows.
    trailer_end = text.find("\n", trailer) + 1
    return text[:heading_end] + "\n" + body + "\n\n" + text[trailer_end:], True


def insert_after_identity(text: str, body: str) -> str:
    """Place a block in the dossier — after `Advert identity` when there is one.

    **It must always insert** (2026-10-19). It used to `return text` unchanged
    when no `Advert identity:` line was found — and the caller recorded the step
    anyway, so a plain-text case dossier got a receipt claiming a review that was
    never written. The pack then stayed unready for ever: `pack_readiness` looks
    for exactly this marker, the decider re-queued the pack every tick, and the
    Monday rule could never be met for that case. **A step is a claim, and a
    claim must be backed by a change to the file.**
    """
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.startswith("Advert identity:"):
            return "\n".join(lines[:index + 1] + ["", body, ""] + lines[index + 1:])
    # No identity line: a hand-made or plain-text dossier. Anchor before the
    # investigation block when there is one, else at the top — never nowhere.
    for index, line in enumerate(lines):
        if line.strip() == INVESTIGATION_HEADING:
            return "\n".join(lines[:index] + [body, ""] + lines[index:])
    return body + "\n\n" + text


def link_verdict(state_root: Path, url: str) -> dict | None:
    """The newest recorded re-check verdict for this advert, if there is one.

    Step 1 asks for the advert *as it is now*, and the 06:00 world pass already
    answers that question with a dated, evidence-carrying verdict. Reusing it is
    one authority for one fact — and one fewer fetch.
    """
    import json

    directory = state_root / "cache" / "link_checks"
    if not directory.is_dir() or not url:
        return None
    newest = None
    for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for verdict in payload.get("verdicts", []):
            if verdict.get("url") and verdict["url"] == url:
                entry = dict(verdict)
                entry["checked_at"] = payload.get("checked_at", "")
                return entry
        if newest is None:
            newest = path
    return None


# ── the shared pack facts and evidence (one authority for both callers) ──

def site_root(url: str) -> str:
    """The company's front page for an advert URL, or "" for an ATS/board host.

    Moved here from `tick` so the investigation and the completion share one
    implementation — the ATS rule (a board's root is not the employer) is a fact
    about adverts, not about a runner.
    """
    from urllib.parse import urlsplit, urlunsplit

    aggregators = ("workable.com", "greenhouse.io", "lever.co", "ashbyhq.com",
                   "workday", "smartrecruiters.com", "linkedin.com", "indeed.",
                   "totaljobs.com", "cwjobs.", "digitaljobs.com", "wellfound.com",
                   "otta.com", "workinstartups.com", "jobs.ashbyhq", "apply.")
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    host = parts.netloc.lower()
    if any(marker in host for marker in aggregators):
        return ""
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def pack_facts(pack_dir: Path) -> dict:
    """Org, role, advert link and dossier text from the pack's own files."""
    from application_packs import dossier_path, markdown_link_target

    dossier = dossier_path(pack_dir)
    markdown = pack_dir / "application_pack.md"
    text = dossier.read_text(encoding="utf-8", errors="ignore") if dossier else ""
    facts = {"pack": pack_dir, "dossier": dossier, "text": text, "url": "",
             "url_raw": ""}
    if markdown.is_file():
        md = markdown.read_text(encoding="utf-8", errors="ignore")
        head = md.splitlines()[0] if md.splitlines() else ""
        if " — " in head:
            facts["org"], facts["role"] = [part.strip() for part in
                                           head.lstrip("# ").split(" — ", 1)]
        link = re.search(r"^- \*\*Link\*\*: (.+)$", md, re.M)
        if link:
            # A cell is a bare URL in a `.txt`-era front page and a markdown link in
            # a converted one; unwrap it (one authority) or the fetcher is handed
            # the markup and refuses the whole completion.
            value = markdown_link_target(link.group(1)).strip("<>")
            facts["url_raw"] = value
            facts["url"] = value if "://" in value else ""
    facts.setdefault("org", pack_dir.name)
    facts.setdefault("role", "")
    return facts


#: The hard ceiling on **one** fetch, in seconds. The action's deadline bounds the action;
#: nothing bounded the library call inside it. Measured 2026-10-21: a vj post-crawl tick sat
#: in `do_poll` for 13 minutes with no model call and no receipt — the action ceiling was
#: enforced between steps, and a single blocking fetch was one step.
FETCH_CEILING_SECONDS = float(os.getenv("LOVEWORK_FETCH_CEILING_SECONDS", "90"))


def _fetch_bounded(fetcher, url: str, deadline: float) -> tuple[str, str]:
    """Call the fetcher with a **wall clock**, whatever the library does inside.

    A thread pool is the only portable way to bound a blocking call: the fetcher's own
    `requests`/CDP timeouts are inside a library we do not control, and an asyncio or signal
    timer would either nest badly (the browser path already runs its own loop) or be unsafe
    off the main thread. On timeout the thread is abandoned — it holds no lock and writes
    nothing — and the caller records an honest `blocked`/timeout instead of hanging.
    """
    import concurrent.futures

    remaining = max(1.0, min(FETCH_CEILING_SECONDS, deadline - time.monotonic()))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fetcher, url, use_cache=False)
        return future.result(timeout=remaining)
    except concurrent.futures.TimeoutError:
        logger.warning("[fetch] %s exceeded %.0fs — abandoned", url[:100], remaining)
        return "", "timeout"
    except Exception as exc:                  # a fetcher error is not a reason to hang
        logger.warning("[fetch] %s failed: %s", url[:100], exc)
        return "", f"error: {type(exc).__name__}"
    finally:
        pool.shutdown(wait=False)


def fetch_evidence(facts: dict, *, fetcher, deadline: float,
                   max_fetches: int = 2) -> tuple[list[str], list[dict]]:
    """The advert, then the company's front page — the two halves of the review."""
    evidence: list[str] = []
    fetches: list[dict] = []
    urls = [u for u in (facts.get("url"), site_root(facts.get("url") or "")) if u]
    for url in urls[:max_fetches]:
        if time.monotonic() > deadline:
            break
        text, provider = _fetch_bounded(fetcher, url, deadline)
        if text:
            evidence.append(f"SOURCE: {url} (via {provider})\n{text[:12000]}")
            fetches.append({"url": url, "provider": provider, "chars": len(text)})
    return evidence, fetches


def complete_pack(state_root: Path, principal: str, *, target: Path,
                  client, deadline: float, fetcher=None, today: date | None = None,
                  run_id: str = "", apply: bool = True) -> dict:
    """Run the workflow's three agent steps for one pack, in one context.

    Two model calls, every effect behind the writer lease, every file additive:
    an artifact that already exists is left alone (the workflow is idempotent by
    construction, and rewriting a tailored CV under the principal is exactly the
    kind of silent change this project refuses).
    """
    from application_packs import NOT_INVESTIGATED_MARKER as GATE
    from model_transcripts import active_dataset_dir
    from writer_boundary import WriterBusy, standalone_writer_guard

    today = today or date.today()
    facts = pack_facts(target)
    if not facts["url"]:
        return {"outcome": "blocked", "why": "the pack carries no advert link",
                "pack": str(target)}

    fetcher = fetcher or _default_fetcher()
    evidence, fetches = fetch_evidence(facts, fetcher=fetcher, deadline=deadline)
    if not evidence:
        return {"outcome": "blocked",
                "why": f"nothing could be fetched from {facts['url']}",
                "pack": str(target), "fetches": fetches}

    verdict = link_verdict(state_root, facts["url"])
    advert_text = "\n".join(e.split("\n", 1)[1] for e in evidence)[:16000]
    if verdict:
        evidence.insert(0, "LINK RE-CHECK VERDICT (the 06:00 world pass): "
                        + json.dumps({k: verdict.get(k) for k in
                                      ("verdict", "status", "method", "title_match",
                                       "detail", "checked_at")}, ensure_ascii=False))

    steps: list[str] = []
    run_id = run_id or f"pack-{state_root.name}-{today.isoformat()}"
    with active_dataset_dir(state_root / "loop" / "transcripts"), \
            outer_run_id(run_id):
        investigation = client.structured(
            [{"role": "user", "content": _review_prompt(facts, evidence)}],
            Investigation, max_tokens=PACK_MAX_TOKENS,
            context=f"[pack] review+investigate {target.name}")
        material = client.structured(
            [{"role": "user", "content": _material_prompt(principal, facts, advert_text,
                                                          investigation)}],
            Material, max_tokens=PACK_MAX_TOKENS,
            context=f"[pack] tailor {target.name}")

    review = advert_review_block(investigation, url=facts["url"], today=today,
                                 verdict=verdict, org=str(facts.get("org") or ""),
                                 principal=principal)
    diligence = investigation_block(investigation, today=today)
    asks = bool(re.search(r"(screening question|please answer|notice period|salary "
                          r"expectation|right to work|work authorisation)",
                          advert_text, re.I))
    files = render_materials(material, today=today, advert_asks=asks)

    result: dict = {"outcome": "acted", "action": "complete_pack",
                    "pack": str(target), "fetches": fetches, "calls": 2,
                    "confidence": investigation.confidence,
                    "unknown": investigation.unknown, "evidence_source": str(target)}
    if not apply:
        result["effect"] = ("dry run: review, diligence and "
                            f"{len(files)} material file(s) prepared, nothing written")
        # The verification names the artifact it *would* write, not the pack
        # directory: a receipt should point at the thing it is about.
        result["verification"] = str(facts["dossier"] or target)
        return result

    try:
        with standalone_writer_guard(state_root.name, mode="pack",
                                     state_root=state_root):
            # 1. The dossier: review block, diligence block, checklist ticks.
            dossier: Path = facts["dossier"]
            text = dossier.read_text(encoding="utf-8")
            if "Advert review —" not in text:
                placed = insert_after_identity(text, review)
                if placed != text:
                    text = placed
                    steps.append("review")
            filled = False
            if GATE in text or INVESTIGATION_HEADING in text:
                replaced, filled = replace_investigation(text, body=diligence)
                if filled:
                    text = replaced
                    steps.append("investigation")
            if GATE in text and not filled:
                # An empty investigation block with no end to write into: filling
                # the rest of the pack and ticking the checklist over it is the
                # silent drift the gate exists to prevent. Refuse, and say why.
                return {"outcome": "blocked", "pack": str(target),
                        "why": "the pack's investigation block has no end — "
                               "nothing was written"}
            if not filled and GATE not in text and "Status: investigated" not in text:
                # A dossier in neither shape (hand-made, or older than both): the
                # workflow's placement is immediately after `Advert identity`.
                placed = insert_after_identity(text, diligence)
                if placed != text:
                    text = placed
                    steps.append("investigation")
            text, ticked = tick_checklist(text)
            if ticked:
                steps.append(f"checklist×{ticked}")
            # The marker is earned here: the scaffold honestly said CUT, and
            # with the review, the diligence and the material on disk the pack
            # IS prepared (LJ, 2026-10-18: don't say PREPARED where the
            # application is not prepared — so PREPARED must mean something).
            # The SUBMITTED transition stays the principal's alone.
            from application_packs import CUT_MARKER, PREPARED_MARKER

            if CUT_MARKER in text:
                text = text.replace(CUT_MARKER, PREPARED_MARKER)
                steps.append("prepared")
            if "Advert review —" not in text:
                # The receipt must not claim a review the file does not carry.
                return {"outcome": "blocked", "pack": str(target),
                        "why": "the review block could not be placed in the "
                               "dossier — nothing was written"}
            tmp = dossier.with_name(dossier.name + ".pack-tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, dossier)
            # 2. The tailored material — additive only.
            written = []
            for name, body in files.items():
                path = target / name
                if path.is_file():
                    continue
                side = path.with_name(path.name + ".pack-tmp")
                side.write_text(body, encoding="utf-8")
                os.replace(side, path)
                written.append(name)
            if written:
                steps.append("tailored: " + ", ".join(written))
            # **The front page is regenerated, not patched** (2026-10-20). It used to
            # have its status marker replaced and nothing else — so a pack whose material
            # had just been written kept saying "Tailored CV: _not prepared yet_" (measured
            # on vj's top GO: both files present, the page denying them). The page says
            # what the directory holds, because it is rendered from it.
            from application_packs import refresh_pack_page

            try:
                if refresh_pack_page(target, state_root.name):
                    steps.append("page")
            except Exception as exc:        # a page is not worth failing the pack over
                logger.warning("front page refresh failed for %s: %s", target.name, exc)
    except WriterBusy:
        return {"outcome": "deferred", "contended": True,
                "why": "writer holds the principal", "pack": str(target)}

    result["steps"] = steps
    result["effect"] = ("dossier updated (" + ", ".join(steps) + ")") if steps         else "already complete: nothing to write"
    result["verification"] = str(facts["dossier"])
    if material.gaps or material.needs_principal:
        result["gaps"] = material.gaps
        result["needs_principal"] = material.needs_principal
    return result


def _default_fetcher():
    from crawler import fetch_page

    return lambda url, use_cache=True: fetch_page(url, use_cache=False)


# The prompt lives in `prompts.py` (2026-10-15).
from prompts import review_prompt as _review_prompt


# The prompt lives in `prompts.py` (2026-10-15).
from prompts import material_prompt as _material_prompt
