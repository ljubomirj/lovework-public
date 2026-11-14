"""
Source: the principal's own "market players" list (repo-owned tracker).

The tracker family — `neolabs`, `neoclouds`, `speech_companies` — follows one
sector each, and every one of those lists is LJ's. This source follows the list
the *principal* owns: `profiles/<principal>/market_tracker.txt`, written in the
same tracker format as the others. Parser, run loop and entry shape are inherited
from `NeolabsSource`; entries with a `➟ Careers:` line crawl that page directly,
entries without one start from the homepage and let the crawler navigate to
openings.

Why this exists: `pipeline.sources_for_profile` deliberately refuses to run
LJ-maintained lists for anyone else — *"Company pages and Harnham still write
LJ-maintained source policy. Do not quietly run either for another person until
they accept a profile parameter."* This source is that parameter. It lets KJ and
VJ watch their own markets, with their own sector choices, rather than inheriting
someone else's idea of what matters.

Two gates, both explicit, so a missing list can never look like a silent crawl:

* the principal must have a tracker file (`tracker_path_for`), and
* the principal must have a declared `goal` (`goal_for`) — the scoring text the
  crawler passes to the matcher, which is sector-specific by nature.

`sources_for_profile` offers this source only when both hold.
"""

from typing import Optional
from pathlib import Path

import config

from .neolabs import NeolabsSource

#: Filename, per principal, under `profiles/<principal>/`.
MARKET_TRACKER_NAME = "market_tracker.txt"

#: What to look for, per principal. This is the `goal` the crawler hands the
#: matcher, so it must name *their* target roles — a generic one would score
#: every list against the wrong sector. Derived from each principal's profile
#: and role criteria; see `profiles/<p>/market_tracker.txt` for the lists these
#: goals accompany.
GOALS = {
    "lj": (
        "Find open positions at this AI-frontier company. Look for: research "
        "engineer / research scientist, ML or post-training engineer (RL, "
        "fine-tuning, evals, data), inference or systems engineer, performance or "
        "kernel engineer, applied/research-adjacent founding engineer, or similar "
        "technical roles at a lab, compute or agentic-systems company."
    ),
    "kj": (
        "Find open positions at this chemistry-adjacent organisation. Look for: "
        "research scientist / R&D chemist (computational, analytical, formulation, "
        "catalysis, materials, battery or agritech chemistry), process or "
        "development chemist, applications/lab scientist, or similar roles a "
        "chemistry MSc with an in-progress PhD could hold."
    ),
    "vj": (
        "Find open positions at this insurance, risk and analytics organisation. "
        "Look for: actuarial analyst/student, pricing or credit-risk analyst, data "
        "scientist / statistician, quantitative analyst, graduate scheme or "
        "junior-level analytics role, or similar."
    ),
}


def tracker_path_for(profile_name: str) -> Optional[Path]:
    """The principal's market tracker, or None when they have not written one."""
    path = config.PROFILES_DIR / (profile_name or "").lower() / MARKET_TRACKER_NAME
    return path if path.is_file() else None


def goal_for(profile_name: str) -> Optional[str]:
    """The principal's market goal, or None when none has been declared."""
    return GOALS.get((profile_name or "").lower())


def available_for(profile_name: str) -> bool:
    """True when this source has both a list and a goal for the principal."""
    return tracker_path_for(profile_name) is not None and goal_for(profile_name) is not None


class MarketPlayersSource(NeolabsSource):
    """Crawls one principal's own market-players tracker for job openings.

    The list is curated by us, not published by the hiring party, so this is
    secondary-tier evidence (`primary = False`), matching the rest of the
    tracker family. `complete_scope` stays False: a weekly crawl of a hand-kept
    list never enumerates a whole market, so absence must not become
    `not_listed_in_scope`.
    """

    primary = False
    complete_scope = False

    name = "market_players"

    def __init__(self, crawler, matcher, registry=None, profile_name: str = ""):
        super().__init__(crawler, matcher, registry)
        self.profile_name = (profile_name or "").lower()
        # Instance attribute shadows the class attribute the base class reads in
        # `_parse_tracker` / `run`, so the shared parser needs no change.
        path = tracker_path_for(self.profile_name)
        self.tracker_path = path if path is not None else config.PROFILES_DIR / self.profile_name / MARKET_TRACKER_NAME
        self.goal = goal_for(self.profile_name) or ""
