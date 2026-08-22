"""Principal state, credential, and preview isolation regressions."""

import pytest

import config
import pipeline
from principal_runtime import _credential_home, resolve_principal_runtime


def test_vj_runtime_uses_visible_state_and_configured_mailbox():
    runtime = resolve_principal_runtime("vj")

    assert runtime.cache_dir == config.STATE_DIR / "vj" / "cache"
    assert runtime.wiki_root == config.STATE_DIR / "vj" / "wiki"
    assert runtime.applications_dir == config.STATE_DIR / "vj" / "applications"
    assert runtime.sources_dir == config.STATE_DIR / "vj" / "sources"
    assert runtime.gmail_mailbox is not None
    assert runtime.gmail_mailbox.label == "VJ-jobs"
    assert runtime.gmail_mailbox.source_name == "gmail_vj_jobs"
    assert runtime.gmail_mailbox.credential_home == (
        config.GMAIL_CREDENTIALS_DIR / config.GMAIL_CREDENTIAL_HOST / "petroula-vj"
    )


def test_vj_source_policy_does_not_inherit_lj_ai_lab_trackers():
    runtime = resolve_principal_runtime("vj")
    sources = pipeline.sources_for_profile("vj", runtime)

    assert runtime.gmail_mailbox.source_name in sources
    assert {"hn_hiring", "hn_jobs", "linkedin_related"} <= set(sources)
    assert not {"research_orgs", "neolabs", "hf_startups"} & set(sources)


def test_lj_runtime_stays_on_legacy_paths_but_uses_its_own_credential_copy():
    runtime = resolve_principal_runtime("lj")

    assert runtime.cache_dir == config.CACHE_DIR
    assert runtime.wiki_root == config.WIKI_ROOT
    assert runtime.dataset_dir == config.DATASET_DIR
    assert runtime.applications_dir == config.APPLICATIONS_DIR
    assert runtime.gmail_mailbox is not None
    assert runtime.gmail_mailbox.label == "LJ-jobs"
    assert runtime.gmail_mailbox.credential_home == (
        config.GMAIL_CREDENTIALS_DIR / config.GMAIL_CREDENTIAL_HOST / "ljubomir-lj"
    )


def test_credential_home_rejects_an_unsafe_host_override(monkeypatch):
    monkeypatch.setattr(config, "GMAIL_CREDENTIAL_HOST", "../other-host")

    with pytest.raises(ValueError, match="Invalid Gmail credential host"):
        _credential_home("ljubomir-lj")


class _NoMutationRegistry:
    def mark_run_complete(self, *args, **kwargs):
        raise AssertionError("preview must not update registry lifecycle")


class _NoopMatcher:
    def match(self, *args, **kwargs):
        raise AssertionError("test source returns no entries")


class _NoopLlm:
    model = "test-model"


class _NoopCrawler:
    pass


def test_vj_preview_uses_temporary_state_and_read_only_source_registry(monkeypatch):
    """The earlier dry-run lifecycle regression must not recur for VJ."""
    seen = []

    def fake_run_source(source, crawler, matcher, registry, runtime, dry_run):
        seen.append((source, registry, runtime, dry_run))
        return []

    monkeypatch.setattr(pipeline, "run_source", fake_run_source)
    principal_state = config.STATE_DIR / "vj"
    existed_before = principal_state.exists()

    entries, disappeared = pipeline.run_pipeline(
        "vj",
        source="gmail_vj_jobs",
        dry_run=True,
        snapshot=True,
        registry=_NoMutationRegistry(),
        llm=_NoopLlm(),
        crawler=_NoopCrawler(),
        matcher=_NoopMatcher(),
    )

    assert entries == []
    assert disappeared == 0
    assert len(seen) == 1
    source, registry, runtime, dry_run = seen[0]
    assert source == "gmail_vj_jobs"
    assert registry is None
    assert dry_run is True
    assert "lovework-preview-" in str(runtime.cache_dir)
    assert not runtime.cache_dir.exists()
    assert principal_state.exists() is existed_before
