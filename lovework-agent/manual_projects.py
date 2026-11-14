#!/usr/bin/env python3
"""Manual (non-LoveWork) applications — index, search, and an evidence digest.

LJ, 2026-10-20: *"The case histories of our manual applications should be
something Lovework should absolutely learn from! So for every user we need to
have an extra directory for 'non-Lovework applications' that lovework should
index, search, use, gather knowledge etc. For principal LJ that should be
~/LJ-work-2026. For principals vj and kj that is currently empty, does not
exist. But it will in due time."*

**What a manual case is.** A `YYYYMMDD-Company-Role/` directory under the
principal's own corpus root, holding the dossier he wrote (`<case>.txt`, or
`<case>.md` since 2026-10-15) plus whatever material he produced — cover
letter, form answers, evidence map, tailored CV, interview prep. These are the
highest-SNR artefacts in the corpus because a human wrote them: they say what he
actually claimed, in his own words, about a real organisation. LoveWork's own
packs (`state/<p>/applications/*-LoveWork`) are a different thing — its output,
not his record — and are excluded here.

**Readers never need an index.** `manual_cases()` walks the tree on every call.
`write_index()` writes `state/<p>/dataset/manual_index.json` for other tools to
read cheaply; nothing in this module requires it to exist, so a cold start (a
fresh host, a principal whose index has never been written) answers normally.

**One authority per fact** — the point of this module is to *reuse* the
project's existing answers, not to add a second copy of each:

- the corpus root — `config.manual_root(principal)`;
- the case-name convention (date, organisation, role) —
  `history.parse_case_dirname`, the project's one parser;
- "is this case about this organisation" — `history.case_dir_matches_org` and
  `history.org_matches_text`, both over `history._org_aliases`;
- the case's own document — `application_packs.dossier_path`;
- "is this directory LoveWork's?" — `history.PREPARATION_MARKERS` plus the
  `-LoveWork` suffix;
- dossier fields (`Position:`, `Primary advert URL:`) — `outcome_link`;
- the principal's dataset directory — `principal_runtime`;
- the cap on reading a case file — `outcome_link.MAX_CASE_FILE_BYTES`.

**What `applied` may say.** True when the case carries submission evidence,
False when the principal's own filing says it was not submitted, and `None`
when the case records neither — because "no evidence" and "did not apply" are
different facts and only one of them is knowable. The verdict is never invented:
`applied_basis` names the file and the line it came from.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import application_packs
import config
import history
import outcome_link
from principal_runtime import resolve_principal_runtime

logger = logging.getLogger(__name__)

#: Where `--write` puts the index, inside the principal's dataset directory.
INDEX_FILENAME = "manual_index.json"
INDEX_SCHEMA = 1

#: LoveWork's own case suffix. A pack is LoveWork's output, never the
#: principal's manual record — the same test `prepare_cases` uses.
LOVEWORK_SUFFIX = "-LoveWork"

#: Files whose text can be quoted or searched. A `.docx`/`.pdf` CV is recorded
#: as present and never read: the bytes are not text, and pretending otherwise
#: would put binary noise in a prompt.
TEXT_SUFFIXES = frozenset({".md", ".txt"})

#: The cap on reading one case file — the resolver's constant, not a second one.
MAX_CASE_BYTES = outcome_link.MAX_CASE_FILE_BYTES

SUMMARY_CHARS = 200
#: How much of a file `search` looks at. "The material files' first lines": the
#: head of a letter says what it argues, and a full-text scan of every file
#: would index the advert as if it were the principal's own words.
SEARCH_HEAD_CHARS = 1200
#: Never quote less than this from one file in an evidence digest.
EVIDENCE_MIN_ITEM_CHARS = 320

#: The material a case holds, in the order a reader wants it: what the
#: principal said about himself, then the CV, then the interview prep.
#: Patterns are globs inside the case directory; the first match per kind is
#: the canonical file, later ones are kept in order.
MATERIAL_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cover_letter", ("company-cover-letter.*", "cover-letter.*", "lj-cov.*")),
    ("answers", ("company-answers.*", "answers.*")),
    ("evidence", ("evidence-*.md", "evidence.*")),
    ("interview", ("interview-*.md",)),
    ("cv", ("cv*.md", "cv*.docx", "cv*.pdf")),
)

#: What an evidence digest quotes, in order. A CV is deliberately absent: it
#: describes the principal, not what he did with *this* organisation.
EVIDENCE_KINDS = ("cover_letter", "answers", "evidence", "interview")


# ── submission evidence ─────────────────────────────────────────────────
#: Markers that the principal **submitted**. ``(name, regex)``; the name is
#: reported in `applied_basis`, so a reader can see which tell fired.
#:
#: The first three are the project's own conventions: a pack's status line
#: (`LoveWork status: SUBMITTED <date>`, the sibling of
#: `history.PREPARATION_MARKERS`), a `Stage:` line, and the stage row a manual
#: case's `README.md` keeps (`| 2026-09-02/03 | SUBMITTED via Workable |` —
#: LJ's own filing). `_CONFIRMATIONS` are a receipt quoted into the dossier: the
#: ATS confirmation page, or the mail that follows it ("Thank you for applying
#: to …"), and `_NOT_SUBMITTED` is the principal saying it did not happen.
_STAGE_SUBMITTED = (
    ("status_marker", re.compile(r"(?m)^[ \t]*LoveWork status:[ \t]*SUBMITTED\b")),
    ("stage_line", re.compile(r"(?mi)^[ \t]*(?:\*\*)?stage[ \t]*:[ \t]*(?:\*\*)?[ \t]*SUBMITTED\b")),
    ("stage_row", re.compile(r"(?m)^\|[ \t]*\d{4}-\d{2}-\d{2}[^|\n]*\|[ \t]*SUBMITTED\b")),
)
_CONFIRMATIONS = (
    ("confirmation_mail", re.compile(r"(?i)thank you for (?:your application|applying)")),
    ("confirmation_page", re.compile(
        r"(?i)\byour application (?:has been |was )?(?:received|submitted)")),
    ("confirmation_page", re.compile(
        r"(?i)\bapplication (?:was |has been )?submitted successfully\b")),
    ("confirmation_mail", re.compile(r"(?i)\bwe (?:have|'ve) received your application\b")),
)
_NOT_SUBMITTED = (
    ("stage_not_submitted", re.compile(
        r"(?mi)^[ \t]*(?:\*\*)?stage[ \t]*:[ \t]*(?:\*\*)?[^\n]{0,60}\bNOT SUBMITTED\b")),
    ("status_not_applied", re.compile(r"(?i)\bnot yet applied\b")),
    ("status_draft", re.compile(r"(?mi)^status:[ \t]*draft\b")),
    ("not_submitted", re.compile(r"(?i)\bnot (?:been )?submitted\b")),
)

#: How far either side of a confirmation quote the digest-guard looks for the
#: organisation's name. Wide enough for a mail's greeting and subject line,
#: narrow enough that a paragraph about another employer does not count.
ATTRIBUTION_WINDOW = 300

_FRONTMATTER_FIELD = re.compile(r"(?m)^([A-Za-z_][\w-]*):[ \t]*(.+?)[ \t]*$")
_README_ROLE = re.compile(r"(?m)^role:[ \t]*(.+?)[ \t]*$")
_H1 = re.compile(r"(?m)^# (.+)$")
_JD_TITLE = re.compile(r"(?m)^TITLE:[ \t]*(.+?)[ \t]*$")
_APPLY_URL = re.compile(
    r"(?m)^[ \t]*(?:Apply URL|Primary advert URL|Advert URL|Application URL)[ \t]*:[ \t]*(\S+)")
_DATE_PREFIX = re.compile(r"^\d{4}-?\d{2}-?\d{2}\b")
_GENERATED_CASE_TITLE = re.compile(r"^Case — \S+$")


# ── the case record ─────────────────────────────────────────────────────

@dataclass
class ManualCase:
    """One manual application, as the files on disk tell it.

    Everything here is read, never inferred: `applied` stays None when the case
    records neither a submission nor a refusal to submit, and `applied_basis`
    names the line the verdict came from.
    """

    principal: str
    directory: Path
    date: Optional[str]                # ISO date from the `YYYYMMDD-` prefix
    organisation: str                  # as the directory spells it, words restored
    role: str                          # the directory's role segment, words restored
    title: str                         # the advertised title, when a file names one
    url: str = ""
    dossier: Optional[Path] = None
    readme: Optional[Path] = None
    material: dict[str, list[Path]] = field(default_factory=dict)
    applied: Optional[bool] = None
    applied_basis: str = ""
    summary: str = ""

    @property
    def name(self) -> str:
        return self.directory.name

    def files(self) -> list[Path]:
        """Every file the case holds that this reader knows about, dossier first."""
        out: list[Path] = [self.dossier] if self.dossier else []
        if self.readme is not None:
            out.append(self.readme)
        for paths in self.material.values():
            out.extend(paths)
        return out


# ── reading one case ────────────────────────────────────────────────────

def _read_text(path: Optional[Path], *, limit: int = MAX_CASE_BYTES) -> str:
    """Text of a case file, capped; "" when unreadable.

    A case file is evidence, not a stream: a missing or unreadable one makes the
    case quieter, never an error that stops the corpus walk.
    """
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError as exc:
        logger.debug("manual case file unreadable: %s (%s)", path, exc)
        return ""


def _one_line(text: str, limit: int = SUMMARY_CHARS) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _words(value: str) -> str:
    """The directory's spelling, restored to words: `Hugging_Face` -> `Hugging Face`."""
    return " ".join((value or "").replace("_", " ").split())


