"""`mem` — the one door to LoveWork's OptMem memories (PLAN.md T1.1, D22).

Every writer and reader goes through this wrapper so MEMORY_DIR resolution
has a single code authority: no call site ever builds a memory path by hand.

Layout (docs/40 §5.1, inside the seal include trees):

    state/<p>/dataset/memory/
      principal/          the operator-agent's own memory for principal p
      orgs/<slug>/        slug = company_events.canonical_company
      people/<slug>/      kebab-case of the person's tracker name
      agents/<name>/      boards, frontierlist, J&J peers, future agents

Usage:

    mem init   --profile lj [--org SLUG|--person SLUG|--agent NAME]
    mem note   --profile lj [--org …|--person …|--agent …] "<one line>"
    mem wake   --profile lj [--org …|--person …|--agent …] [--asof DATE]
    mem recall --profile lj [--org …|--person …|--agent …] "<regex>"
    mem nap    --profile lj [--org …|--person …|--agent …] [LO-HI "<line>"]

Ground rules enforced here (PLAN.md §0): writes refuse the subagent
environment (LOVEWORK_SUBAGENT=1 → exit 75, the honest-state vocabulary's
"blocked" code); entity memories init at WAKE_LINES=48 while principal
memories keep the upstream default (96); everything else passes through to
the vendored `memo` (tools/optmem/memo) unchanged.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import config
from filename_limits import clip_stem

MEMO = config.AGENT_ROOT / "tools" / "optmem" / "memo"

ENTITY_WAKE_LINES = 48  # principal/ keeps the upstream default (96)

USAGE = __doc__.strip()

# ── filesystem-friendly slugs (2026-10-05) ───────────────────────────────
# Memory directory names must be convenient for a *person* at a shell as well as
# for tools: no spaces, no shell metacharacters, no trailing dots, accents folded
# to ASCII. The ledger's `org_name` is machine-extracted and dirty ("Jefferies
# New York, NY", "Enthought, Inc.", "Dassault Systèmes"), so the slug folds it.
# LJ: "for the names on the file system use names user- and filesystem friendly,
# don't use only machine friendly name… we just need to put some extra care."
#
# The rules handle most names; this table handles the ones they cannot (a
# trailing city after a comma-less location, an acronym buried in a
# parenthetical, ledger noise that is not an org at all).
SLUG_OVERRIDES = {
    "jefferies new york, ny": "Jefferies",
    "voleon group berkeley, ca": "Voleon-Group",
    "hyperbolic millbrae, ca": "Hyperbolic",
    "piper sandler east boston, ma": "Piper-Sandler",
    "university of pennsylvania new york, ny": "University-of-Pennsylvania",
    "le 001 leerink partners new york, ny": "Leerink-Partners",
    "leerink partners new york, ny": "Leerink-Partners",
    # one firm, several ledger spellings → one memory
    "hastings insurance": "Hastings-Insurance",
    "hastings insurance services ltd": "Hastings-Insurance",
    "mufg bank": "MUFG-Bank",
    "mufg bank ltd": "MUFG-Bank",
    "hyper recruitment solutions": "Hyper-Recruitment-Solutions",
    "hyper recruitment solutions (hrs)": "Hyper-Recruitment-Solutions",
    "ing bank n.v., milan branch": "ING-Bank",
    "mbzuai (mohamed bin zayed university of artificial intelligence)": "MBZUAI",
    "enthought, inc.": "Enthought",
    "openai residency": "OpenAI-Residency",
    "openai": "OpenAI",
    "openai, inc.": "OpenAI",
    "the association of professional football analysis":
        "Association-of-Professional-Football-Analysis",
    "20250819 elevenlabs research engineer": "ElevenLabs",
    "advanced machine intelligence ": "Advanced-Machine-Intelligence",
    # a programme is its company, with the programme named in the text (the
    # crawl-independent case ingest derives an org from the case directory when
    # the dossier carries no identity: docs/41 P1b, a16z SR008 2026-10-06)
    "a16z_speedrun_sr008": "a16z",
    "a16z speedrun sr008": "a16z",
    "a16z speedrun": "a16z",
}

# Ledger rows whose `org_name` is not an organisation (a role title, a board
# label, a test fixture). They must never become memories; the crawl source
# that produced them is a separate fix (recorded in JOURNAL 2026-10-05).
NOT_AN_ORG = {
    "machine learning engineer", "software engineer", "near you",
    "recruitment advertising partnerships", "testco",
    "trading interview new york, ny",
    "experience mid (l4) · junior and senior included",
}


def slug(name: str) -> str:
    """A user- and filesystem-friendly directory name for an entity.

    Folds accents, drops trailing descriptors and locations, drops marks and
    apostrophes, spells `&` as `and`, and joins words with single hyphens:
    `Dassault Systèmes` → `Dassault-Systemes`, `Jefferies New York, NY` →
    `Jefferies`, `Legal & General` → `Legal-and-General`. The proper name stays
    in the ledger and in the memory's own text; the directory is the convenient
    form of it. Returns "" when nothing usable remains.
    """
    raw = unicodedata.normalize("NFKC", (name or "").strip())
    override = SLUG_OVERRIDES.get(raw.lower())
    if override:
        return override
    text = re.split(r"\s*\|\s*", raw)[0].strip()   # drop a trailing descriptor
    # "A / B" is one entity written two ways (the keel rows carry combined names,
    # e.g. "Advanced Machine Intelligence / AMI Labs (Yann LeCun)"); a slash with
    # no surrounding spaces is left to the non-alnum rule below
    text = re.split(r"\s+/\s+", text)[0].strip()
    text = re.sub(r"[®™©]", "", text)               # marks carry no name
    text = re.sub(r"['’`]", "", text)               # Lloyd's → Lloyds
    # a trailing ", XX" / ", Country" segment is location, not name
    text = re.sub(r",\s*[A-Za-z][A-Za-z .'-]{0,20}$", "", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("&", " and ")               # shell-safe, still English
    text = re.sub(r"[^A-Za-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-.")
    return clip_stem(text, strip="-.")


def is_org_name(name: str) -> bool:
    """False for ledger noise that is not an organisation."""
    return (name or "").strip().lower() not in NOT_AN_ORG


def _kebab(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not s:
        raise SystemExit("refusing empty slug for name: %r" % name)
    return clip_stem(s, strip="-")


def memory_dir(profile: str, *, org: str | None = None,
               person: str | None = None, agent: str | None = None) -> Path:
    """The MEMORY_DIR for one entity. Pure path resolution — creates nothing
    (creation is `mem init`'s job, via `memo init`)."""
    picked = [n for n, on in (("org", org), ("person", person),
                              ("agent", agent)) if on]
    if len(picked) > 1:
        raise SystemExit("pick one of --org/--person/--agent, not %s"
                         % " + ".join(picked))
    root = config.STATE_DIR / profile / "dataset" / "memory"
    if org:
        from company_events import canonical_company
        # the canonical ledger name, folded to a filesystem-friendly slug; a name
        # that folds to nothing falls back to the old lowercase form
        canonical = canonical_company(org)
        return root / "orgs" / (slug(canonical or org) or _kebab(org))
    if person:
        return root / "people" / _kebab(person)
    if agent:
        return root / "agents" / _kebab(agent)
    return root / "principal"


def capture_memo(memdir: Path, args: list[str]) -> str:
    """Run memo and capture stdout ('' on any failure) — for readers that
    embed a wake in their own output, like the brief's org-memory context."""
    import subprocess
    env = dict(os.environ, MEMORY_DIR=str(memdir))
    try:
        proc = subprocess.run([sys.executable, str(MEMO), *args], env=env,
                              capture_output=True, text=True)
    except OSError:
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def note_if_exists(memdir: Path, line: str) -> bool:
    """One note into one memory, only if that memory exists (the bulk
    discipline: hooks mirror, they never mint). Never raises."""
    try:
        if not (memdir / "LOG.txt").exists():
            return False
        return run_memo(memdir, ["note", line], quiet=True) == 0
    except Exception:
        return False


def note_case_lines(dataset_dir: Path, pairs: list[tuple[str, str]],
                    *, principal: str | None = None) -> int:
    """Mirror case/outcome milestones into org memories (PLAN.md T2.3).

    `pairs` = [(org display name, note line)] built by the writer that owns
    the vocabulary (it knows the age, the probability, the role). Bulk
    semantics: note-if-exists — only orgs that already have a memory are
    noted, never minted. Never raises; returns how many notes landed."""
    if not pairs:
        return 0
    principal = principal or _principal_of_dataset(dataset_dir)
    noted = 0
    for org, line in pairs:
        if note_if_exists(memory_dir(principal, org=org), line):
            noted += 1
    return noted


def _principal_of_dataset(dataset_dir: Path) -> str:
    """state/<p>/dataset → <p>; anything else falls back to lj."""
    name = Path(dataset_dir).parent.name
    return name if re.fullmatch(r"[a-z][a-z0-9-]*", name or "") else "lj"


def run_memo(memdir: Path, args: list[str], *, quiet: bool = False) -> int:
    """Run the vendored memo against one memory, passing the exit code
    through. `quiet` drops stdout (init's paste-me block, note's nap
    prompts) — for pipeline callers, never for a session's own wake."""
    import subprocess
    env = dict(os.environ, MEMORY_DIR=str(memdir))
    proc = subprocess.run([sys.executable, str(MEMO), *args], env=env,
                          stdout=subprocess.DEVNULL if quiet else None)
    return proc.returncode


_MEMO_MODULE = None


def memo_module():
    """The vendored OptMem tool, imported once — the authority for its own
    geometry (LOG_REC, ENTRY_CHARS), so an upstream change cannot be silently
    contradicted by a copy of a constant here."""
    global _MEMO_MODULE
    if _MEMO_MODULE is None:
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("optmem_memo", str(MEMO))
        spec = importlib.util.spec_from_loader("optmem_memo", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        _MEMO_MODULE = mod
    return _MEMO_MODULE


def fit_one_line(text: str, limit: int | None = None) -> str:
    """Coerce `text` into one memory line within the tool's byte budget.

    A memory is one line, and `check()` refuses anything over ENTRY_CHARS bytes —
    counting the ellipsis an agent adds when it truncates. Getting that wrong
    twice (280 - 1 + a 3-byte "…" = 282) cost a refused note and a refused nap,
    so the arithmetic lives here, once, in bytes and utf-8-safely.
    """
    limit = limit or int(memo_module().ENTRY_CHARS)
    line = " ".join(text.split())
    if len(line.encode()) > limit:
        keep = limit - len("…".encode())
        line = line.encode()[:keep].decode("utf-8", "ignore") + "…"
    return line


def _asof_record_count(memdir: Path, asof: str) -> int:
    """Records dated <= asof — a binary search over the date-ordered LOG.

    A LOG record is `#<id> <date> <text>` padded to a fixed width with the
    newline last, and dates are non-decreasing (import enforces it, note
    uses today), so a bisect over record offsets is exact. LOG_REC comes
    from the vendored tool itself rather than a copy of the constant, so a
    width change upstream cannot silently corrupt the cutoff."""
    log = memdir / "LOG.txt"
    if not log.exists():
        raise SystemExit("no memory at %s — run mem init first" % memdir)
    mod = memo_module()
    data = log.read_bytes()
    rec = mod.LOG_REC

    def date_at(i: int) -> str:
        # "#123 2026-10-04 text..." → the 10 bytes after "#<id> "
        line = data[i * rec:(i + 1) * rec]
        return line.split(b" ", 2)[1].decode("utf-8", "replace")

    lo, hi = 0, len(data) // rec
    while lo < hi:
        mid = (lo + hi) // 2
        if date_at(mid) <= asof:
            lo = mid + 1
        else:
            hi = mid
    return lo


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = args[0], args[1:]

    # entity flags, then the tool's own arguments
    profile, org, person, agent, asof = "lj", None, None, None, None
    tool_args: list[str] = []
    it = iter(rest)
    for a in it:
        if a == "--profile":
            profile = next(it, None) or ""
        elif a == "--org":
            org = next(it, None)
        elif a == "--person":
            person = next(it, None)
        elif a == "--agent":
            agent = next(it, None)
        elif a == "--asof":
            asof = next(it, None)
        else:
            tool_args.append(a)
    if not profile or not re.fullmatch(r"[a-z][a-z0-9-]*", profile):
        raise SystemExit("bad --profile %r" % profile)
    if asof and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", asof):
        raise SystemExit("bad --asof %r (want YYYY-MM-DD)" % asof)

    # the subagent rule (PLAN.md §0.4): writes are refused outright
    if cmd in ("note", "nap", "init") and os.environ.get(
            "LOVEWORK_SUBAGENT") == "1":
        print("blocked: LOVEWORK_SUBAGENT=1 — subagents never run memo",
              file=sys.stderr)
        return 75

    memdir = memory_dir(profile, org=org, person=person, agent=agent)
    entity = bool(org or person or agent)

    if cmd == "init":
        rc = run_memo(memdir, ["init"])
        if rc == 0 and entity:
            run_memo(memdir, ["config", "WAKE_LINES=%d" % ENTITY_WAKE_LINES])
        return rc

    if cmd == "wake":
        wake_args: list[str] = []
        if asof:
            t = _asof_record_count(memdir, asof)
            if t == 0:
                print("no memories dated <= %s" % asof)
                return 0
            wake_args = ["1", str(t)]
        return run_memo(memdir, ["wake", *wake_args])

    if cmd in ("note", "recall", "nap", "zoom", "forget", "config",
               "import"):
        return run_memo(memdir, [cmd, *tool_args])

    print("No such command: %s\n\n%s" % (cmd, USAGE), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
