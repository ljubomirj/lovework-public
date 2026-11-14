"""Lead identity gates (P2 — identity integrity).

The title field must be the role title and the location field a place.
Parser output is not a title: email subjects ("Results from the new
AI-powered job search — 28 new jobs match your preferences."), salary
strings ("£120000 - £150000 per annum + equity + benefits"), relative
dates ("1 day ago"), and description paragraphs are identity garbage and
must be repaired or dropped at the source — never scored and surfaced.

Centralising the gates here keeps every source (Gmail parsers, HN
parsers, aggregators) on one definition instead of drifting per parser.
"""

from __future__ import annotations

import re

# Titles that are clearly not a role: email-subject / digest lines,
# salary strings, relative dates, generic employment labels, or URLs.
TITLE_NOISE_PATTERNS = [
    # Aggregator/email-subject digests.
    r"^\s*results?\s+from\s+the\s+new",
    r"new\s+jobs?\s+match",
    r"^\s*(top\s+)?job\s+picks",
    r"^\s*new\s+recommended",
    r"^\s*jobs?\s+that\s+match\s+your",
    r"^\s*(you\s+might\s+like|recommended\s+for\s+you)",
    # Salary / comp strings used as a title.
    r"^\s*[£$€]\s?\d[\d,]*",
    r"\bper\s+annum\b",
    r"\bpa\s*\+\s*(equity|bonus|benefits)",
    r"\bsalary\b",
    # Relative dates ("1 day ago") are never a title.
    r"^\s*\d+\s+(day|days|hour|hours|week|weeks|month|months)\s+ago\b",
    # URL / location-only fallbacks.
    r"^https?://",
    r"^\s*(remote|onsite|hybrid)\s*[-–]?\s*(remote|onsite|hybrid)?\s*$",
]

# Aggregator UI strings and email-subject lines that arrive in the title
# column when a digest's layout is mis-read ("See all matching jobs", "Your
# update for July 22nd", "Post a job"). Generic employment labels are the
# same failure: they are a *contract type*, not a role (2026-10-07 audit:
# 573 such titles in lj, 215 in vj, 139 in kj).
TITLE_NOISE_PATTERNS += [
    r"^\s*(permanent|contract|contractor|full[- ]?time|part[- ]?time|"
    r"temporary|temp|internship|freelance|casual|fixed[- ]term)\s*$",
    r"^\s*(post a job|see all (matching )?jobs|view (all )?jobs|"
    r"apply now|job details|new job alert)\s*$",
    r"^\s*(your|our) update for\b",
    r"^\s*not assigned\s*$",
    r"^\s*(remote|onsite|hybrid)\s*\(utc[^)]*\)\s*$",
    r"^\s*\d[\d, ]*\s*[-–—]\s*\d[\d, ]*\s*(per|/|a)\s*"
    r"(day|week|month|year|annum|hour)\b",
]

# Company names that are not company names: a salary band, a relative age, a
# contract type, or an aggregator UI string that landed in the company column
# ("Competitive", "400 - 450 per day", "120000 - 150000 per annum equity
# benefits" — 2026-10-07 audit). The write gate routes these to
# `_Unattributed`; the audit reports the ones already on disk.
COMPANY_NOISE_PATTERNS = [
    r"^\s*[£$€]?\s*\d[\d, .]*\s*(k|K)?\s*[-–—]\s*[£$€]?\s*\d[\d, .]*\s*"
    r"(k|K)?\s*(per|/|a)\s*(day|week|month|year|annum|hour)\b",
    r"^\s*\d[\d, .]*\s*(k|K)\b",
    r"\bper\s+(annum|day|year|month|hour)\b",
    r"^\s*\d+\s+(day|days|hour|hours|week|weeks|month|months)\s+ago\b",
    r"^\s*(competitive|negotiable|market rate|depending on experience|doe)\s*$",
    r"^\s*(permanent|contract|contractor|full[- ]?time|part[- ]?time|"
    r"temporary|internship|freelance)\s*$",
    r"^\s*(not assigned|unknown|undisclosed|various|multiple|tbd|n/?a)\s*$",
    r"^\s*(post a job|see all (matching )?jobs|view (all )?jobs|"
    r"new job alert)\s*$",
    r"^\s*(your|our) update for\b",
    r"^\s*results? from the new\b",
    r"^\s*\d+\s*(new\s*)?jobs?\s+match",
]

# Locations that are actually dates, digests, or email noise.
LOCATION_NOISE_PATTERNS = [
    r"^\s*\d+\s+(day|days|hour|hours|week|weeks|month|months)\s+ago\b",
    r"^\s*(new|updated|expired|closed|filled)\s*$",
    r"^\s*\d+\s*(new)?\s*jobs?\s+match",
]

# A title and a location are short fields. Past these lengths the extractor has
# put an advert paragraph (or the whole page body) in the field — the 2026-06
# wiki pages are full of them ("The successful candidate will be based in
# London. Our offices are located at 1 Churchill Place …" as a *title*).
MAX_TITLE_WORDS = 20
MAX_LOCATION_WORDS = 12

# A company name is a short proper noun — a long prose sentence is a
# mis-split header (e.g. an em-dash inside a description paragraph).
MAX_COMPANY_WORDS = 6
MAX_ROLE_WORDS = 12
MIN_TITLE_CHARS = 4


