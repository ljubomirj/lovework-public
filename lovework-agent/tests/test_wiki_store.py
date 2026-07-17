"""
Tests for the wiki store (markdown output).

The wiki is the user's view into the agent's findings. These tests verify
- Reports save correctly
- Org pages accumulate history
- The index is built properly
- Safe filename handling
"""

import pytest

from wiki_store import WikiEntry, WikiStore


@pytest.fixture
def wiki(tmp_path):
    """Fresh WikiStore in a temp dir."""
    return WikiStore(root=tmp_path)


def test_save_report_creates_file(wiki, tmp_path):
    """save_report creates a markdown file."""
    entries = [
        WikiEntry(
            org_name="Acme",
            title="ML Engineer",
            url="https://acme.com/jobs/1",
            location="London, UK",
            score=8.5,
            decision="GO",
            reasoning="Strong fit",
            source="test",
        )
    ]
    path = wiki.save_report(entries, profile_name="LJ")
    assert path.exists()
    content = path.read_text()
    assert "Acme" in content
    assert "ML Engineer" in content
    assert "GO" in content


def test_save_report_groups_by_decision(wiki, tmp_path):
    """Report has separate sections for GO, MAYBE, FLAG, DROP."""
    entries = [
        WikiEntry(org_name="A", title="X", url="", location="", score=9, decision="GO", reasoning="", source="t"),
        WikiEntry(org_name="B", title="Y", url="", location="", score=5, decision="MAYBE", reasoning="", source="t"),
        WikiEntry(org_name="C", title="Z", url="", location="", score=2, decision="DROP", reasoning="", source="t"),
    ]
    path = wiki.save_report(entries, profile_name="LJ")
    content = path.read_text()
    assert "## GO" in content
    assert "## MAYBE" in content
    assert "## DROP" in content


def test_save_report_includes_lifecycle_section(wiki, tmp_path):
    """Lifecycle sections (New, Long-Lasting) appear when present."""
    entries = [
        WikiEntry(
            org_name="A", title="X", url="", location="", score=9,
            decision="GO", reasoning="", source="t",
            lifecycle_status="new", first_seen="2026-06-19",
        ),
        WikiEntry(
            org_name="B", title="Y", url="", location="", score=5,
            decision="MAYBE", reasoning="", source="t",
            lifecycle_status="long_lasting", first_seen="2026-01-15",
        ),
    ]
    path = wiki.save_report(entries, profile_name="LJ")
    content = path.read_text()
    assert "New Listings" in content
    assert "Long-Lasting" in content
    assert "open" in content  # shows the age


def test_save_report_includes_multi_axis_fields(wiki, tmp_path):
    """Reports preserve fit/reach/flourish, combined score, and action."""
    entries = [
        WikiEntry(
            org_name="Isomorphic Labs",
            title="Research Scientist",
            url="https://example.com/job",
            location="London",
            score=4.0,
            decision="FLAG",
            reasoning="High fit but low reach",
            source="test",
            fit_score=8.0,
            reach_score=2.0,
            flourish_score=3.0,
            combined_score=4.0,
            recommended_action="USE_AS_GAP_SIGNAL",
        )
    ]
    path = wiki.save_report(entries, profile_name="LJ")
    content = path.read_text()
    assert "- **Fit**: 8.0/10" in content
    assert "- **Reach**: 2.0/10" in content
    assert "- **Flourish**: 3.0/10" in content
    assert "- **Combined**: 4.0/10" in content
    assert "- **Action**: USE_AS_GAP_SIGNAL" in content


def test_report_and_org_page_preserve_discovery_provenance(wiki):
    entry = WikiEntry(
        org_name="Talk Machine",
        title="Founding Engineers",
        url="https://talkmachine.com/jobs/engineer",
        location="Remote",
        score=9.3,
        decision="GO",
        reasoning="Rare direct voice match",
        source="hn_hiring",
        discovery_url="https://news.ycombinator.com/item?id=48749307",
        discovery_date="2026-07-01",
    )

    report = wiki.save_report([entry], profile_name="LJ").read_text()
    assert "[hn_hiring](https://news.ycombinator.com/item?id=48749307) (2026-07-01)" in report

    wiki.update_org_page(entry)
    parsed = wiki._entries_from_org_pages()
    assert parsed[0].discovery_url.endswith("item?id=48749307")
    assert parsed[0].discovery_date == "2026-07-01"


