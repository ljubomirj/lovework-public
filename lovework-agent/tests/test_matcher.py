"""
Tests for the matcher (no LLM calls — just the structural parts).

The actual scoring needs an LLM. These tests verify the structure:
- MatchResult is well-formed
- The re-apply kill logic works
- The history-aware matching is correct
"""

from datetime import datetime, timedelta

import pytest

from matcher import (
    JobMatcher,
    MatchResult,
    REAPPLY_COOLDOWN_MONTHS,
    _action_from_scores,
    _compute_combined,
    _decision_from_action,
)


@pytest.fixture
def profile_text():
    return "Test profile. ML engineer based in UK."


def test_match_result_validates_score_range():
    """MatchResult enforces score 0-10."""
    # Pydantic should accept 0 and 10
    MatchResult(score=0.0, decision="DROP", reasoning="")
    MatchResult(score=10.0, decision="GO", reasoning="")


def test_match_result_legacy_constructor_defaults_axes():
    """Old call sites can still construct MatchResult with score/decision only."""
    result = MatchResult(score=5.0, decision="MAYBE", reasoning="legacy")
    assert result.fit_score == 0.0
    assert result.reach_score == 0.0
    assert result.flourish_score == 0.0


def test_action_maps_to_legacy_decision():
    """Rich actions do not leak into the legacy decision bucket."""
    assert _decision_from_action("APPLY_NOW") == "GO"
    assert _decision_from_action("WARM_INTRO_ONLY") == "MAYBE"
    assert _decision_from_action("WATCH") == "MAYBE"
    assert _decision_from_action("USE_AS_GAP_SIGNAL") == "FLAG"
    assert _decision_from_action("MONITOR") == "FLAG"
    assert _decision_from_action("DROP") == "DROP"


def test_multi_axis_calibration_examples():
    """Spot-check the low-reach and applied-role calibration examples."""
    assert _compute_combined(8, 2, 3) == 4.0
    assert _action_from_scores(8, 2, 3) == "USE_AS_GAP_SIGNAL"
    assert _decision_from_action(_action_from_scores(8, 2, 3)) == "FLAG"

    assert _compute_combined(8, 7, 8) == 7.6
    assert _action_from_scores(8, 7, 8) == "APPLY_NOW"
    assert _decision_from_action(_action_from_scores(8, 7, 8)) == "GO"


def test_match_result_rejects_out_of_range():
    """Pydantic rejects scores outside 0-10."""
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        MatchResult(score=-1.0, decision="DROP", reasoning="")
    with pytest.raises(pydantic.ValidationError):
        MatchResult(score=11.0, decision="GO", reasoning="")


# ── Re-apply kill ────────────────────────────────────────────────────────

def test_reapply_kill_triggers_on_same_role_with_rejection(isolated_config):
    """Recent rejection for same role triggers auto-DROP."""
    from matcher import _check_reapply_kill

    apps = isolated_config["applications"]
    recent = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    d = apps / f"{recent.replace('-', '')}-Acme-AI_Scientist"
    d.mkdir()
    (d / f"{recent.replace('-', '')}-Acme-AI_Scientist.txt").write_text(
        f"Applied.\n\nRejection received {recent}: 'decided not to move forward'"
    )

    result = _check_reapply_kill("Acme", "AI Scientist", applications_dir=apps)
    assert result is not None
    assert "rejection" in result.lower()


def test_reapply_kill_no_trigger_if_no_rejection(isolated_config):
    """No rejection marker in the .txt file means no kill."""
    from matcher import _check_reapply_kill

    apps = isolated_config["applications"]
    recent = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    d = apps / f"{recent.replace('-', '')}-Acme-AI_Scientist"
    d.mkdir()
    (d / f"{recent.replace('-', '')}-Acme-AI_Scientist.txt").write_text("Applied. No response yet.")

    assert _check_reapply_kill("Acme", "AI Scientist", applications_dir=apps) is None


