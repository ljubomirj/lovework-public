"""Golden regression for the July 2026 Talk Machine lead."""

import config
from candidate_evidence import CandidateEvidenceIndex, EvidenceGroundedMatcher
from enrichment import EnrichedLead, EnrichingMatcher
from matcher import MatchResult


TALK_MACHINE_COPY = """
Hello. We’re looking for founding engineers to join our small technical team
building Talk Machine, a voice-first consumer communication product powered by
AI. Generalists work across product features, backend APIs, audio and
transcription systems, AI agent workflows, tools, services, and infrastructure.
We prefer experience building strong products in small teams and early product
development close to users.
"""


def test_talk_machine_retrieves_lj_speech_product_and_small_team_evidence():
    profile = config.load_profile_text("lj", role="general")
    bio = config.load_bio("lj")
    index = CandidateEvidenceIndex(profile, bio)
    evidence = index.format_for_matcher(TALK_MACHINE_COPY, limit=10)

    assert "speech recognition" in evidence.lower()
    assert "playlist" in evidence.lower() or "spoken document" in evidence.lower()
    assert "[CONCRETE ARTIFACT]" in evidence
    assert "production" in evidence.lower() or "product" in evidence.lower()


def test_talk_machine_full_evidence_contract_supports_high_score():
    class FixedEnricher:
        def enrich(self, description, url):
            return EnrichedLead(
                original_description=description,
                primary_url=url,
                primary_text=TALK_MACHINE_COPY,
                primary_content_hash="talk-machine-golden",
                primary_fetched_at="2026-07-16T12:00:00+00:00",
                primary_fetch_method="fixture",
            )

    class ContractMatcher:
        def match(self, title, description, org, job_url="", location=""):
            lowered = description.lower()
            assert "voice-first" in lowered
            assert "speech recognition" in lowered
            assert "playlist" in lowered or "spoken document" in lowered
            return MatchResult(
                fit_score=9.8,
                reach_score=8.5,
                flourish_score=9.3,
                combined_score=9.2,
                score=9.2,
                recommended_action="APPLY_NOW",
                decision="GO",
                alignment_matrix=[
                    "voice/audio systems -> 10 years of speech and embedded ASR",
                    "consumer voice product -> playlist voice-selection demo",
                    "small product team -> production systems and startup work",
                ],
                application_angle="Lead with the embedded playlist voice demo and production systems work.",
                reasoning="Rare direct domain and working-style match.",
            )

    profile = config.load_profile_text("lj", role="general")
    matcher = EvidenceGroundedMatcher(
        ContractMatcher(), CandidateEvidenceIndex(profile, config.load_bio("lj"))
    )
    matcher = EnrichingMatcher(matcher, FixedEnricher())
    result = matcher.match(
        "Founding Engineers",
        "HN lead",
        "Talk Machine",
        job_url="https://talkmachine.com/jobs/engineer",
        location="Remote (London, Europe, Dubai)",
    )
    assert result.score >= 9
    assert result.recommended_action == "APPLY_NOW"
