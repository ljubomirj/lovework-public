"""Regression tests for cached-evidence principal reassessment."""

import json

from principal_runtime import PrincipalRuntime
from enrichment import LeadEnricher
from job_registry import JobRegistry
from matcher import MatchResult
from reassess import reassess_records, write_reassessment_report


class _FakeLlm:
    model = "test-model"

    def __init__(self):
        self.calls = 0

    def structured(self, messages, schema, context=""):
        self.calls += 1
        return MatchResult(
            fit_score=8.0,
            reach_score=7.0,
            flourish_score=8.0,
            reasoning="Strong statistical pricing match.",
        )


def _runtime(tmp_path):
    root = tmp_path / "state" / "vj"
    return PrincipalRuntime(
        profile_name="vj",
        cache_dir=root / "cache",
        wiki_root=root / "wiki",
        dataset_dir=root / "dataset",
        applications_dir=root / "applications",
        sources_dir=root / "sources",
    )


def _cache_primary_advert(runtime, url, text):
    enricher = LeadEnricher(cache_dir=runtime.cache_dir / "enrichment")
    cache_path = enricher._cache_path(url)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "original_description": "",
                "primary_url": url,
                "primary_text": text,
                "primary_content_hash": "evidence-hash",
                "primary_fetched_at": "2026-07-22T09:00:00+00:00",
                "primary_fetch_method": "firecrawl",
            }
        ),
        encoding="utf-8",
    )


def test_reassessment_uses_cached_advert_without_network_or_registry_mutation(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    registry = JobRegistry(runtime.cache_dir / "jobs.csv")
    record = registry.upsert(
        "Actuary Co",
        "Pricing Analyst",
        "https://example.test/pricing",
        source="gmail_vj_jobs",
    )
    before_registry = registry.csv_path.read_bytes()
    _cache_primary_advert(
        runtime,
        record.url,
        "Pricing Analyst role using statistics, insurance pricing models, and Python.",
    )

    def fail_fetch(self, url):
        raise AssertionError("cached reassessment must never fetch the web")

    monkeypatch.setattr(LeadEnricher, "_fetch", fail_fetch)
    llm = _FakeLlm()
    entries, coverage, _, namespace = reassess_records(
        "vj", "data-statistics-pricing", runtime=runtime, registry=registry, llm=llm
    )

    assert len(entries) == 1
    assert llm.calls == 1
    assert entries[0].decision in {"GO", "MAYBE", "FLAG"}
    assert entries[0].primary_content_hash == "evidence-hash"
    assert coverage == {"total": 1, "cached": 1, "guarded": 0, "unscored": 0}
    assert "vj:data-statistics-pricing" in namespace
    assert registry.csv_path.read_bytes() == before_registry


def test_reassessment_marks_missing_primary_evidence_unscored_without_llm(tmp_path):
    runtime = _runtime(tmp_path)
    registry = JobRegistry(runtime.cache_dir / "jobs.csv")
    registry.upsert("Statistics Co", "Statistician", "https://example.test/no-cache")
    llm = _FakeLlm()

    entries, coverage, _, _ = reassess_records(
        "vj", "data-statistics-pricing", runtime=runtime, registry=registry, llm=llm
    )

    assert llm.calls == 0
    assert entries[0].assessment_status == "UNSCORED"
    assert entries[0].decision == "FLAG"
    assert coverage == {"total": 1, "cached": 0, "guarded": 0, "unscored": 1}


def test_reassessment_report_is_distinct_and_explains_read_only_boundary(tmp_path):
    runtime = _runtime(tmp_path)
    registry = JobRegistry(runtime.cache_dir / "jobs.csv")
    record = registry.upsert("Actuary Co", "Pricing Analyst", "https://example.test/pricing")
    _cache_primary_advert(runtime, record.url, "Statistics and insurance pricing.")
    entries, coverage, _, namespace = reassess_records(
        "vj", "data-statistics-pricing", runtime=runtime, registry=registry, llm=_FakeLlm()
    )

    report = write_reassessment_report(
        entries,
        coverage,
        profile_name="vj",
        role="data-statistics-pricing",
        namespace=namespace,
        reports_dir=runtime.wiki_root / "reports",
    )

    assert report.name.endswith("-vj-data-statistics-pricing-reassessment.md")
    content = report.read_text(encoding="utf-8")
    assert "not a crawl" in content
    assert "did not fetch the web, access Gmail, change registry lifecycle" in content
    assert "Pricing Analyst" in content
