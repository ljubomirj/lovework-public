from assessment_cache import AssessmentCachingMatcher, assessment_cache_namespace
from matcher import MatchResult


class CountingMatcher:
    def __init__(self, status="SCORED"):
        self.calls = 0
        self.status = status

    def match(self, title, description, org, job_url="", location=""):
        self.calls += 1
        return MatchResult(
            fit_score=9,
            reach_score=8,
            flourish_score=9,
            score=8.6,
            combined_score=8.6,
            decision="GO" if self.status == "SCORED" else "FLAG",
            reasoning="direct voice match",
            assessment_status=self.status,
        )


def test_successful_assessment_is_reused(tmp_path):
    inner = CountingMatcher()
    cached = AssessmentCachingMatcher(inner, "test-v1", tmp_path)
    args = ("Founding Engineer", "voice AI", "Talk Machine")
    first = cached.match(*args, job_url="https://talkmachine.com/jobs/engineer")
    second = cached.match(*args, job_url="https://talkmachine.com/jobs/engineer")
    assert inner.calls == 1
    assert second.model_dump() == first.model_dump()


def test_unscored_failure_is_not_cached(tmp_path):
    inner = CountingMatcher(status="UNSCORED")
    cached = AssessmentCachingMatcher(inner, "test-v1", tmp_path)
    cached.match("Engineer", "voice AI", "Talk Machine")
    cached.match("Engineer", "voice AI", "Talk Machine")
    assert inner.calls == 2
    assert list(tmp_path.glob("*.json")) == []


def test_assessment_namespace_changes_when_effective_profile_changes():
    first = assessment_cache_namespace("vj", "data-statistics-pricing", "test", "profile v1")
    second = assessment_cache_namespace("vj", "data-statistics-pricing", "test", "profile v2")

    assert first != second
    assert "vj:data-statistics-pricing" in first