def test_update_org_page_appends(wiki, tmp_path):
    """Multiple updates to the same org accumulate."""
    entry1 = WikiEntry(org_name="Acme", title="ML Engineer", url="", location="", score=8, decision="GO", reasoning="first", source="t")
    entry2 = WikiEntry(org_name="Acme", title="AI Scientist", url="", location="", score=9, decision="GO", reasoning="second", source="t")

    wiki.update_org_page(entry1)
    wiki.update_org_page(entry2)

    org_path = wiki.orgs_dir / "Acme.md"
    assert org_path.exists()
    content = org_path.read_text()
    assert "ML Engineer" in content
    assert "AI Scientist" in content
    assert "first" in content
    assert "second" in content


def test_org_page_round_trips_multi_axis_fields(wiki, tmp_path):
    """Index rebuild can parse multi-axis metadata from org pages."""
    entry = WikiEntry(
        org_name="Acme",
        title="Applied AI Engineer",
        url="https://example.com/acme",
        location="London",
        score=7.6,
        decision="GO",
        reasoning="Reachable applied systems role",
        source="test",
        fit_score=8.0,
        reach_score=7.0,
        flourish_score=8.0,
        combined_score=7.6,
        recommended_action="APPLY_NOW",
    )
    wiki.update_org_page(entry)
    parsed = wiki._entries_from_org_pages()
    assert len(parsed) == 1
    assert parsed[0].fit_score == 8.0
    assert parsed[0].reach_score == 7.0
    assert parsed[0].flourish_score == 8.0
    assert parsed[0].combined_score == 7.6
    assert parsed[0].recommended_action == "APPLY_NOW"


def test_rebuild_index_includes_go_and_maybe(wiki, tmp_path):
    """The index lists GO and MAYBE findings."""
    entries = [
        WikiEntry(org_name="A", title="X", url="", location="", score=9, decision="GO", reasoning="", source="t"),
        WikiEntry(org_name="B", title="Y", url="", location="", score=5, decision="MAYBE", reasoning="", source="t"),
    ]
    wiki.rebuild_index(entries)

    index_path = wiki.root / "index.md"
    assert index_path.exists()
    content = index_path.read_text()
    assert "GO Listings" in content
    assert "MAYBE Listings" in content
    assert "A — X" in content or "A" in content


def test_safe_filename_handles_special_chars():
    """Special characters in org names are sanitized; period is kept."""
    from wiki_store import WikiStore
    # Period is preserved (FAR.AI is a real org name).
    assert WikiStore._safe_filename("FAR.AI") == "FAR.AI"
    assert WikiStore._safe_filename("Open AI") == "Open_AI"
    assert WikiStore._safe_filename("Hugging Face") == "Hugging_Face"
    assert WikiStore._safe_filename("Foo/Bar") == "Foo_Bar"
    # Non-ASCII characters are folded to underscore (the original bug).
    assert WikiStore._safe_filename("Zūm") == "Z_m"
    assert WikiStore._safe_filename("Pâté Systems") == "P_t_Systems"
    # Empty / unsafe-only → default "x".
    assert WikiStore._safe_filename("") == "x"
    assert WikiStore._safe_filename("///") == "x"
    # No runs of underscores.
    assert WikiStore._safe_filename("a   b") == "a_b"
    assert WikiStore._safe_filename("a  /  b") == "a_b"


def test_wiki_entry_default_date(wiki):
    """A WikiEntry without an explicit date gets today's date."""
    from datetime import datetime
    e = WikiEntry(
        org_name="X", title="T", url="", location="",
        score=5, decision="MAYBE", reasoning="", source="t",
    )
    assert e.date == datetime.now().strftime("%Y-%m-%d")
