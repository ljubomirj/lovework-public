"""
Source: cheminformatics & computational-chemistry companies (repo-owned list).

A topic-scoped tracker in the neolab / neocloud / speech family: where
`neolabs` follows frontier AI labs and `neoclouds` the compute layer, this
follows KJ's own profession — cheminformatics, computational chemistry,
molecular modelling, and the AI-for-drug-discovery companies that hire for it.

The list lives in the repo-owned `cheminfo-companies-tracker.txt` (symlinked
from the parent repo, like the other three trackers). Parser, run loop and
entry shape are inherited from `NeolabsSource` — one tracker format for every
list.

**One owner per org.** This list overlaps the principal's *market* tracker by
design (that is the sector view; this is the profession view), so
`_parse_tracker()` drops any company the market tracker already covers: the
same careers page is never crawled twice in one run. Remove the company from
`profiles/<p>/market_tracker.txt` (or set
`LOVEWORK_CHEMINFO_SKIP_MARKET_TRACKER=0`) to let this list own it.
"""

import logging
import os
import re
from typing import List

import config

from .neolabs import NeolabsSource

logger = logging.getLogger(__name__)

SKIP_IF_IN_MARKET_TRACKER = os.getenv("LOVEWORK_CHEMINFO_SKIP_MARKET_TRACKER", "1") != "0"


def _tokens(name: str) -> frozenset:
    """Name → comparable token set (`Isomorphic Labs` == `isomorphic labs`)."""
    return frozenset(t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(t) > 1)


class CheminfoCompaniesSource(NeolabsSource):
    """Crawls the curated cheminformatics company tracker for job openings."""

    primary = False
    complete_scope = False
    name = "cheminfo_companies"
    tracker_path = config.CHEMINFO_TRACKER
    goal = (
        "Find open positions at this cheminformatics / computational-chemistry / "
        "AI-for-drug-discovery company. Look for: cheminformatics or computational "
        "chemistry scientist or engineer, CADD, molecular modelling, ML for molecular "
        "design, scientific software engineer, data scientist (chemistry), or similar."
    )

    def __init__(self, crawler, matcher, registry=None, profile_name: str = ""):
        super().__init__(crawler, matcher, registry)
        #: Whose market tracker to dedupe against (pipeline passes the runtime's).
        self.profile_name = (profile_name or "lj").lower()
        #: Resolved per instance so LOVEWORK_CHEMINFO_TRACKER (and tests) can move it.
        self.tracker_path = config.CHEMINFO_TRACKER

    def _market_tracker_orgs(self) -> List[frozenset]:
        """Token sets of the orgs the principal's own market tracker already lists."""
        if not SKIP_IF_IN_MARKET_TRACKER:
            return []
        try:
            from .market_players import tracker_path_for

            path = tracker_path_for(self.profile_name)
            if path is None:
                return []
            class _Probe(NeolabsSource):
                name = "market_tracker_probe"
                tracker_path = path
                goal = ""
            return [_tokens(o["name"]) for o in _Probe(crawler=None, matcher=None)._parse_tracker()]
        except Exception as exc:                      # noqa: BLE001 - dedupe is best-effort
            logger.warning("[cheminfo_companies] market-tracker dedupe unavailable: %s", exc)
            return []

    def _parse_tracker(self) -> List[dict]:
        orgs = super()._parse_tracker()
        covered = self._market_tracker_orgs()
        if not covered:
            return orgs
        kept = [o for o in orgs if _tokens(o["name"]) not in covered]
        skipped = len(orgs) - len(kept)
        if skipped:
            logger.info(
                "[cheminfo_companies] %d/%d orgs skipped — already covered by %s's market tracker",
                skipped, len(orgs), getattr(self, "profile_name", "lj") or "lj",
            )
        return kept
