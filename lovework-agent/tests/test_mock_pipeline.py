"""Phase C tests for the SuperMe mock interview pipeline.

C.3: walk the full mock sequence and assert the contract.
C.4: transcript shape for mock messages.
"""

import json

import pytest

from interview_providers.mock_pipeline import MockSuperMeSession
from interview_providers.request_sequence import (
    ALL_STEPS,
    AUTH_STEPS,
    ONBOARDING_STEPS,
    INTERVIEW_STEPS,
    SANDBOX_STEPS,
    full_flow,
    required_steps,
    steps_by_phase,
)


# ── Request sequence (C.1) ───────────────────────────────────────────────


def test_request_sequence_covers_all_phases():
    phases = {s.phase for s in ALL_STEPS}
    assert phases == {"auth", "onboarding", "interview", "sandbox"}


def test_request_sequence_step_count():
    assert len(AUTH_STEPS) == 2
    assert len(ONBOARDING_STEPS) == 13
    assert len(INTERVIEW_STEPS) == 7
    assert len(SANDBOX_STEPS) == 2
    assert len(ALL_STEPS) == 24


def test_request_sequence_auth_requirements():
    """Auth steps and GET /roles are public; onboarding and other interview steps require auth."""
    for step in AUTH_STEPS:
        assert step.expects_auth is False, f"Step {step.step} should be public"
    # GET /roles/{company_id} is public per the SuperMe spec.
    public_interview = [s for s in INTERVIEW_STEPS if not s.expects_auth]
    assert len(public_interview) == 1
    assert public_interview[0].path == "/roles/{company_id}"
    for step in ONBOARDING_STEPS + SANDBOX_STEPS:
        assert step.expects_auth is True, f"Step {step.step} should require auth"
    for step in INTERVIEW_STEPS:
        if step.path != "/roles/{company_id}":
            assert step.expects_auth is True, f"Step {step.step} should require auth"


def test_request_sequence_all_steps_have_mock_query():
    for step in ALL_STEPS:
        assert "dry_run=true" in step.mock_query
        assert "mock=true" in step.mock_query


def test_request_sequence_required_steps_excludes_optional():
    required = required_steps()
    optional = [s for s in ALL_STEPS if s.optional]
    assert len(optional) > 0
    for step in optional:
        assert step not in required


def test_request_sequence_steps_by_phase():
    auth = steps_by_phase("auth")
    assert len(auth) == 2
    assert all(s.phase == "auth" for s in auth)


def test_request_sequence_expected_keys_not_empty():
    for step in ALL_STEPS:
        assert len(step.expected_keys) > 0, f"Step {step.step} has no expected_keys"


# ── MockSuperMeSession: guard (C.2) ──────────────────────────────────────


def test_mock_session_requires_mock_flags():
    """No-arg instantiation OK; dry_run=False or mock=False raises."""
    session = MockSuperMeSession("test@example.com")
    assert session.is_authenticated is False
    session2 = MockSuperMeSession("test@example.com", dry_run=True, mock=True)
    assert session2.is_authenticated is False

    with pytest.raises(ValueError, match="dry_run"):
        MockSuperMeSession("test@example.com", dry_run=False, mock=True)
    with pytest.raises(ValueError, match="mock"):
        MockSuperMeSession("test@example.com", dry_run=True, mock=False)
    with pytest.raises(ValueError, match="dry_run"):
        MockSuperMeSession("test@example.com", dry_run=False, mock=False)


# ── MockSuperMeSession: login flow ───────────────────────────────────────


def test_mock_login_flow():
    """request_magic_link + login returns backend_token, is_authenticated=True."""
    s = MockSuperMeSession("agent@lovework.be")
    ml = s.request_magic_link()
    assert ml["success"] is True
    assert s.is_authenticated is False

    login = s.login()
    assert login["backend_token"].startswith("mock-bt-")
    assert s.is_authenticated is True
    assert s.backend_token == login["backend_token"]


# ── MockSuperMeSession: onboarding flow ──────────────────────────────────