#: Applicant-tracking hosts whose path (or subdomain) carries the **employer's own
#: slug**.
#:
#: LJ, 2026-10-18: four HuggingFace GOs sat on his active list as *"TTS Arena V2"* and
#: *"Open ASR Leaderboard"* — the names of two HF **Spaces** — because
#: `speech-and-voice-ai-companies-tracker.txt` lists those Spaces (tagged `[ARENA]`)
#: beside companies, the source treats every line as an org, and HuggingFace's own
#: adverts were then filed under the Space's name. The HuggingFace cooldown could not
#: match, so a rejected org stayed on the GO list.
#:
#: The advert's own URL carries the employer: `apply.workable.com/huggingface/j/…`,
#: `jobs.ashbyhq.com/poetiq/…`, `boards.greenhouse.io/acme/…`. That slug is the
#: employer's own filing — evidence a page title cannot overrule.
#: Value = the **0-based path segment** holding the org, or `-1` when the org is the
#: **subdomain**. (The first version mixed the two conventions and returned `"j"` for
#: `apply.workable.com/huggingface/j/…` — caught by printing the answer for six real
#: URLs rather than trusting the code to be obvious.)
ATS_ORG_HOSTS = {
    "workable.com": 0,          # apply.workable.com/<org>/j/<id>
    "ashbyhq.com": 0,           # jobs.ashbyhq.com/<org>/<id>
    "greenhouse.io": 0,         # boards.greenhouse.io/<org>/jobs/<id>
    "lever.co": 0,              # jobs.lever.co/<org>/<id>
    "gem.com": 0,               # jobs.gem.com/<org>/<id>
    "smartrecruiters.com": 0,   # jobs.smartrecruiters.com/<Org>/<id>
    "recruitee.com": -1,        # <org>.recruitee.com/o/<id>
    "bamboohr.com": -1,         # <org>.bamboohr.com/careers/<id>
    "teamtailor.com": -1,       # <org>.teamtailor.com/jobs/<id>
}


