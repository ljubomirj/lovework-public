"""
Source: speech, voice-AI and speech-agent companies (repo-owned list).

Complements the neolab and neocloud trackers: where `neolabs` follows frontier
AI research labs and `neoclouds` follows the compute layer, this follows the
speech stack — TTS/STT/speech-to-speech model providers, voice-agent platforms
and orchestration/realtime infrastructure, open-source speech labs, and the
speech arenas/leaderboards that rank them.

The list lives in the *repo-owned* speech tracker file (a symlink to the
parent repo's `speech-and-voice-ai-companies-tracker.txt`), deliberately not in
the downloaded neolab tracker (which a refresh overwrites).

Parser, run loop and entry shape are inherited from `NeolabsSource` — one
tracker format for every list. Entries with a `➟ Careers:` line crawl that page
directly; entries without one start from the company homepage and let the
crawler navigate to openings.
"""

import config

from .neolabs import NeolabsSource


class SpeechCompaniesSource(NeolabsSource):
    #: This source's tier, declared here and nowhere else
    #: (docs/28 §WP3b box 1). Primary = the hiring party's own
    #: publication; secondary = someone relaying it.
    primary = False
    #: Set when the run enumerated its *whole* scope (no truncation,
    #: caps or failures). Only then may absence become
    #: `not_listed_in_scope` rather than `disappeared`.
    complete_scope = False
    """Crawls the curated speech/voice-AI company tracker for job openings."""

    name = "speech_companies"
    tracker_path = config.SPEECH_TRACKER
    goal = (
        "Find open positions at this speech / voice-AI / audio-ML company. "
        "Look for: ML/research engineer, speech or audio ML engineer, inference/"
        "systems engineer, research scientist (ASR/TTS/audio), founding engineer, "
        "or similar technical roles."
    )
