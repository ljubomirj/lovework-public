"""Regression tests for profile-scoped Gmail OAuth subprocesses."""

import json
from pathlib import Path
from types import SimpleNamespace

import gmail_accessor as gmail


def test_run_gapi_passes_active_profile_to_google_script(monkeypatch, tmp_path):
    """A profile-specific script must not fall back to ~/.hermes' token."""
    profile = tmp_path / "profiles" / "hermel"
    profile.mkdir(parents=True)
    script = tmp_path / "google_api.py"
    script.write_text("# test placeholder\n")
    captured = {}

    monkeypatch.setattr(gmail, "resolve_hermes_home", lambda: profile)
    monkeypatch.setattr(gmail, "gapi_path", lambda: script)
    monkeypatch.setattr(gmail, "gapi_python", lambda: "test-python")

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout=json.dumps([]), stderr="")

    monkeypatch.setattr(gmail.subprocess, "run", fake_run)

    assert gmail.run_gapi("gmail", "search", "is:unread") == []
    assert captured["command"] == ["test-python", str(script), "gmail", "search", "is:unread"]
    assert captured["env"]["HERMES_HOME"] == str(profile)


def test_run_gapi_can_use_principal_credential_home(monkeypatch, tmp_path):
    """A source credential may differ from the Hermes runtime profile."""
    runtime_profile = tmp_path / "profiles" / "hermeo"
    runtime_profile.mkdir(parents=True)
    credential_home = tmp_path / "credentials" / "petroula-vj"
    credential_home.mkdir(parents=True)
    script = tmp_path / "google_api.py"
    script.write_text("# test placeholder\n")
    captured = {}

    monkeypatch.setattr(gmail, "resolve_hermes_home", lambda: runtime_profile)
    monkeypatch.setattr(gmail, "gapi_path", lambda: script)
    monkeypatch.setattr(gmail, "gapi_python", lambda: "test-python")

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout=json.dumps([]), stderr="")

    monkeypatch.setattr(gmail.subprocess, "run", fake_run)

    assert gmail.run_gapi("gmail", "search", "label:VJ-jobs", credential_home=credential_home) == []
    assert captured["command"][:3] == ["test-python", str(script), "gmail"]
    assert captured["env"]["HERMES_HOME"] == str(credential_home)
