"""
Smoke test — fast end-to-end check that the agent is functional.

Run from CI or after a fresh install:
    ../venv/bin/python3 lovework-agent/tests/smoke_test.py

Verifies:
- All modules import
- Profile loads
- Job registry creates and queries
- Sandbox runs Python
- Wiki writes work
- Filter logic is correct
"""

import sys
from pathlib import Path

# Add agent dir to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_imports():
    """All public modules import without error."""
    import config
    import crawler
    import matcher
    import job_registry
    import history
    import wiki_store
    import llm_client
    import llm_runtime
    import tools
    import sandbox
    # dspy_signatures and agent are optional (require pi-agent/dspy)
    try:
        import dspy_signatures
    except ImportError:
        pass
    try:
        import agent
    except ImportError:
        pass
    print("✓ All modules import")


def test_profile_loads():
    """At least one profile loads successfully."""
    import config
    text = config.load_profile_text("lj", role="general")
    assert len(text) > 1000
    print(f"✓ LJ general profile loads ({len(text)} chars)")


def test_registry_lifecycle():
    """Registry upsert + lifecycle works."""
    import tempfile
    from job_registry import JobRegistry, STATUS_NEW, STATUS_STILL_OPEN

    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "jobs.csv"
        r = JobRegistry(csv_path=csv_path)
        rec1 = r.upsert("Test Co", "ML Engineer", "https://test.com/1")
        assert rec1.status == STATUS_NEW
        rec2 = r.upsert("Test Co", "ML Engineer", "https://test.com/1")
        assert rec2.status == STATUS_STILL_OPEN
        stats = r.stats()
        assert stats.get(STATUS_STILL_OPEN) == 1
    print("✓ Registry lifecycle works")


def test_sandbox_runs():
    """Sandbox executes Python code."""
    import asyncio
    from sandbox import run_python_tool_factory

    tool = run_python_tool_factory("lj", "general")
    result = asyncio.run(tool.execute("test", {"code": 'print("hello from sandbox")'}))
    text = result.content[0].text
    assert "hello from sandbox" in text
    print("✓ Sandbox runs Python")


def test_filter_logic():
    """Location and recency filters work."""
    from crawler import is_location_acceptable, is_recent
    assert is_location_acceptable("London, UK") is True
    assert is_location_acceptable("San Francisco, CA") is False
    assert is_recent("2 weeks ago") is True
    assert is_recent("5 weeks ago") is False
    print("✓ Filter logic works")


def test_wiki_writes():
    """Wiki writes files correctly."""
    import tempfile
    from wiki_store import WikiStore, WikiEntry

    with tempfile.TemporaryDirectory() as tmp:
        wiki = WikiStore(root=Path(tmp) / "wiki")
        entry = WikiEntry(
            org_name="Test", title="ML Engineer", url="", location="",
            score=8, decision="GO", reasoning="ok", source="test",
        )
        wiki.update_org_page(entry)
        assert (wiki.orgs_dir / "Test.md").exists()
    print("✓ Wiki writes work")


if __name__ == "__main__":
    print("Running LoveWork smoke test...\n")
    test_imports()
    test_profile_loads()
    test_registry_lifecycle()
    test_sandbox_runs()
    test_filter_logic()
    test_wiki_writes()
    print("\nAll smoke tests passed ✓")