def org_from_ats_url(url: str) -> str:
    """The employer's slug from an applicant-tracking URL, or "".

    `0` in `ATS_ORG_HOSTS` means the org is the **subdomain**; any other value is the
    path-segment index. Returns the slug as the ATS spells it; matching it to an org
    name is the caller's business (`org_aliases` and the compact-key rules).
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return ""
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    segments = [seg for seg in (parts.path or "").split("/") if seg]
    for ats, index in ATS_ORG_HOSTS.items():
        if host == ats or host.endswith("." + ats):
            if index < 0:
                return host[: -len(ats) - 1] if host != ats else ""
            return segments[index] if len(segments) > index else ""
    if host.endswith(".myworkdayjobs.com"):
        return host.split(".", 1)[0]      # <org>.wd<N>.myworkdayjobs.com
    return ""


def _word_count(value: str) -> int:
    return len(re.findall(r"\S+", value or ""))


def is_noise_title(title: str) -> bool:
    """True when a parsed title is not a role title (subject/salary/date/prose)."""
    value = (title or "").strip()
    if len(value) < MIN_TITLE_CHARS:
        return True
    lowered = value.lower()
    if re.search(r"https?://", lowered):
        return True
    if _word_count(value) > MAX_TITLE_WORDS:
        return True
    # A place is not a role. The extractor sometimes takes the location for the
    # position — lj had a GO titled **"Madrid, Spain"** (CNIO), and the hourly
    # pack pass dutifully prepared a dossier for a city (2026-10-14). The
    # vocabulary is the one that already decides whether a *company* name is a
    # place (`_is_place_only`), so both questions get one answer: every
    # significant word is a place word and there is no role noun. "Sales
    # Manager, Madrid" and "Machine Learning Engineer" are roles and stay.
    if _is_place_only(value):
        return True
    return any(re.search(p, lowered) for p in TITLE_NOISE_PATTERNS)


def is_noise_company(name: str) -> bool:
    """True when a parsed company name is not a company name.

    The company column of a digest or an aggregator row can hold the salary
    band, the posting age, the contract type, or a UI string — and a page
    named after it is a page nobody can ever find ("Competitive.md" held 151
    blocks, "400_-_450_per_day.md" 20). The write gate folds these onto
    `_Unattributed`; this predicate is the single definition both the gate
    and the audit use.
    """
    value = (name or "").strip()
    if not value:
        return True
    lowered = value.lower()
    if re.search(r"https?://|www\.", lowered):
        return True
    return any(re.search(p, lowered) for p in COMPANY_NOISE_PATTERNS)


def is_noise_location(location: str) -> bool:
    """True when a parsed location is not a place (date/digest/prose noise)."""
    value = (location or "").strip()
    lowered = value.lower()
    if not lowered:
        return False  # empty is fine — omitted, not mis-labelled
    if _word_count(value) > MAX_LOCATION_WORDS:
        return True
    return any(re.search(p, lowered) for p in LOCATION_NOISE_PATTERNS)


def is_implausible_header(company: str, role: str) -> bool:
    """True when a split header is probably a mis-split description.

    ``_split_header`` treats "|", "–", "—" as separators, so an em-dash
    inside a description paragraph (e.g. "...happy customers — and now
    we're ready to accelerate.") yields a prose company / prose role.
    A company is a short proper noun; a role is a short noun phrase.
    """
    company_words = re.findall(r"\S+", company or "")
    role_words = re.findall(r"\S+", role or "")
    if len(company_words) > MAX_COMPANY_WORDS:
        return True
    if len(role_words) > MAX_ROLE_WORDS:
        return True
    # A role that is really a sentence fragment (verb-led, long tail)
    # is a mis-split; keep it conservative and cheap.
    if re.search(r"\b(and|because|however|which|that|our)\b", (role or "").lower()):
        return len(role_words) >= 8
    # A company that names a job is a header with no company at all
    # ("Lead SWE | Remote | ..." — the 2026-10-07 audit's "Lead_SWE" page).
    # Role NOUNS only: "Lead Forensics" stays a company, "Lead SWE" does not.
    if re.search(r"\b(engineer(?:ing)?|developer|scientist|designer|analyst|"
                 r"researcher|architect|manager|swe|founder)s?\b",
                 (company or ""), re.I):
        return True
    return False


# ── is this an organisation at all? ──────────────────────────────────────
#
# One predicate, used by the extraction gate, the wiki page-minting gate and
# the wiki audit. It exists because the 2026-06/07 wikis minted ~111 pages named
# after something that is not an organisation: a sentence from the advert body
# ("Barclays payments acceptance business provides critical infrastructure …"),
# a salary string ("£120000 - £150000 per annum + equity"), a mail-digest line
# ("Results from the new AI-powered job search"), a URL ("LAGO - https://
# getlago.com/hiring/jobs"), a street address ("15001 Kercheval Ave"), an
# opaque token, or a bare role ("senior-python-backend-engineer").

#: A name carrying a URL or a domain tail. A *bare* domain ("Checkout.com",
#: "IPinfo.io") is a legitimate company name and must survive — over-matching it
#: was the audit's own false class (2026-10-07). So the defect is a name PLUS a
#: tail: a separator, then a URL, or a hostname followed by a path.
_URL_TAIL_RE = re.compile(
    # A name, a separator, then a URL or a hostname-with-path. The leading
    # `\S` is the whole point: a BARE domain is a company name ("Checkout.com",
    # "FAR.AI", "IPinfo.io"), and matching those was this audit's own false
    # class (2026-10-07) — the defect is a name *plus* a tail.
    r"\S[\s_-]+(?:https?://|www\.)"
    r"|\S[\s_-]+[A-Za-z0-9-]+\.(?:com|org|net|io|ai|co|dev|pro|health|tech|app)"
    r"(?:[/\s_-]|$)",
    re.I,
)

#: A salary or compensation string used as a name.
_SALARY_RE = re.compile(
    r"per\s+annum|per\s+day|per\s+month|\bsalary\b|\bcompensation\b"
    r"|\d{2,3}[,\s]?\d{3}\s*(?:-|–|to)\s*\d"
    r"|[£$€]\s?\d|\bup\s+to\s+\d",
    re.I,
)

#: An email subject / digest / alert line used as a name.
_ALERT_RE = re.compile(
    r"results?\s+from|jobs?\s+match|match\s+your|job\s+picks|recommended\s+for\s+you"
    r"|notifications?\s+when|manage\s+alerts|change\s+frequency|unsubscribe"
    r"|saved\s+job|new\s+job\s+alert|jobs?\s+that\s+match|your\s+search\s+preferences",
    re.I,
)

#: A postal address used as a name ("15001 Kercheval Ave", "110 Southwark Street").
_ADDRESS_RE = re.compile(
    r"^\d+\s+\S+(?:\s+\S+){0,3}\s+"
    r"(?:ave|avenue|street|st|road|rd|lane|ln|way|drive|dr|blvd|boulevard|square|court|place)\b"
    r"|\b[A-Z]{1,2}\d{1,2}\s*\d[A-Z]{2}\b",       # UK postcode
    re.I,
)

#: One long opaque token — a hash or a base64 id used as a name. A long
#: underscored *name* is not opaque: the token must carry digits, which is what
#: separates an id from "XPT_Software_Australia_Pty_Ltd" (31 chars, no digits).
#: Generic nouns that describe a kind of business rather than naming one.
_GENERIC_DESCRIPTOR_RE = re.compile(
    r"\b(startup|start-up|company|employer|firm|agency|platform|business|"
    r"organisation|organization|group of companies)\b",
    re.I,
)

_OPAQUE_RE = re.compile(r"^(?=[A-Za-z0-9_-]{30,}$)(?=(?:\D*\d){6,})[A-Za-z0-9_-]+$")

#: Words that join the parts of a real company name and are legitimately
#: lowercase. Anything else lowercase means the "name" is a sentence.
_NAME_CONNECTORS = frozenset({
    "of", "and", "the", "for", "in", "on", "to", "a", "an", "at", "by", "with",
    "de", "van", "von", "der", "den", "und", "en", "y", "e", "la", "le", "du",
    "di", "da", "el", "los", "las", "et", "al", "bin", "ben", "san", "santa",
    "&", "+", "-", "–", "—", "/", "|", "x",
    # apostrophe artefacts: "Domino_s_Pizza" is Domino's, not a sentence
    "s", "t", "d", "m", "ll", "re", "ve",
    # encoding artefacts: "Children x27 s Research Hospital", "Tesco nbsp Welwyn"
    "x27", "nbsp", "amp", "quot",
})

#: A role noun makes a name role-shaped; an org-signal word makes it a company.
_ROLE_NOUN_RE = re.compile(
    r"\b(engineer(?:ing)?|developer|scientist|designer|analyst|researcher|"
    r"architect|manager|swe|founder|consultant|director|intern|specialist|"
    r"officer|administrator|technician|associate|assistant)s?\b",
    re.I,
)
_ORG_SIGNAL_RE = re.compile(
    r"\b(ltd|limited|inc|llc|plc|gmbh|ag|se|co|corp|corporation|group|holdings|"
    r"sa|nv|bv|oy|ab|aps|srl|spa|pty|llp|kg|ug|sarl|trust|foundation|university|"
    r"institute|hospital|centre|center|college|school|council|authority|company|"
    r"partners|consulting|engineering|recruitment|talent|technologies|technology|"
    r"solutions|systems|services|studios|media|capital|ventures|bank|insurance|"
    r"labs?|research|network|alliance|academy|society|union|federation|agency|"
    r"associates|consultants|staffing|resourcing|accountants|solicitors|advisors|"
    r"publishers|press|books|theatre|cinema|hospitality|logistics)\b",
    re.I,
)


#: Words that may appear in a *place* and nowhere else useful. Used to answer
#: "is this whole name a location?" — `Welwyn England United Kingdom`,
#: `San Francisco California United States`, `London`, `NYC` are all places
#: mis-read as companies, while `Bank Of England` and `England Rugby` are real
#: names that merely contain one.
_PLACE_WORDS = frozenset("""
remote onsite hybrid worldwide anywhere europe emea uk us usa
united states kingdom england scotland wales ireland britain
germany france spain italy poland sweden norway denmark switzerland netherlands
belgium luxembourg austria portugal canada australia india singapore japan china
israel brazil mexico
london berlin paris madrid barcelona milan rome amsterdam zurich geneva basel
cologne munich frankfurt dublin edinburgh manchester cambridge oxford bristol
welwyn springfield st albans
new york york san francisco california texas boston seattle austin chicago
toronto vancouver sydney melbourne tokyo bangalore hyderabad
nyc sf la dc
area city region country
north south east west central greater
""".split())


def _is_place_only(value: str) -> bool:
    """True when a whole name is a place, not a company name.

    Every significant word must be a place word: `England Rugby` and
    `Bank Of England` are names (Rugby and Bank are not places), while
    `Welwyn England United Kingdom` is a location with nothing else in it.
    """
    words = [w for w in re.findall(r"[A-Za-z]+", (value or "").lower())]
    if not words:
        return False
    return all(w in _PLACE_WORDS for w in words)


#: US state / territory codes. A trailing "<City> <ST>" is a location; a
#: trailing "<Word> <XX>" is not, so the code set keeps the rule precise
#: (`Leerink Partners New York NY` → `Leerink Partners`).
_US_STATE_CODES = frozenset("""
AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO
MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY
DC PR VI GU AS MP
""".split())


#: Words that begin a multi-word place name ("New York", "San Francisco").
#: After the city and state are dropped, one of these left dangling is part of
#: the place too, so it goes with them.
_PLACE_PREFIX_WORDS = frozenset({
    "new", "san", "los", "las", "el", "la", "st", "saint", "fort", "port",
    "mount", "north", "south", "east", "west", "bay", "costa", "santa", "villa",
})


def _strip_city_state_tail(text: str) -> str:
    """Drop a trailing "<City> <ST>" — a location, not part of the name."""
    words = text.split()
    # The code must be *upper-case in the source*: "Co" is a company suffix as
    # often as it is Colorado ("AB3 Nationwide Life And Annuity Insurance Co").
    if (len(words) >= 4 and len(words[-1]) == 2 and words[-1].isupper()
            and words[-1] in _US_STATE_CODES and words[-2][:1].isupper()):
        words = words[:-2]
        if len(words) >= 2 and words[-1].lower() in _PLACE_PREFIX_WORDS:
            words = words[:-1]
    return " ".join(words)


def clean_org_name(name: str) -> str:
    """Strip the tails a source concatenated onto a company name.

    Recovers the organisation the entry belongs to, so a repair can file it
    where it belongs instead of discarding it:

        "Tesco&nbsp;Welwyn Garden City England United Kingdom" -> "Tesco"
        "MeanderX formerly Orbio Earth (YC S23)"               -> "MeanderX"
        "LAGO - https://getlago.com/hiring/jobs"               -> "LAGO"
        "Twindo - Formerly Canvas The Spatial Computing Co"    -> "Twindo"
        "MBZUAI (Mohamed bin Zayed University of AI)"          -> "MBZUAI"
        "Jefferies New York NY"                                -> "Jefferies"
        "____BIT Capital___Berlin__Germany__ONSITE_2 days"     -> "BIT Capital"
        "St. Jude Children x27 s Research Hospital"            -> "St. Jude Children's Research Hospital"

    Returns the cleaned name; equal to the input when there is nothing to strip.
    A country word *inside* a name is left alone — `Raytheon UK`, `Accenture UK
    Ireland`, `Bank Of England` are names, not name-plus-location.
    """
    raw = (name or "").strip()
    if not raw:
        return raw

    # 1. Join markers, read on the raw form.
    #    A *run* of underscores marks a join ("____BIT Capital___Berlin__…");
    #    a single `_` is how every stem separates words, so treating it as a
    #    join would cut real names in half.
    if re.search(r"_{2,}", raw):
        parts = [p for p in re.split(r"_{2,}", raw.strip("_")) if p.strip()]
        kept = [p for p in parts if not _is_place_only(p.replace("_", " "))]
        if kept:
            raw = kept[0]

    text = raw.replace("_", " ")
    #    `&nbsp;`/`nbsp` is a non-breaking space the source inserted, so it marks
    #    the same join ("Tesco&nbsp;Welwyn Garden City …").
    if re.search(r"&nbsp;?|\bnbsp\b", text, flags=re.I):
        head = re.split(r"&nbsp;?|\bnbsp\b", text, flags=re.I)[0]
        if len(head.strip(" -.")) >= 2:
            text = head

    # 2. Apostrophe artefacts before anything else reads the words.
    text = re.sub(r"&#?x?27;?|\bx27\b", "'", text, flags=re.I)
    text = re.sub(r"\b([A-Z][A-Za-z]*) s\b", r"\1's", text)
    text = re.sub(r"\s+'\s+s\b", "'s", text)

    # 3. A URL, or a hostname with a path, ends the name — but the tail must be
    #    *attached to a name*: "LAGO - https://getlago.com/…" and
    #    "amodo-design-amododesign.com" are name-plus-tail, while "Checkout.com"
    #    and "FAR.AI" are the company's own name and must survive.
    text = re.sub(r"(?<=\S)[\s_-]+(?:https?://|www\.)\S+.*$", "", text, flags=re.I)
    text = re.sub(r"(?<=\S)[\s_-]+[A-Za-z0-9.]+\.(?:com|org|net|io|ai|co|dev|pro"
                  r"|health|tech|app|xyz|store|shop)(?:[/\s].*)?$", "", text, flags=re.I)

    # 4. Descriptors: a display alias, a parenthetical, "formerly"/"t-a", or a
    #    " - descriptor" tail.
    text = re.split(r"\s*[|]\s*", text)[0]
    text = re.split(r"\s+/\s+", text)[0]
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text)
    text = re.sub(r"\s*[-–—]?\s*\b(?:formerly|fka|aka|t/?a|dba|now)\b.*$", "", text, flags=re.I)
    text = re.sub(r"\s+[-–—]\s+.*$", "", text)

    # 5. A trailing location: ", City, ST", "City ST", a work-mode marker, or a
    #    leading country code.
    text = re.sub(r"\s*,\s*[A-Za-z][A-Za-z .'’-]{0,24}$", "", text)
    text = _strip_city_state_tail(text)
    text = re.sub(r"\s+(?:ONSITE|REMOTE|HYBRID)\b.*$", "", text, flags=re.I)
    # A leading country code is NOT stripped: "US Foods Inc" and "UK Civil
    # Service" are names, and no rule separates them from "USA Solventum …".

    # 6. Source-specific markers the extractors appended to a name.
    #    Deliberately NOT a general "cut at the first lowercase word" rule: it
    #    turned "Mercury Insurance Company" into "Mercury Insurance" and
    #    "Don't let this job opportunity pass by" into "Don't". A prose name is
    #    not repaired by guessing at a head — the repair recovers it only from
    #    an existing page (see wiki_org_repair).
    text = re.sub(r"\s+YC\s+S\d+\b.*$", "", text)
    text = re.sub(r"\s+Series\s+[A-Z]\b.*$", "", text)
    text = re.sub(r"\s+Ex-[A-Z][A-Za-z]*\b.*$", "", text)

    return re.sub(r"\s{2,}", " ", text).strip(" -–—,.'")


def _looks_like_prose(text: str) -> bool:
    """True when a name reads as a sentence, not as a proper noun.

    A real company name capitalises every word that is not a connector
    ("Aioi Nissay Dowa Insurance Company Of Europe SE"); a sentence does not
    ("Infor is where ambition meets impact").
    """
    # Split on anything that is not a word character, so a kebab-cased sentence
    # ("we-use-web-data-to-identify-things") is read as the words it is. `\w` is
    # Unicode-aware, which matters: splitting on ASCII only cut "Jülich" into
    # "J" + "lich", and the fragment then read as a lowercase-starting word —
    # so `QCG Computer (Jülich) GmbH`, a real company, was called a sentence.
    words = [w for w in re.split(r"[^\w'’]+", text or "", flags=re.UNICODE) if w]
    if len(words) < 5:
        return False
    for word in words:
        core = word.strip("_-.,;:!?()[]{}'’“”\"")
        if not core or core.lower() in _NAME_CONNECTORS:
            continue
        if core[0].islower() and core.isascii():
            return True
    return False


def is_non_org_name(name: str) -> bool:
    """True when a "company" name is not an organisation at all.

    The gates above run on the *cleaned* name, so a recoverable tail (a domain,
    a location, an alias) is not mistaken for a non-org: `Tesco&nbsp;Welwyn …`
    cleans to `Tesco` and survives, while `Results from the new AI-powered job
    search` has no organisation in it at all.

    Used by three callers, one definition: the extraction gate (do not create a
    listing), the wiki page-minting gate (route to `_Unattributed`), and the
    audit (report the class).
    """
    raw = (name or "").strip()
    if len(raw.strip("_-. ")) < 2:
        return True
    if _URL_TAIL_RE.search(raw):
        return True
    if _OPAQUE_RE.match(raw):
        return True
    # A name that is only a number (or a currency amount) is a salary fragment
    # the cleaner cut down to its leading figure ("400 - 450 per day" → "400").
    if re.fullmatch(r"[£$€\d.,\s+-]+", raw.strip()):
        return True
    cleaned = clean_org_name(raw)
    if len(cleaned.strip("_-. ")) < 2:
        return True
    # Every test runs on the raw name as well as the cleaned one, because
    # cleaning is lossy by design: it cuts a " - " tail, which would otherwise
    # turn "400 - 450 per day" into the apparently-innocent name "400".
    flat = re.sub(r"[_]+", " ", raw)
    forms = (cleaned, flat)
    if any(p.search(f) for p in (_SALARY_RE, _ALERT_RE, _ADDRESS_RE) for f in forms):
        return True
    if _is_place_only(cleaned):
        return True
    if any(_looks_like_prose(f) for f in forms):
        return True
    # A short descriptor is not a name either: "Robotics startup STEALTH",
    # "Audio Video Collaboration company worldwide", "Early-stage health
    # wellness startup". The generic noun must be lower-case — "Mercury
    # Insurance Company" and "The Tiny Pharma Company" are real names.
    if (_GENERIC_DESCRIPTOR_RE.search(cleaned)
            and any(w[:1].islower() for w in re.split(r"[^A-Za-z]+", cleaned) if w)):
        return True
    # A bare role ("senior-python-backend-engineer", "Full Stack Software
    # Engineer") is a role, not a company. A role *plus* an org signal is a
    # mis-split of a real company ("DeepMind Student Researcher"): still not an
    # organisation, but the repair can recover the org from the name.
    if is_role_shaped_name(raw):
        return True
    return False


# ── where a job physically sits (the "work location allow-list" rule) ────
#
# The country-exclusion kill in matcher.py asks "is this location exclusively
# in an *excluded* country?" — an EXCLUDE-list. That cannot express LJ's
# constraint, which is an ALLOW-list: his body may only be in the UK or MK
# (work_auth.md), so an advert that sits in Paris is ineligible no matter how
# good the role looks. LJ's 2026-09-23 file said exactly that ("EU-located
# roles — not a green signal on their own"), the matcher *read* it (the
# 2026-10-03 Gradium reasoning cites "the Paris location is a material gate —
# no EU free movement"), and still scored GO 9.7 with six packs prepared.
#
# So: one classifier for a location string, used by the matcher's kill, the wiki
# audit's class and the link re-check — because a second spelling of "where is
# this job" is how this ran for three weeks.

#: A place that is "wherever I am" — never a physical site.
REMOTE_SIGNAL_RE = re.compile(
    r"(?<![a-z])(remote|wfh|work[ -]from[ -]home|anywhere|worldwide|"
    r"global|distributed|telecommut)(?![a-z])", re.I)

#: Places inside LJ's allow-list (UK and MK). Deliberately generous: an unknown
#: token is never a kill, because a kill that fires on a place it did not
#: recognise would drop real work.
# Longest-first: "richmond upon thames" is a London borough, and the US regex's
# bare "richmond" would otherwise read it as Richmond VA — the false positive
# that made the country rule drop a UK role for VJ (found 2026-10-09 while
# sizing the class).
ALLOWED_PLACE_RE = re.compile(
    r"(?<![a-z])(uk|u\.k\.|united kingdom|england|scotland|wales|"
    r"northern ireland|"
    r"richmond upon thames|milton keynes|newcastle upon tyne|"
    r"kingston upon thames|kingston upon hull|stoke[- ]on[- ]trent|"
    r"har?p?enden|london|leeds|manchester|birmingham|bristol|cambridge|"
    r"oxford|edinburgh|glasgow|cardiff|belfast|brighton|nottingham|"
    r"liverpool|sheffield|bath|reading|york|newcastle|leicester|coventry|"
    r"sunderland|wolverhampton|plymouth|southampton|portsmouth|norwich|"
    r"exeter|bath|chester|durham|lancaster|preston|blackpool|huddersfield|"
    r"wakefield|doncaster|derby|shrewsbury|telford|swindon|slough|watford|"
    r"harrow|wembley|ealing|ilford|romford|brentford|kingston|croydon|"
    r"hackney|islington|camden|westminster|kensington|chelsea|fulham|"
    r"surrey|kent|essex|sussex|hampshire|hertfordshire|berkshire|oxfordshire|"
    r"cambridgeshire|buckinghamshire|yorkshire|lancashire|cheshire|"
    r"derbyshire|nottinghamshire|staffordshire|warwickshire|leicestershire|"
    r"norfolk|suffolk|devon|cornwall|somerset|gloucestershire|wiltshire|"
    r"dorset|bedfordshire|mk|macedonia|skopje|"
    r"uk-wide|uk based|uk-based|uk remote)(?![a-z])", re.I)

#: A named physical place outside the UK/MK — a city or a country. Used to
#: answer "does this advert name a workplace I cannot be in?", never to
#: classify an employer's country (a French company hiring remote is fine).
#: The regex above carries two branches on purpose: a **US postal-code shape**
#: (", CA" — a curated city list can never be complete, and it missed Los Altos,
#: where two of the biggest AI labs sit; LJ's Poetiq case, 2026-10-14) and the
#: **named places**. The comma is what makes the shape a location rather than
#: prose, and it is why that branch cannot carry the "not preceded by a letter"
#: guard the names branch needs.

NON_UK_PLACE_RE = re.compile(
    # Two branches, each with its own guard: the state shape begins with a comma
    # (so the "not preceded by a letter" lookbehind must not apply to it — it
    # always follows a letter: "Somewhere, TX"), and the names branch keeps that
    # guard so "paris" inside a word cannot match.
    r"(?:,\s*(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn|"
    r"ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|"
    r"wi|wy|dc)(?![a-z])"
    r"|(?<![a-z])(paris|france|lyon|marseille|berlin|munich|m(?:ue|u)nichen|"
    r"hamburg|frankfurt|cologne|stuttgart|germany|deutschland|"
    r"stockholm|sweden|oslo|norway|helsinki|finland|copenhagen|denmark|"
    r"amsterdam|netherlands|rotterdam|utrecht|brussels|belgium|"
    r"madrid|barcelona|spain|lisbon|portugal|rome|milan|italy|"
    r"zurich|geneva|switzerland|vienna|austria|warsaw|krakow|poland|"
    r"prague|czech|budapest|hungary|bucharest|romania|sofia|bulgaria|"
    r"dublin|ireland|luxembourg|athens|greece|tallinn|riga|vilnius|"
    r"united states|u\.?s\.?a?\b|usa|canada|toronto|vancouver|montreal|"
    r"austin|seattle|boston|chicago|denver|atlanta|portland|pittsburgh|"
    r"san diego|los angeles|silicon valley|bay area|palo alto|mountain view|"
    r"new york|nyc|brooklyn|jersey city|philadelphia|washington|houston|"
    r"dallas|phoenix|minneapolis|miami|salt lake|san jose|baltimore|"
    r"bangalore|india|hyderabad|pune|tokyo|japan|singapore|sydney|"
    r"melbourne|australia|tel aviv|israel|dubai|abudhabi|zurich|"
    # The Bay Area cluster and the metros a US on-site advert usually names.
    r"los altos|menlo park|redwood city|sunnyvale|santa clara|cupertino|"
    r"san francisco|oakland|berkeley|bellevue|redmond|culver city|"
    r"santa monica|ann arbor|raleigh|durham|charlotte|nashville|"
    r"st\.? louis|kansas city|cincinnati|cleveland|detroit|indianapolis|"
    r"columbus|milwaukee|orlando|tampa|las vegas|tucson|memphis)(?![a-z]))",
    re.I)

#: A place named only by a region/state without a city ("EMEA", "APAC",
#: "Americas") is not a workplace decision — leave it to the model.
VAGUE_REGION_RE = re.compile(r"^[A-Z]{2,6}$")


def classify_work_location(location: str) -> tuple[bool, bool, list[str]]:
    """(is_remote, names_an_allowed_place, other_physical_places).

    Read a location field the way a work-location rule must: is it "wherever I
    am", does it name somewhere in the allow-list, and which other physical
    places it names. An empty or vague field returns (False, False, []) — the
    model judges that one.
    """
    text = (location or "").strip()
    if not text:
        return (False, False, [])
    is_remote = bool(REMOTE_SIGNAL_RE.search(text))
    allowed = bool(ALLOWED_PLACE_RE.search(text))
    others = [m.group(0).strip() for m in NON_UK_PLACE_RE.finditer(text)]
    return (is_remote, allowed, others)


def is_ineligible_work_location(location: str) -> bool:
    """True when an advert's workplace is a physical place LJ may not work in.

    Fires only when the field names a non-UK physical place AND no remote signal
    AND no allowed place — the permissive reading the other kills use: anything
    ambiguous is left to the model, and a location that also names London is
    never killed on the strength of its other names.
    """
    is_remote, allowed, others = classify_work_location(location)
    return bool(others) and not is_remote and not allowed


#: Query parameters that name **which** record a URL is, not how we got there.
#:
#: `wiki_store.clean_url` removes the tracking parameters; what remains is either
#: identity (`id`, `jobid`, `vacancyid`) or browsing state (`position`, `pageNum`,
#: `q`, `source`). Keeping the first is what makes the key injective; keeping the
#: second would split one advert into several.
_ADVERT_ID_PARAMS = frozenset({
    "id", "jid", "job", "jobs", "jobid", "job_id",
    "posting", "postingid", "position_id", "vacancy", "vacancyid",
    "advert", "advertid", "gh_jid", "ashby_jid",
})


def advert_url_key(url: str) -> str:
    """The same advert, however its URL is spelled — one identity for matching.

    A verdict recorded against `https://www.linkedin.com/comm/jobs/view/4446786931/`
    must attach to a pack whose link is `https://www.linkedin.com/jobs/view/4446786931/`:
    they are the same advert, and until this existed they were two strings that
    never met (measured 2026-10-14 — the re-check had recorded *"advert closed: no
    longer accepting applications"* on 2026-09-12 and a fresh GO pack was cut for
    that advert three weeks later).

    Normalises what is *not* the advert: the scheme, a `www.` prefix, LinkedIn's
    `/comm` mail/campaign prefix, a trailing slash, and tracking parameters
    (`wiki_store.clean_url` already owns that last one, and this calls it rather
    than repeating it).

    **The query is not tracking.** This used to drop the whole query string, which
    made the key silently non-injective: every `news.ycombinator.com/item?id=…`
    link collapsed to `https://news.ycombinator.com/item`, so all HN-hiring adverts
    shared one identity (measured 2026-10-14 — 1,199 of them, and it was also why
    the wiki audit could not match a retired HN advert against its pile, since the
    pile stores canonical keys and the block holds the raw URL). `clean_url` keeps
    every parameter that is not tracking, and now so do we — **but only the ones
    that name a record**, because a browsing parameter is not the advert:
    `?position=1&pageNum=0` is where the advert sat on a page, not which advert it
    is. The identity set is drawn from the corpus, not guessed: `id` appears 1,204
    times (1,199 `news.ycombinator.com`, i.e. distinct threads), then `ashby_jid`
    65, `jobid` 34, `vacancyid` 30.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    from wiki_store import clean_url

    text = clean_url((url or "").strip())
    if "://" not in text:
        return text
    parts = urlsplit(text)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path
    # LinkedIn's `/comm/` (mail and campaign links) is not part of the advert.
    for prefix in ("/comm/", "/comm"):
        if path.startswith(prefix):
            path = path[len(prefix) - 1:] if prefix.endswith("/") else ""
            break
    path = path.rstrip("/") or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query,
                                                          keep_blank_values=True)
                             if k.lower() in _ADVERT_ID_PARAMS))
    return urlunsplit(("https", host, path, query, ""))