def test_mock_onboarding_flow():
    """set_linkedin → discover_accounts → connect_social → profile → learning_stats → complete."""
    s = MockSuperMeSession("agent@lovework.be")
    s.request_magic_link()
    s.login()

    li = s.set_linkedin("https://linkedin.com/in/mockuser")
    assert li["success"] is True

    accounts = s.discover_accounts("mockuser")
    assert "confident" in accounts["social_accounts"]

    longform = s.discover_longform("mockuser")
    assert "content" in longform

    connected = s.connect_social("github", "mock-user")
    assert connected["platform"] == "github"

    content = s.add_content(["https://example.com/post"])
    assert content["queued_count"] == 1

    prof = s.profile()
    assert "connected_accounts" in prof

    stats = s.learning_stats()
    assert stats["total_learnings"] == 5

    s.complete_onboarding()
    assert s.is_onboarded is True


# ── MockSuperMeSession: interview start ──────────────────────────────────


def test_mock_interview_start():
    """start_interview returns interview_id, _interview_id set."""
    s = MockSuperMeSession("agent@lovework.be")
    s.request_magic_link()
    s.login()
    s.set_linkedin("u")
    s.complete_onboarding()

    start = s.start_interview("role-001")
    assert start["interview_id"] is not None
    assert start["interview_id"] == s.interview_id
    assert start["status"] == "preparing"


# ── MockSuperMeSession: message exchange ─────────────────────────────────


def test_mock_message_exchange():
    """send_message returns response with stage_status, appends to _transcript."""
    s = MockSuperMeSession("agent@lovework.be")
    s.request_magic_link()
    s.login()
    s.set_linkedin("u")
    s.complete_onboarding()
    s.start_interview("role-001")
    s.poll_interview()

    msg = s.send_message("I build production agentic systems.")
    assert msg["stage_status"] == "completed"
    assert msg["message"].startswith("[Mock")

    # Transcript has 2 entries: sent + received.
    assert len(s.transcript) == 2
    assert s.transcript[0]["kind"] == "interview_message_sent"
    assert s.transcript[0]["body_digest"].startswith("sha256:")
    assert s.transcript[1]["kind"] == "interview_message_received"


# ── MockSuperMeSession: recovery ─────────────────────────────────────────


def test_mock_recovery_returns_current_state_after_advancement():
    """Recovery reads current state — including after messages advance stages."""
    s = MockSuperMeSession("agent@lovework.be")
    s.request_magic_link()
    s.login()
    s.set_linkedin("u")
    s.complete_onboarding()
    s.start_interview("role-001")
    s.poll_interview()

    # Advance stage 1 (intro) by sending a message.
    s.send_message("I build agentic systems.")

    # Recovery should reflect the advanced state: stage 1 completed, stage 2 awaiting.
    recover = s.recover_interview()
    assert recover["interview_id"] == s.interview_id
    stages = {st["stage_name"]: st["status"] for st in recover["stages"]}
    assert stages["intro"] == "completed"
    assert stages["technical"] == "awaiting_input"


def test_mock_recovery_same_as_poll_before_messages():
    """Recovery reflects the poll lifecycle without advancing it."""
    s = MockSuperMeSession("agent@lovework.be")
    s.request_magic_link()
    s.login()
    s.set_linkedin("u")
    s.complete_onboarding()
    s.start_interview("role-001")

    poll = s.poll_interview()          # first → preparing
    assert poll["status"] == "preparing"

    recover = s.recover_interview()    # pure read → same
    assert recover["status"] == poll["status"]
    assert recover["interview_id"] == s.interview_id

    poll2 = s.poll_interview()         # second → awaiting_input
    assert poll2["status"] == "awaiting_input"

    recover2 = s.recover_interview()   # pure read → awaiting_input
    assert recover2["status"] == "awaiting_input"


# ── MockSuperMeSession: events ───────────────────────────────────────────


