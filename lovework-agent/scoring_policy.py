"""Run-level scoring policy: deterministic rules applied after scoring, before writing.

Per-advert overrides that must beat the LLM (staleness, reapply cooldown, US
work-authorization) live in `matcher.py`, where they run against a single
`MatchResult`. The rules here are different in kind: they need the *whole run*
(the entries as collected) and signals the matcher never sees — the source's own
`seniority` metadata, the entry's location string — so they run once, over the
run, just before the report, wiki pages and review packs are written.

They exist because a profile *stating* a constraint is not enough. VJ's profile
has said "early-career principal, junior/graduate roles are positives" since the
beginning, and his 2026-09-22 sweep still returned senior-titled roles (with
`Seniority: senior` from the source's own metadata) in MAYBE while comparable
mid-level roles sat in DROP — LJ's judgement: "it's unlikely that someone looking
for a senior candidate will pick a junior graduate" (2026-09-22).

Profiles declare policy in machine-readable HTML comments inside their own docs,
the same idiom as the work-authorization exclusion marker::

    <!-- lovework:seniority: early-career -->
    <!-- lovework:cap-countries: BG -->

Absent markers mean "no opinion", so a profile that says nothing is untouched.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: The matcher's eligibility rule, re-applied where the entry's own
#: location/URL are available (imported lazily to avoid a cycle).
def _country_auth_kill(location, description, profile, url):
    from matcher import _check_country_auth_kill

    return _check_country_auth_kill(location, description, profile, url)

SENIORITY_MARKER = "lovework:seniority"
CAP_COUNTRIES_MARKER = "lovework:cap-countries"

_SENIORITY_RE = re.compile(
    rf"{SENIORITY_MARKER}\s*:\s*(?P<value>[a-z_-]+)", re.IGNORECASE
)
_CAP_COUNTRIES_RE = re.compile(
    rf"{CAP_COUNTRIES_MARKER}\s*:\s*(?P<codes>[A-Za-z|,;\s]+)", re.IGNORECASE
)

#: A senior-titled role for an early-career principal: the ceiling is MAYBE, not
#: a kill. The advert is a real job and the principal may still apply, but the
#: reader must not see it ranked above roles they can actually win.
SENIORITY_CAP = float(os.getenv("LOVEWORK_SENIORITY_CAP", "6.0"))

#: Countries declared "do not rank high": the working language is not English and
#: the principal does not speak it, so the role is a long shot despite the
#: passport. Also a ceiling, never a kill — an English-language role in such a
#: country is still a real option, so this stays a demotion.
CAP_COUNTRY_CAP = float(os.getenv("LOVEWORK_CAP_COUNTRY_CAP", "5.5"))

#: Title markers of a role pitched above an early-career principal. Kept as a
#: fallback for entries whose source gives no seniority metadata (LinkedIn,
#: Hacker News, mail alerts): the source's own field is preferred when present
#: because it describes the advert, whereas a title can be inflated.
SENIOR_TITLE_RE = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|head\s+of|chief|director|"
    r"vp|vice\s+president|manager)\b",
    re.IGNORECASE,
)

#: Source-provided seniority values that mean "above the principal's level".
SENIOR_SENIORITY_VALUES = {
    "senior", "sr", "lead", "principal", "staff", "head", "director",
    "high", "expert", "manager", "vp",
}

#: Country names/codes for the declared cap list, so "BG", "Bulgaria" and
#: "Sofia, Bulgaria" all match.
_COUNTRY_NAMES = {
    "bg": ("bulgaria", "българия", "sofia", "plovdiv", "varna"),
    "ro": ("romania", "bucharest", "cluj", "timisoara"),
    "hu": ("hungary", "budapest"),
    "cz": ("czech", "prague", "brno"),
    "sk": ("slovakia", "bratislava"),
    "hr": ("croatia", "zagreb"),
    "si": ("slovenia", "ljubljana"),
    "pl": ("poland", "warsaw", "krakow", "wroclaw"),
    "lt": ("lithuania", "vilnius"),
    "lv": ("latvia", "riga"),
    "ee": ("estonia", "tallinn"),
    "gr": ("greece", "athens", "thessaloniki"),
    "de": ("germany", "berlin", "munich", "munchen", "hamburg", "frankfurt"),
    "fr": ("france", "paris", "lyon"),
    "es": ("spain", "madrid", "barcelona"),
    "it": ("italy", "milan", "rome"),
}

#: A location naming one of these is not exclusively in the capped country.
_ESCAPE_SIGNALS = (
    "remote", "worldwide", "anywhere", "uk", "united kingdom", "london",
    "england", "scotland", "wales", "ireland", "dublin", "netherlands",
    "amsterdam", "rotterdam", "cyprus", "nicosia", "malta",
)


def declared_seniority(profile_text: str) -> str:
    """The principal's declared career level ("" when the profile is silent)."""
    match = _SENIORITY_RE.search(profile_text or "")
    return match.group("value").strip().lower() if match else ""