#: An organisation that *relays* adverts rather than hiring for itself.
#:
#: LJ, 2026-10-15, on his first GO row: *"Enigma is a recruiter - not a company
#: hiring itself … How do we treat other recruiting orgs? Presumably we need to
#: distinguish between the org hiring and org recruiting?"*
#:
#: The registry already declares a **source** tier (primary = the hiring party's own
#: publication; secondary = boards, alert mailboxes, recruiters, aggregators). That
#: describes where *we found* the advert, and it does not answer this question:
#: Enigma's roles arrived through `company_pages` — a **primary** source, because LJ
#: keeps its site in his list — while `enigma-rec.ai` is an agency, so every "this is
#: the employer's own page" assumption behind that tier is wrong for it.
#:
#: **This is a label, not a filter.** The patterns fire on ~33 of the corpus's org
#: names and eyeballing them finds real agencies (Arthur Recruitment, Proclinical
#: Staffing, Understanding Recruitment, Hyper Recruitment Solutions) beside a few
#: false positives ("Deloitte - Recruitment" is Deloitte's own hiring page). Nothing
#: is dropped or demoted on this signal: the row and the pack *say* who is
#: advertising, and the principal decides what that is worth.
_AGENCY_NAME_RE = re.compile(
    r"\b(recruit(?:ment|er|ers|ing)?|staffing|headhunt(?:ing|er)?|resourcing|"
    r"search\s*&?\s*selection|talent\s+(?:solutions|partners|acquisition|consulting))\b",
    re.I)