def _frontmatter(text: str) -> dict[str, str]:
    """The `---` block at the top of a README, when it has one.

    Ten of the twenty case READMEs carry `company:` / `role:` / `status:` fields;
    the others start with a heading. Both are his writing, so both are read.
    """
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        match = _FRONTMATTER_FIELD.match(line)
        if match:
            fields[match.group(1).lower()] = match.group(2)
    return {}


def _readme_summary(readme: Optional[Path]) -> str:
    """A one-line summary of the case, from `README.md` when there is one.

    The heading first (LJ writes `# 20260902 — Hugging Face — Senior Machine
    Learning Engineer, Voice Agents (EMEA Remote)`), then the frontmatter's
    `role`, then its `status`. `cases.make_case_dir` writes a bare
    `# Case — <slug>`, which says nothing a caller does not already have, so it
    is skipped rather than repeated.
    """
    text = _read_text(readme, limit=64 * 1024)
    if not text:
        return ""
    for line in text.splitlines():
        if not line.startswith("# "):
            continue
        title = line[2:].strip()
        if title and not _GENERATED_CASE_TITLE.match(title):
            return _one_line(title)
    fields = _frontmatter(text)
    if fields.get("role"):
        company = fields.get("company")
        return _one_line(f"{fields['role']} — {company}" if company else fields["role"])
    if fields.get("status"):
        return _one_line(f"status: {fields['status']}")
    return ""


