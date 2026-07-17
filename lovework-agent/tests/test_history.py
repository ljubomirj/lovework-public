"""
Tests for the applications/ directory scanner.

The history scanner looks at applications/YYYYMMDD-Company-Role/ dirs and
extracts date, company, role, and rejection markers.

`scan_applications(org)` returns List[PriorApplication].
`scan_history(org)` wraps it in PriorContact (used by the matcher).
"""

import pytest

from history import scan_applications, scan_history, _org_aliases


def test_scan_applications_finds_matching_dirs(isolated_config):
    """The scanner finds dirs whose name contains any alias of the org."""
    apps = isolated_config["applications"]
    (apps / "20251220-Poetiq_ai-AI_Scientist").mkdir()
    (apps / "20251220-Poetiq_ai-AI_Scientist" / "20251220-Poetiq_ai-AI_Scientist.txt").write_text(
        "Applied for the AI Scientist role."
    )

    apps_found = scan_applications("Poetiq", applications_dir=apps)
    assert len(apps_found) == 1
    app = apps_found[0]
    assert app.date == "2025-12-20"
    assert "AI" in app.role
    assert app.path.exists()


def test_scan_applications_no_match(isolated_config):
    """Returns empty list when no matching dirs exist."""
    apps = isolated_config["applications"]
    apps_found = scan_applications("NonexistentOrg", applications_dir=apps)
    assert apps_found == []


def test_scan_applications_detects_rejection_in_text(isolated_config):
    """Rejection markers in the .txt file are detected."""
    apps = isolated_config["applications"]
    d = apps / "20250115-Acme-Senior_Engineer"
    d.mkdir()
    (d / "20250115-Acme-Senior_Engineer.txt").write_text(
        "Applied for Senior Engineer.\n\n"
        "Rejection received 2025-02-10: 'We've decided not to move forward with your application.'"
    )

    apps_found = scan_applications("Acme", applications_dir=apps)
    assert len(apps_found) == 1
    assert apps_found[0].has_rejection is True
    assert apps_found[0].rejection_date == "2025-02-10"


def test_scan_applications_no_rejection_marker(isolated_config):
    """If no rejection marker in text, has_rejection is False."""
    apps = isolated_config["applications"]
    d = apps / "20250115-Acme-Engineer"
    d.mkdir()
    (d / "20250115-Acme-Engineer.txt").write_text("Applied. No response yet.")

    apps_found = scan_applications("Acme", applications_dir=apps)
    assert apps_found[0].has_rejection is False
    assert apps_found[0].rejection_date is None


def test_scan_history_summary(isolated_config):
    """The scan_history wrapper produces a human-readable summary."""
    apps = isolated_config["applications"]
    (apps / "20251220-Test_Co-AI_Scientist").mkdir()
    (apps / "20251220-Test_Co-AI_Scientist" / "20251220-Test_Co-AI_Scientist.txt").write_text(
        "Applied. Rejection received 2026-01-10."
    )

    prior = scan_history("Test_Co", use_gmail=False, applications_dir=apps)
    assert "Applied 2025-12-20" in prior.summary()
    assert "rejection received 2026-01-10" in prior.summary()


# ── Alias generation ────────────────────────────────────────────────────

def test_org_aliases_basic():
    """Aliases include the org name and a punctuation-stripped version."""
    aliases = _org_aliases("OpenAI")
    assert "openai" in aliases
    # Strips 'AI' suffix since it ends with ' AI' (case-insensitive check)
    assert "open" in aliases


def test_org_aliases_strips_ai_suffix():
    """Aliases strip 'AI' suffix."""
    aliases = _org_aliases("Anthropic AI")
    assert "anthropic" in aliases


def test_org_aliases_strips_punctuation():
    """Aliases strip dots and commas."""
    aliases = _org_aliases("FAR.AI")
    assert "farai" in aliases
    assert "far ai" in aliases


def test_org_aliases_strips_dots_for_comparison():
    """The dotted form and the un-dotted form both appear in aliases."""
    aliases = _org_aliases("DeepMind")
    assert "deepmind" in aliases