#: A domain that says "recruitment" in its own spelling: `enigma-rec.ai` is the case
#: that started this. Deliberately narrower than the name rule — most `careers.`
#: hosts are the employer's own site, and `talent` alone flags an employer's talent
#: community (`aviva.talent-community.com`), so both are excluded.
_AGENCY_DOMAIN_RE = re.compile(r"(?:^|[.\-])(rec|recruit(?:ment|er)?|staffing|headhunt)(?:[.\-]|$)",
                               re.I)


def kind_for_org(principal: str, org: str, url: str = "") -> str:
    """The organisation's kind **as its wiki page records it** — the one reader.

    A caller that has a principal and an organisation name should ask this rather
    than the rules: the page carries the decision, its date and its reason, and the
    page is what survives a merge between hosts. Falls back to the rules when the
    page has no marker (a page nobody has classified yet).
    """
    try:
        import config
        from wiki_store import org_page_for

        page = org_page_for(config.STATE_DIR / principal / "wiki", _org_page_name(org))
        if page is not None and page.is_file():
            return org_kind(org, url, page.read_text(encoding="utf-8",
                                                     errors="ignore"))
    except Exception:                          # a missing page is not an error
        pass
    return org_kind(org, url)


def _org_page_name(org: str) -> str:
    """The page filename for an organisation — `wiki_store`'s own rule."""
    from wiki_store import WikiStore

    return WikiStore._safe_filename(org)