def declared_cap_countries(profile_text: str) -> List[str]:
    """Country codes the profile declares "do not rank high"."""
    match = _CAP_COUNTRIES_RE.search(profile_text or "")
    if not match:
        return []
    return [code.strip().upper() for code in re.split(r"[|,;]", match.group("codes") or "")
            if code.strip()]


def _entry_score(entry) -> float:
    """The entry's scored value, preferring the combined axis.

    `WikiEntry` carries both `score` and `combined_score` (the report prints
    "Score" and "Combined" from them), so a demotion has to move both or the
    report contradicts itself.
    """
    for attr in ("combined_score", "score"):
        value = getattr(entry, attr, None)
        if value is not None:
            return float(value)
    return 0.0


def _capped(entry, cap: float, reason: str) -> bool:
    """Demote an entry to at most `cap`, annotated. True when it changed.

    Idempotent: an entry already at or below the ceiling is left alone, so
    re-running the pass over the same entries cannot stack annotations.
    """
    score = _entry_score(entry)
    if score <= cap and (getattr(entry, "decision", "") or "") != "GO":
        return False
    capped = min(score, cap)
    for attr in ("combined_score", "score"):
        if getattr(entry, attr, None) is not None:
            setattr(entry, attr, capped)
    entry.decision = "MAYBE"
    entry.recommended_action = "WATCH"
    entry.reasoning = f"{reason} {getattr(entry, 'reasoning', '')}".strip()
    return True


def apply_run_policy(entries: Iterable, profile_text: str) -> dict:
    """Apply the profile's declared run-level rules. Returns a count summary.

    Only ceilings, never promotions: a rule may lower a verdict's score and
    decision, never raise them, so the pass cannot manufacture a recommendation.
    """
    entries = list(entries)
    summary = {"seniority": 0, "cap_country": 0, "ineligible": 0}

    seniority = declared_seniority(profile_text)
    early = seniority in ("early-career", "early_career", "junior", "graduate")
    cap_countries = set(declared_cap_countries(profile_text))

    for entry in entries:
        # Eligibility re-check (2026-09-22): the matcher's kill needs the
        # location and URL, and a source can attach them after scoring — VJ's
        # sweep listed Xylem ("Pittsburgh, PA") and Cohere Health ("United
        # States") in DROP for their low score rather than hiding them, because
        # the kill saw neither field at match time. Here the entry has both, so
        # the same rule is applied once more and the advert is recorded as
        # ineligible instead of merely scored low.
        eligibility = _country_auth_kill(
            getattr(entry, "location", "") or "",
            getattr(entry, "advert_excerpt", "") or "",
            profile_text,
            getattr(entry, "url", "") or "",
        )
        if eligibility:
            entry.decision = "DROP"
            entry.recommended_action = "DROP"
            for attr in ("combined_score", "score"):
                if getattr(entry, attr, None) is not None:
                    setattr(entry, attr, 0.0)
            if not (getattr(entry, "reasoning", "") or "").startswith("AUTO-DROP:"):
                entry.reasoning = f"AUTO-DROP: {eligibility}. {entry.reasoning or ''}".strip()
            summary["ineligible"] += 1

        if early:
            source_level = (getattr(entry, "seniority", "") or "").strip().lower()
            title_says_senior = bool(SENIOR_TITLE_RE.search(f"{getattr(entry, 'title', '')}"))
            if any(value in source_level for value in SENIOR_SENIORITY_VALUES) or title_says_senior:
                if _capped(
                    entry,
                    SENIORITY_CAP,
                    f"SENIORITY ({source_level or 'senior-titled'}): early-career principal — "
                    f"a senior-level role is unlikely to hire them.",
                ):
                    summary["seniority"] += 1
        if cap_countries:
            location = (getattr(entry, "location", "") or "").lower()
            if location and not any(signal in location for signal in _ESCAPE_SIGNALS):
                for code in sorted(cap_countries):
                    names = _COUNTRY_NAMES.get(code.lower(), (code.lower(),))
                    if any(name in location for name in names):
                        if _capped(
                            entry,
                            CAP_COUNTRY_CAP,
                            f"LOCATION ({code}): working language there is not English — "
                            f"not ranked high.",
                        ):
                            summary["cap_country"] += 1
                        break

    if summary["seniority"] or summary["cap_country"]:
        logger.info(
            "[policy] run-level demotions: seniority=%d cap-country=%d ineligible=%d "
            "(caps %.1f / %.1f)",
            summary["seniority"], summary["cap_country"], summary["ineligible"],
            SENIORITY_CAP, CAP_COUNTRY_CAP,
        )
    return summary