def _material(directory: Path) -> dict[str, list[Path]]:
    """Which known files the case holds, by kind (`MATERIAL_KINDS` order)."""
    found: dict[str, list[Path]] = {}
    for kind, patterns in MATERIAL_KINDS:
        paths: list[Path] = []
        for pattern in patterns:
            for path in sorted(directory.glob(pattern)):
                if path.is_file() and path not in paths:
                    paths.append(path)
        if paths:
            found[kind] = paths
    return found


#: A title candidate needs this many letters to be a title rather than a blank
#: form line. An employment contract's signature block reads
#: `Position: ___________________________`, and that was returned as the
#: advertised role until 2026-10-20 — a placeholder read as a value.
_MIN_TITLE_LETTERS = 3


def _usable_title(value: str) -> str:
    """The title if it says something, else "" so the ladder falls through."""
    text = _one_line(value)
    return text if sum(character.isalpha() for character in text) >= _MIN_TITLE_LETTERS else ""


def _advert_title(dossier_text: str, readme_text: str, fallback: str) -> str:
    """The advertised title, from the first file that names a real one.

    Four file rungs, most specific first: the dossier's `Position:` field
    (LoveWork's own convention, read through `outcome_link`), the README's
    frontmatter `role:`, the JD's `TITLE:` line (a fetched ATS advert), and the
    README heading's third `—`-separated segment
    (`# <date> — <org> — <role>`). A rung that yields only punctuation is
    skipped, not returned. The directory's role segment is the floor: it is
    always there, and it is what the principal himself typed.
    """
    for match in (outcome_link.CASE_FIELD_RES["title"].search(dossier_text),
                  _README_ROLE.search(readme_text),
                  _JD_TITLE.search(dossier_text)):
        if match:
            title = _usable_title(match.group(1))
            if title:
                return title
    match = _H1.search(readme_text)
    if match:
        segments = [part.strip() for part in match.group(1).split(" — ")]
        if len(segments) >= 3 and _DATE_PREFIX.match(segments[0]):
            title = _usable_title(" — ".join(segments[2:]))
            if title:
                return title
    return _usable_title(fallback) or fallback