#: The kinds of organisation LoveWork meets. **A different type of entity**, not a
#: flag on a company (LJ, 2026-10-15): *"we should not hard-code as there will be
#: more. Best we treat it as a different type of entity, separate from principal
#: (candidate) and company (hiring)."*
#:
#:   `company`  the hiring organisation — what the wiki's org pages have always been
#:   `agency`   an intermediary: recruitment, staffing, headhunting. It advertises
#:              roles it does not own, and often does not name the client.
#:   `board`    a job board or aggregator: it publishes other people's adverts at
#:              volume and is not a party to the hiring at all.
#:
#: A principal (candidate) is the fourth kind and lives in `profiles/`, not here.
ENTITY_KINDS = ("company", "agency", "board")

#: How the kind is **recorded**: one marker on the organisation's wiki page.
#:
#: `<!-- lovework:entity: agency | set 2026-10-15 by rule:name -->`
#:
#: The kind is a **fact about an organisation, with a date and a reason** — so it
#: belongs in the page that holds what we know about that organisation, exactly as
#: `work_auth.md` holds the principal's allowed workplaces. LJ: *"the list of
#: agencies should not be in the code … So we keep history, but we can also
#: distinguish company from agency."* A code constant cannot say who decided, when,
#: or on what evidence; a marker can, and it travels with the page through every
#: seal and every merge.
ENTITY_MARKER_RE = re.compile(
    r"<!--\s*lovework:entity:\s*([a-z]+)\s*(?:\|\s*([^>]*?))?\s*-->")

