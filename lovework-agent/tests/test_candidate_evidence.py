from candidate_evidence import CandidateEvidenceIndex, EvidenceGroundedMatcher
from matcher import MatchResult


def test_retrieval_bridges_voice_job_to_speech_experience():
    profile = "Built production distributed systems for financial trading."
    bio = "Spent ten years in speech recognition research and built voice retrieval demos on an iPAQ."
    index = CandidateEvidenceIndex(profile, bio)

    facts = index.retrieve(
        "Voice-first product using audio, transcription systems and AI agents",
        limit=3,
    )
    assert any("ten years in speech recognition" in fact.text for fact in facts)


def test_grounded_matcher_injects_only_retrieved_candidate_evidence():
    class FakeMatcher:
        def __init__(self):
            self.description = ""

        def match(self, title, description, org, job_url="", location=""):
            self.description = description
            return MatchResult(score=9, decision="GO", reasoning="grounded")

    inner = FakeMatcher()
    wrapper = EvidenceGroundedMatcher(
        inner,
        CandidateEvidenceIndex("Built voice retrieval and speech recognition products."),
    )
    wrapper.match("Founding Engineer", "Voice-first AI product", "Talk Machine")
    assert "RETRIEVED CANDIDATE EVIDENCE" in inner.description
    assert "speech recognition" in inner.description