def _advert_url(dossier_text: str, readme_text: str) -> str:
    """The advert URL the case recorded, or "" — never a guessed one."""
    match = outcome_link.CASE_FIELD_RES["url"].search(dossier_text)
    if match:
        return match.group(1).strip()
    match = _APPLY_URL.search(dossier_text)
    if match:
        return match.group(1).rstrip(".,);")
    for text in (readme_text, dossier_text):
        match = outcome_link.URL_RE.search(text or "")
        if match:
            return match.group(0).rstrip(".,);")
    return ""


def _first_match(patterns, text: str):
    """The first `(name, match)` that fires in this text, or None."""
    for name, pattern in patterns:
        match = pattern.search(text)
        if match:
            return name, match
    return None


def _basis(source: str, name: str, match: re.Match) -> str:
    """The line a verdict came from, short enough to print."""
    line = match.group(0).strip()
    if not line:
        start = max(0, match.start() - 40)
        line = " ".join(match.string[start:match.end()].split())
    return f"{source}: {name} — {_one_line(line, 140)}"


def _names_org(compact_window: str, org: str) -> bool:
    """Does this folded text name `org`? Aliases shorter than 4 chars are skipped.

    Four because a three-letter alias is a substring of ordinary words — the
    guard's job is to notice *another employer* in a quoted receipt, not to
    score a match.
    """
    for alias in history._org_aliases(org):
        compact = re.sub(r"[^a-z0-9]", "", alias.lower())
        if len(compact) >= 4 and compact in compact_window:
            return True
    return False


def _attributable(text: str, match: re.Match, own_org: str,
                  other_orgs: Iterable[str]) -> bool:
    """Is a quoted receipt about *this* case?

    A dossier is a working file, not a clean log: it holds pasted conversations
    in which other applications are discussed, so "Thank you for applying to
    Renaissance Philanthropy" can sit inside another case's dossier. When the
    window around a receipt names a different organisation and not this one, the
    receipt is not evidence about this case and is ignored. Without the guard,
    one org's confirmation made an unrelated case look applied.
    """
    start = max(0, match.start() - ATTRIBUTION_WINDOW)
    window = re.sub(r"[^a-z0-9]", "", text[start:match.end() + ATTRIBUTION_WINDOW].lower())
    if _names_org(window, own_org):
        return True
    return not any(_names_org(window, org) for org in other_orgs if org)


def _applied_verdict(
    readme_text: str,
    dossier_text: str,
    *,
    own_org: str,
    other_orgs: Iterable[str],
) -> tuple[Optional[bool], str]:
    """Did the principal submit this case? `(True|False|None, basis)`.

    The ladder, in order — the principal's own filing outranks a quoted
    receipt, because his `Stage:` line is a decision and a pasted confirmation
    page can belong to an earlier attempt:

    1. `README.md` says SUBMITTED;
    2. `README.md` says NOT SUBMITTED / "not yet applied" / a draft;
    3. the dossier carries an explicit submitted marker (a pack status line);
    4. the dossier quotes a submission receipt, attributed to this organisation;
    5. the dossier says, in prose, that it was not submitted;
    6. otherwise None — the case records neither, which is not "no".
    """
    found = _first_match(_STAGE_SUBMITTED, readme_text)
    if found:
        return True, _basis("README.md", *found)
    found = _first_match(_NOT_SUBMITTED, readme_text)
    if found:
        return False, _basis("README.md", *found)
    found = _first_match(_STAGE_SUBMITTED, dossier_text)
    if found:
        return True, _basis("dossier", *found)
    for name, pattern in _CONFIRMATIONS:
        for match in pattern.finditer(dossier_text):
            if _attributable(dossier_text, match, own_org, other_orgs):
                return True, _basis("dossier", name, match)
    found = _first_match(_NOT_SUBMITTED, dossier_text)
    if found:
        return False, _basis("dossier", *found)
    return None, ""