#: Rules propose; a marker **decides**. This is the order everywhere.
def org_kind(org: str, url: str = "", page_text: str = "") -> str:
    """The organisation's kind: the **recorded** one if the page has it, else a rule.

    The marker wins because it is a decision someone made and can be argued with;
    a rule is a guess that happens to be cheap. Where there is no marker the rules
    answer, so a page that has never been classified still behaves sensibly — and
    `entity_kind.py` is what turns a rule's answer into a recorded fact.
    """
    recorded = recorded_entity_kind(page_text)
    if recorded:
        return recorded
    if _AGENCY_NAME_RE.search(org or ""):
        return "agency"
    host = re.sub(r"^https?://([^/]+).*$", r"\1", (url or "").strip()).lower()
    if host and _AGENCY_DOMAIN_RE.search(host):
        return "agency"
    return "company"


def recorded_entity_kind(page_text: str) -> str:
    """The kind a page records, or "" when it records none."""
    found = ENTITY_MARKER_RE.search(page_text or "")
    if found and found.group(1) in ENTITY_KINDS:
        return found.group(1)
    return ""


def entity_marker(kind: str, *, set_on: str, by: str) -> str:
    """The marker line, with its provenance — never a bare label."""
    return f"<!-- lovework:entity: {kind} | set {set_on} by {by} -->"


def is_role_shaped_name(name: str) -> bool:
    """True when a name is made of role words and carries no org signal.

    The narrow rule, extracted so three callers share one definition: the
    extraction gate (`is_non_org_name`), the wiki audit's `role_in_org_name`
    class, and the repair tool's name gate. The audit used
    `is_implausible_header`, whose "more than six words" test flagged real
    companies as role names — "Aioi Nissay Dowa Insurance Company Of Europe SE"
    (8 words) and "MBZUAI Mohamed bin Zayed University of Artificial
    Intelligence" were reported as defects for a year (2026-10-08 audit). A
    *long* name is not a defect; a name with a role noun and no organisation
    word ("Lead SWE", "Software Engineer - Full Stack") is.
    """
    cleaned = clean_org_name(name)
    if len(cleaned.strip("_-. ")) < 2:
        return False
    forms = (cleaned, re.sub(r"_+", " ", name or ""))
    return (any(_ROLE_NOUN_RE.search(f) for f in forms)
            and not _ORG_SIGNAL_RE.search(cleaned))
