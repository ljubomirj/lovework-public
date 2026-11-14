"""
Source: Neoclouds and compute providers (LJ's curated list, from the @jessiedong_
"all paths lead to becoming a neocloud" taxonomy).

Complements the neolab tracker: where `neolabs` follows frontier AI research labs,
this follows the compute layer (own-GPU clouds, power/data-centre operators, compute
aggregators, research+compute labs, own-silicon inference vendors, multi-chip inference).
The list lives in the *repo-owned* neocloud tracker file, deliberately not in the
downloaded neolab tracker (which a refresh overwrites).

Parser, run loop and entry shape are inherited from `NeolabsSource` — one tracker
format for both lists.
"""

import config

from .neolabs import NeolabsSource


class NeocloudsSource(NeolabsSource):
    #: This source's tier, declared here and nowhere else
    #: (docs/28 §WP3b box 1). Primary = the hiring party's own
    #: publication; secondary = someone relaying it.
    primary = False
    #: Set when the run enumerated its *whole* scope (no truncation,
    #: caps or failures). Only then may absence become
    #: `not_listed_in_scope` rather than `disappeared`.
    complete_scope = False
    """Crawls the curated neocloud/compute-provider tracker for job openings."""

    name = "neoclouds"
    tracker_path = config.NEOCLOUD_TRACKER
    goal = (
        "Find open positions at this neocloud / GPU-cloud / AI-compute company. "
        "Look for: infrastructure engineer, systems engineer, distributed-systems "
        "engineer, SRE/platform engineer, ML/inference engineer, research engineer, "
        "founding engineer, or similar technical roles."
    )