def read_case(principal: str, directory: Path,
              *, other_orgs: Iterable[str] = ()) -> Optional[ManualCase]:
    """One manual case, or None when the directory is not one.

    Not a manual case: a name that is not `YYYYMMDD-…` (nothing can be filed
    under it), a LoveWork pack (`-LoveWork`, or a dossier carrying a pack
    status marker), which is LoveWork's output rather than the principal's
    record.
    """
    parsed = history.parse_case_dirname(directory.name)
    if parsed is None:
        return None
    if directory.name.endswith(LOVEWORK_SUFFIX):
        return None

    dossier = application_packs.dossier_path(directory)
    dossier_text = _read_text(dossier)
    if any(marker in dossier_text for marker in history.PREPARATION_MARKERS):
        return None

    readme = directory / "README.md"
    readme = readme if readme.is_file() else None
    readme_text = _read_text(readme, limit=64 * 1024)

    organisation = _words(parsed.company)
    role = _words(parsed.role)
    title = _advert_title(dossier_text, readme_text, role)
    applied, basis = _applied_verdict(
        readme_text, dossier_text, own_org=organisation, other_orgs=other_orgs)

    case_date: Optional[str] = None
    try:
        case_date = date.fromisoformat(parsed.date).isoformat()
    except ValueError:
        logger.debug("case directory date is not a calendar date: %s", directory.name)

    return ManualCase(
        principal=principal,
        directory=directory,
        date=case_date,
        organisation=organisation,
        role=role,
        title=title,
        url=_advert_url(dossier_text, readme_text),
        dossier=dossier,
        readme=readme,
        material=_material(directory),
        applied=applied,
        applied_basis=basis,
        summary=_readme_summary(readme),
    )


# ── the corpus ──────────────────────────────────────────────────────────

def corpus_root(principal: str, root: Optional[Path] = None) -> Optional[Path]:
    """The principal's manual corpus, or None when it has none (yet)."""
    if root is not None:
        return Path(root)
    return config.manual_root(principal)


def manual_cases(principal: str, *, root: Optional[Path] = None) -> list[ManualCase]:
    """Every manual case this principal has, newest first. Never an error.

    A principal with no corpus — vj and kj today — answers `[]`, and so does a
    corpus directory that is absent or empty. The reader computes from the tree
    every call: there is no index to be stale, and no cold-start state to
    require (`write_index` is a convenience for other tools, not a prerequisite).
    """
    base = corpus_root(principal, root)
    if base is None or not Path(base).is_dir():
        return []
    parsed = {entry.name: history.parse_case_dirname(entry.name)
              for entry in Path(base).iterdir()
              if entry.is_dir() and not entry.name.startswith(".")}
    others = [_words(name.company) for name in parsed.values() if name is not None]
    cases: list[ManualCase] = []
    for directory_name in sorted(parsed):
        case = read_case(principal, Path(base) / directory_name, other_orgs=others)
        if case is not None:
            cases.append(case)
    cases.sort(key=lambda case: (case.date or "", case.name), reverse=True)
    return cases


def for_org(principal: str, org: str, *, root: Optional[Path] = None) -> list[ManualCase]:
    """The cases this principal filed against `org`, newest first.

    The organisation test is the project's own, in two surfaces it can honestly
    appear on: the directory name (`history.case_dir_matches_org`) and the
    `company:` line the principal wrote in `README.md`
    (`history.org_matches_text`) — `20260614-EQL_Tech-…` is filed as *EQL
    Global*, and nothing in the slug says Global.
    """
    matched: list[ManualCase] = []
    for case in manual_cases(principal, root=root):
        if history.case_dir_matches_org(case.name, org):
            matched.append(case)
            continue
        fields = _frontmatter(_read_text(case.readme, limit=64 * 1024))
        if fields.get("company") and history.org_matches_text(fields["company"], org):
            matched.append(case)
    return matched


def _search_head(path: Optional[Path]) -> str:
    """The first lines of a material file — what `search` reads."""
    return _read_text(path, limit=SEARCH_HEAD_CHARS)


def search_hits(principal: str, text: str, *,
                root: Optional[Path] = None) -> list[tuple[ManualCase, str]]:
    """Search the corpus; each hit carries the surface it matched.

    Over the titles, the organisations, and the first lines of the material
    files — the surfaces that are the principal's own words. The dossier is
    deliberately outside the search: it is mostly the employer's advert copy, so
    matching it would answer "this advert mentions agents" rather than "this
    principal wrote about agents".
    """
    needle = (text or "").strip().lower()
    if not needle:
        return []
    hits: list[tuple[ManualCase, str]] = []
    for case in manual_cases(principal, root=root):
        where = ""
        if needle in case.organisation.lower():
            where = "organisation"
        elif needle in (case.title or "").lower() or needle in case.role.lower():
            where = "title"
        elif needle in case.summary.lower():
            where = "README summary"
        elif needle in case.name.lower():
            where = "directory"
        else:
            for kind, paths in case.material.items():
                for path in paths:
                    if path.suffix.lower() not in TEXT_SUFFIXES:
                        continue
                    if needle in _search_head(path).lower():
                        where = f"{kind}: {path.name}"
                        break
                if where:
                    break
        if where:
            hits.append((case, where))
    return hits


