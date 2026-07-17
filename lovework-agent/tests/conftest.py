"""
Pytest configuration and shared fixtures.

We isolate tests from the user's real data by:
- Pointing LOVEWORK_HOME at a temp dir
- Pointing LOVEWORK_WIKI, LOVEWORK_CACHE, and LOVEWORK_DATASET at temp dirs
- Skipping LLM tests if DEEPSEEK_API_KEY is not set
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Each test gets a fresh temp config dir."""
    home = tmp_path / "lovework-home"
    wiki = tmp_path / "wiki"
    cache = tmp_path / "cache"
    dataset = tmp_path / "dataset"
    home.mkdir()
    wiki.mkdir()
    cache.mkdir()
    dataset.mkdir()

    monkeypatch.setenv("LOVEWORK_HOME", str(home))
    monkeypatch.setenv("LOVEWORK_WIKI", str(wiki))
    monkeypatch.setenv("LOVEWORK_CACHE", str(cache))
    monkeypatch.setenv("LOVEWORK_DATASET", str(dataset))
    try:
        import config
        monkeypatch.setattr(config, "DATASET_DIR", dataset)
    except Exception:
        pass
    # Use a fake applications dir to avoid touching real data
    apps = tmp_path / "applications"
    apps.mkdir()
    monkeypatch.setenv("LOVEWORK_APPLICATIONS_DIR", str(apps))

    yield {
        "home": home,
        "wiki": wiki,
        "cache": cache,
        "dataset": dataset,
        "applications": apps,
    }


@pytest.fixture
def llm_key():
    """Return the DEEPSEEK_API_KEY if set, else skip the test."""
    key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not key or key == "test":
        pytest.skip("DEEPSEEK_API_KEY not set — skipping LLM test")
    return key


def pytest_collection_modifyitems(config, items):
    """Mark LLM tests so they can be skipped with -m 'not llm'."""
    for item in items:
        if "llm" in item.keywords:
            continue