def test_mock_events_logged_for_every_action():
    """Every method call produces an event row with required fields."""
    s = MockSuperMeSession("agent@lovework.be")
    s.request_magic_link()
    s.login()
    s.set_linkedin("u")
    s.discover_accounts("u")
    s.connect_social("github", "u")
    s.add_content([])
    s.profile()
    s.learning_stats()
    s.complete_onboarding()
    s.start_interview("role-001")
    s.poll_interview()
    s.send_message("Hello")

    events = s.events
    assert len(events) >= 12
    for e in events:
        assert "at" in e
        assert "kind" in e
        assert e["mock"] is True
        assert "body_digest" in e
        assert e["body_digest"].startswith("sha256:")
        assert "phase" in e
        assert "endpoint" in e


def test_mock_events_jsonl_round_trip(tmp_path):
    """write_events produces valid JSONL, readable back."""
    s = MockSuperMeSession("agent@lovework.be")
    s.request_magic_link()
    s.login()
    s.start_interview("role-001")
    s.send_message("Test")

    events_path = tmp_path / "events.jsonl"
    count = s.write_events(events_path)
    assert count == len(s.events)

    lines = events_path.read_text().strip().split("\n")
    assert len(lines) == count
    for line in lines:
        row = json.loads(line)
        assert row["mock"] is True


# ── MockSuperMeSession: transcript ───────────────────────────────────────


def test_mock_transcript_jsonl_round_trip(tmp_path):
    """write_transcript produces JSONL without raw_body by default."""
    s = MockSuperMeSession("agent@lovework.be")
    s.request_magic_link()
    s.login()
    s.start_interview("role-001")
    s.poll_interview()
    s.send_message("Secret CV material")

    tx_path = tmp_path / "transcript.jsonl"
    count = s.write_transcript(tx_path)
    assert count == 2

    for line in tx_path.read_text().strip().split("\n"):
        row = json.loads(line)
        assert "raw_body" not in row, "raw_body must not be in default JSONL"
        assert "body_digest" in row


def test_mock_transcript_plaintext_mode_includes_raw_body(tmp_path):
    """write_transcript(plaintext=True) includes raw_body + sidecar .txt."""
    s = MockSuperMeSession("agent@lovework.be")
    s.request_magic_link()
    s.login()
    s.start_interview("role-001")
    s.poll_interview()
    s.send_message("Private info")

    tx_path = tmp_path / "transcript.jsonl"
    count = s.write_transcript(tx_path, plaintext=True)
    assert count == 2

    # JSONL includes raw_body in plaintext mode.
    for line in tx_path.read_text().strip().split("\n"):
        row = json.loads(line)
        assert "raw_body" in row

    # Sidecar .txt exists.
    txt_path = tx_path.with_suffix(".txt")
    assert txt_path.exists()
    txt_content = txt_path.read_text()
    assert "Private info" in txt_content
    assert "sha256:" in txt_content


# ── MockSuperMeSession: manual stage guard ───────────────────────────────


def test_mock_manual_stage_guard():
    """Sending to a non-manual stage raises ValueError."""
    s = MockSuperMeSession("agent@lovework.be")
    s.request_magic_link()
    s.login()
    s.set_linkedin("u")
    s.complete_onboarding()
    s.start_interview("role-001")
    s.poll_interview()

    # Stage 0 (system_init) is not manual — sending should fail.
    with pytest.raises(ValueError, match="not a manual stage"):
        s.send_message("This should fail", stage_number=0)


# ── MockSuperMeSession: no real network ──────────────────────────────────


def test_mock_no_real_network_writes():
    """Session never imports httpx for sending."""
    import interview_providers.mock_pipeline as mod
    # The module should not import httpx at all.
    assert "httpx" not in dir(mod), (
        "mock_pipeline.py must not import httpx — the mock session never makes real network writes"
    )


# ── Request sequence + mock pipeline integration ─────────────────────────


def test_mock_session_deterministic_uid():
    """Same email + seed → same UID across instances."""
    s1 = MockSuperMeSession("agent@lovework.be")
    s2 = MockSuperMeSession("agent@lovework.be")
    s1.request_magic_link()
    s1.login()
    s2.request_magic_link()
    s2.login()
    assert s1.backend_token == s2.backend_token


def test_mock_session_different_emails_different_uids():
    s1 = MockSuperMeSession("a@lovework.be")
    s2 = MockSuperMeSession("b@lovework.be")
    s1.login()
    s2.login()
    assert s1.backend_token != s2.backend_token