def search(principal: str, text: str, *, root: Optional[Path] = None) -> list[ManualCase]:
    """The cases matching `text` (see `search_hits` for where a match was found)."""
    return [case for case, _ in search_hits(principal, text, root=root)]


# ── the evidence digest ─────────────────────────────────────────────────

#: A line that labels a file rather than saying anything in it.
_METADATA_LINE = re.compile(
    r"(?i)^(?:to|cc|from|subject|drafted|notes?\b|sources?:|compiled|created|updated|"
    r"short enough|files?:)")


def _prose_start(text: str) -> int:
    """Where a case file's own prose begins, past its title-and-notes preamble.

    LJ's case files open with a heading, a line or two about the file ("Drafted
    2026-09-02 by agent from LJ notes"), a rule, and sometimes a quoted source
    list. That preamble says what the file *is*; the paragraph after it says what
    he did. An excerpt that stops inside the preamble quotes the label instead of
    the evidence — `# Hugging Face — cover letter` is not a claim about him.
    """
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        preamble = (not stripped
                    or stripped.startswith(("#", ">", "|", "---", "===", "***"))
                    or bool(_METADATA_LINE.match(stripped)))
        if not preamble:
            return offset
        offset += len(line)
    return 0


def _excerpt(path: Path, chars: int) -> str:
    """An excerpt of a case file, past its preamble, cut near a line boundary."""
    text = _read_text(path)
    if not text.strip() or chars < 1:
        return ""
    start = _prose_start(text)
    if len(text) - start < EVIDENCE_MIN_ITEM_CHARS:
        start = 0                      # skipping would leave too little to quote
    body = text[start:start + chars].rstrip()
    if len(text) - start > chars:
        cut = body.rfind("\n")
        if cut > chars // 2:
            body = body[:cut].rstrip()
        body += "\n…"
    return body


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.splitlines())


def _applied_phrase(case: ManualCase) -> str:
    if case.applied is True:
        return f"submitted — {case.applied_basis}" if case.applied_basis else "submitted"
    if case.applied is False:
        return f"not submitted — {case.applied_basis}" if case.applied_basis else "not submitted"
    return "submission not recorded"