def test_reapply_kill_no_trigger_if_old_enough(isolated_config):
    """Application older than 6 months ago is outside the cooldown."""
    from matcher import _check_reapply_kill, REAPPLY_ORG_COOLDOWN_MONTHS

    apps = isolated_config["applications"]
    # Place the rejection older than both the role cooldown AND the org cooldown.
    old_date = (datetime.now() - timedelta(days=REAPPLY_ORG_COOLDOWN_MONTHS * 30 + 60)).strftime("%Y-%m-%d")
    d = apps / f"{old_date.replace('-', '')}-Acme-AI_Scientist"
    d.mkdir()
    (d / f"{old_date.replace('-', '')}-Acme-AI_Scientist.txt").write_text(
        f"Applied.\n\nRejection received {old_date}"
    )

    assert _check_reapply_kill("Acme", "AI Scientist", applications_dir=apps) is None


def test_reapply_kill_no_trigger_for_different_role(isolated_config):
    """Different role at the same org IS blocked (org-level cooldown)."""
    from matcher import _check_reapply_kill

    apps = isolated_config["applications"]
    recent = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    d = apps / f"{recent.replace('-', '')}-Acme-ML_Engineer"
    d.mkdir()
    (d / f"{recent.replace('-', '')}-Acme-ML_Engineer.txt").write_text(
        f"Applied.\n\nRejection received {recent}"
    )

    # Different role (AI Scientist, not ML Engineer) — but same org, so
    # the org-level cooldown (1.5y default) fires.
    result = _check_reapply_kill("Acme", "AI Scientist", applications_dir=apps)
    assert result is not None
    assert "Org-level cooldown" in result


def test_org_cooldown_fires_for_different_role(isolated_config):
    """Org-level cooldown is the catch-all for 'same org, different role'."""
    from matcher import _check_reapply_kill, REAPPLY_ORG_COOLDOWN_MONTHS

    apps = isolated_config["applications"]
    recent = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    d = apps / f"{recent.replace('-', '')}-Poolside-Member_of_Engineering_Evaluations"
    d.mkdir()
    (d / f"{recent.replace('-', '')}-Poolside-Member_of_Engineering_Evaluations.txt").write_text(
        f"Applied.\n\nRejection received {recent}: decided not to move forward."
    )

    # New Poolside role with a completely different title.
    r = _check_reapply_kill(
        "Poolside", "Member of Engineering (Pre-training / Data Acquisition)",
        applications_dir=apps,
    )
    assert r is not None
    assert "Org-level cooldown" in r
    assert "Poolside" in r
    # The cooldown is the configured one (default 18 months).
    assert f"{REAPPLY_ORG_COOLDOWN_MONTHS} months" in r


def test_org_cooldown_respects_env_override(isolated_config, monkeypatch):
    """Setting LOVEWORK_REAPPLY_ORG_COOLDOWN_MONTHS=0 disables the org-level kill."""
    monkeypatch.setenv("LOVEWORK_REAPPLY_ORG_COOLDOWN_MONTHS", "0")
    # Reload the matcher module so the env var is re-read at module load time.
    import importlib
    import matcher
    importlib.reload(matcher)
    from matcher import _check_reapply_kill, REAPPLY_ORG_COOLDOWN_MONTHS
    assert REAPPLY_ORG_COOLDOWN_MONTHS == 0

    apps = isolated_config["applications"]
    recent = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    d = apps / f"{recent.replace('-', '')}-Acme-ML_Engineer"
    d.mkdir()
    (d / f"{recent.replace('-', '')}-Acme-ML_Engineer.txt").write_text(
        f"Applied.\n\nRejection received {recent}"
    )

    # Same role, same org, recent rejection — only the role cooldown fires,
    # not the org cooldown (which is disabled).
    r = _check_reapply_kill("Acme", "ML Engineer", applications_dir=apps)
    assert r is not None
    # The role cooldown message doesn't mention "Org-level".
    assert "Org-level" not in r


def test_reapply_kill_no_trigger_for_no_prior_application(isolated_config):
    """If no prior application, no kill."""
    from matcher import _check_reapply_kill
    apps = isolated_config["applications"]
    assert _check_reapply_kill("NeverAppliedTo", "ML Engineer", applications_dir=apps) is None