def evidence(principal: str, org: str, *,
             limit_chars: int = 4000, root: Optional[Path] = None) -> str:
    """What this principal already did with `org` — the digest a pack reads.

    Answers the question a pack's material step actually has: *has this
    principal written to this organisation before, and what did he say?* The
    advert title, the date, whether he submitted, and excerpts of his own cover
    letter, form answers and evidence file — his words, quoted, never paraphrased
    and never extended by guesswork.

    The reason this exists (LJ, 2026-10-20): a pack once told him he had "no
    public OSS-contribution record" while the case history held his repos, his
    Hugging Face models and his open PRs. Inventing a gap that the corpus
    already refutes is the failure this function prevents.

    Empty string when no case matches — the same answer as "nothing on file",
    which the caller can say out loud.

    `limit_chars` bounds the whole digest including its headers, so the default
    4000 holds a quote from every file of the newest case; a very small limit
    truncates from the end, where the quotes are — ask for at least ~1500 if you
    want text rather than the case's identity.
    """
    cases = for_org(principal, org, root=root)
    if not cases:
        return ""
    base = corpus_root(principal, root)
    total = len(cases)
    header = [
        f"MANUAL APPLICATION EVIDENCE — {org} ({principal})",
        f"{total} case(s) in `{base}` (newest first). Written by the principal, "
        f"not by LoveWork. Reuse what is here; do not claim a gap this refutes.",
        "",
    ]
    budget = max(0, int(limit_chars) - len("\n".join(header)))
    blocks: list[str] = []
    for index, case in enumerate(cases):
        lines = [
            f"### {case.date or 'date not recorded'} · {case.organisation} · "
            f"{case.title or case.role or 'role not recorded'}",
            f"- Case: `{case.name}` ({'dossier: ' + case.dossier.name if case.dossier else 'no dossier'})",
            f"- Submission: {_applied_phrase(case)}",
        ]
        if case.url:
            lines.append(f"- Advert: {case.url}")
        if case.summary:
            lines.append(f"- Summary: {case.summary}")
        quoted = [(kind, path) for kind in EVIDENCE_KINDS
                  for path in case.material.get(kind, ())
                  if path.suffix.lower() in TEXT_SUFFIXES]
        if quoted:
            lines.append("- Files: " + ", ".join(
                path.name for _, path in quoted))
        else:
            lines.append("- Files: dossier only (the advert copy and the "
                         "principal's notes; no letter, answers or evidence file)")
        block = "\n".join(lines)
        remaining_cases = len(cases) - index
        share = max(0, budget - len(block)) // max(1, remaining_cases)
        for kind, path in quoted:
            per_file = max(EVIDENCE_MIN_ITEM_CHARS, share // max(1, len(quoted)))
            body = _excerpt(path, per_file)
            if not body:
                continue
            block += f"\n\n**{path.name}**\n\n{_quote(body)}"
            share = max(0, share - len(body))
        blocks.append(block)
    text = "\n".join(header) + "\n" + "\n\n".join(blocks)
    if len(text) > limit_chars:
        suffix = "\n…(digest truncated)"
        text = text[: max(0, limit_chars - len(suffix))].rstrip() + suffix
    return text


# ── serialisation and the index ─────────────────────────────────────────

def case_to_dict(case: ManualCase) -> dict:
    """A JSON-ready view of one case (filenames, never file contents)."""
    return {
        "name": case.name,
        "directory": str(case.directory),
        "date": case.date,
        "organisation": case.organisation,
        "role": case.role,
        "title": case.title,
        "url": case.url,
        "dossier": case.dossier.name if case.dossier else None,
        "readme": case.readme.name if case.readme else None,
        "material": {kind: [path.name for path in paths]
                     for kind, paths in case.material.items()},
        "applied": case.applied,
        "applied_basis": case.applied_basis,
        "summary": case.summary,
    }


def index_payload(principal: str, *, root: Optional[Path] = None) -> dict:
    """The index document for `state/<p>/dataset/manual_index.json`."""
    cases = manual_cases(principal, root=root)
    base = corpus_root(principal, root)
    counts = {"yes": 0, "no": 0, "unknown": 0}
    for case in cases:
        counts["yes" if case.applied is True else
               "no" if case.applied is False else "unknown"] += 1
    return {
        "schema": INDEX_SCHEMA,
        "principal": principal,
        "root": str(base) if base is not None else None,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(cases),
        "applied": counts,
        "cases": [case_to_dict(case) for case in cases],
    }


def write_index(principal: str, *, root: Optional[Path] = None) -> dict:
    """Write the index atomically into the principal's dataset directory.

    A cache for other tools, never a prerequisite: every reader in this module
    computes from the tree. Written through `resolve_principal_runtime`, so the
    index lands beside the ledgers even when a host points the dataset directory
    somewhere else.
    """
    runtime = resolve_principal_runtime(principal)
    target = runtime.dataset_dir / INDEX_FILENAME
    payload = index_payload(principal, root=root)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="manual-index-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, target)
    except Exception as exc:                      # noqa: BLE001 — reported, not swallowed
        try:
            os.unlink(tmp)
        except OSError:
            pass
        logger.warning("manual index write failed: %s", exc)
        return {"ok": False, "path": str(target), "error": f"{type(exc).__name__}: {exc}",
                "count": payload["count"]}
    return {"ok": True, "path": str(target), "bytes": len(text), "count": payload["count"]}


# ── CLI ─────────────────────────────────────────────────────────────────

def _no_corpus_message(principal: str) -> str:
    """Say which of the two absences this is — a principal with no corpus, or a
    corpus with no directory — rather than printing an empty list that looks
    like a corpus holding nothing."""
    if config.manual_root(principal) is None:
        configured = ", ".join(sorted(config.MANUAL_CORPUS_ROOTS)) or "no principal"
        root = config.manual_root("lj")
        where = f" (lj's is {root})" if root is not None and "lj" in configured else ""
        return (f"{principal}: no manual-application corpus exists for this principal "
                f"yet — configured for {configured}{where}. Nothing to index.")
    return f"{principal}: the manual corpus directory does not exist yet — nothing to index."


def _print_case(case: ManualCase) -> None:
    mark = {True: "submitted", False: "not submitted", None: "submission not recorded"}[case.applied]
    files = sum(len(paths) for paths in case.material.values())
    print(f"{case.date or 'date?':10s}  {case.organisation[:28]:28s}  "
          f"{(case.title or case.role or 'role not recorded')[:62]:62s}  "
          f"[{mark}; {files} material file(s)]")
    if case.summary and case.summary != case.title:
        print(f"{'':10s}  {case.summary}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="The principal's manual (non-LoveWork) applications — list, "
                    "find, search, and read as evidence.")
    parser.add_argument("--profile", required=True, help="principal (lj, vj, kj, pk)")
    parser.add_argument("--root", default=None,
                        help="override the corpus root (a relocated tree, a test)")
    view = parser.add_mutually_exclusive_group()
    view.add_argument("--list", action="store_true",
                      help="every case (the default)")
    view.add_argument("--for-org", default=None, metavar="ORG",
                      help="cases filed against this organisation")
    view.add_argument("--search", default=None, metavar="TEXT",
                      help="search titles, organisations and material files")
    view.add_argument("--evidence", default=None, metavar="ORG",
                      help="the digest a pack reads: what he already said to this org")
    parser.add_argument("--limit-chars", type=int, default=4000,
                        help="evidence digest size (default 4000)")
    parser.add_argument("--write", action="store_true",
                        help=f"write state/<p>/dataset/{INDEX_FILENAME}")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    principal = args.profile.lower().strip()
    root = Path(args.root) if args.root else None
    corpus = corpus_root(principal, root)

    if corpus is None or not corpus.is_dir():
        if args.json:
            print(json.dumps({"principal": principal,
                              "root": str(corpus) if corpus else None,
                              "count": 0, "cases": [],
                              "note": _no_corpus_message(principal)}, indent=2))
        else:
            print(_no_corpus_message(principal))
        if args.write and corpus is not None:
            result = write_index(principal, root=root)
            print(f"[manual index] {'written' if result['ok'] else 'FAILED'}: "
                  f"{result['path']} ({result['count']} case(s))", file=sys.stderr)
            return 0 if result["ok"] else 1
        if args.write:
            # No corpus is configured for this principal: an empty index would
            # create a `dataset/manual_index.json` for a person LoveWork has
            # nothing to index for. Say so instead.
            print(f"[manual index] not written: {principal} has no corpus", file=sys.stderr)
        return 0

    if args.evidence:
        text = evidence(principal, args.evidence, limit_chars=args.limit_chars, root=root)
        if args.json:
            print(json.dumps({"principal": principal, "organisation": args.evidence,
                              "digest": text}, indent=2, ensure_ascii=False))
        elif text:
            print(text)
        else:
            print(f"{principal}: no manual case for {args.evidence!r} — nothing on file.")
        return 0

    hits: list[tuple[ManualCase, str]] = []
    if args.for_org:
        cases = for_org(principal, args.for_org, root=root)
        note = f"manual corpus: {corpus} ({len(cases)} case(s) for {args.for_org!r})"
    elif args.search:
        hits = search_hits(principal, args.search, root=root)
        cases = [case for case, _ in hits]
        note = f"manual corpus: {corpus} ({len(cases)} hit(s) for {args.search!r})"
    else:
        cases = manual_cases(principal, root=root)
        note = f"manual corpus: {corpus}"

    if args.json:
        payload = [case_to_dict(case) for case in cases]
        print(json.dumps({"principal": principal, "root": str(corpus),
                          "count": len(payload),
                          "where": [where for _, where in hits] if hits else [],
                          "cases": payload}, indent=2, ensure_ascii=False))
    else:
        print(note)
        if not cases:
            if args.for_org:
                print(f"no manual case for {args.for_org!r}")
            elif args.search:
                print(f"nothing matches {args.search!r} in titles, organisations, "
                      f"READMEs or material files")
            else:
                print("no manual cases on file")
        matched = {case.name: where for case, where in hits}
        for case in cases:
            _print_case(case)
            if case.name in matched:
                print(f"{'':10s}  matched: {matched[case.name]}")
        if cases:
            yes = sum(1 for case in cases if case.applied is True)
            no = sum(1 for case in cases if case.applied is False)
            print(f"\n{len(cases)} case(s): submitted {yes}/{len(cases)}, "
                  f"not submitted {no}/{len(cases)}, "
                  f"not recorded {len(cases) - yes - no}/{len(cases)}")

    if args.write:
        result = write_index(principal, root=root)
        print(f"[manual index] {'written' if result['ok'] else 'FAILED'}: "
              f"{result['path']} ({result['count']} case(s))", file=sys.stderr)
        if not result["ok"]:
            print(f"[manual index] {result['error']}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
