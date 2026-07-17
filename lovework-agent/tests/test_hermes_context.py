import os
from pathlib import Path

import pytest

import hermes_context


def test_known_host_maps_to_profile(tmp_path, monkeypatch):
    base = tmp_path / ".hermes-gigul2" / "profiles" / "hermel"
    base.mkdir(parents=True)
    monkeypatch.setattr(hermes_context.socket, "gethostname", lambda: "gigul2")
    monkeypatch.setenv("LOVEWORK_HERMES_BASE", str(tmp_path / ".hermes-gigul2"))
    monkeypatch.delenv("LOVEWORK_HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("LOVEWORK_HERMES_PROFILE", raising=False)
    assert hermes_context.resolve_hermes_home() == base


def test_explicit_profile_wins(tmp_path, monkeypatch):
    base = tmp_path / ".hermes-other" / "profiles" / "custom"
    base.mkdir(parents=True)
    monkeypatch.setenv("LOVEWORK_HERMES_BASE", str(tmp_path / ".hermes-other"))
    monkeypatch.setenv("LOVEWORK_HERMES_PROFILE", "custom")
    monkeypatch.delenv("LOVEWORK_HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert hermes_context.resolve_hermes_home() == base


def test_unknown_host_requires_profile_when_ambiguous(tmp_path, monkeypatch):
    (tmp_path / ".hermes-unknown" / "profiles" / "one").mkdir(parents=True)
    (tmp_path / ".hermes-unknown" / "profiles" / "two").mkdir(parents=True)
    monkeypatch.setattr(hermes_context.socket, "gethostname", lambda: "unknown")
    monkeypatch.setenv("LOVEWORK_HERMES_BASE", str(tmp_path / ".hermes-unknown"))
    monkeypatch.delenv("LOVEWORK_HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("LOVEWORK_HERMES_PROFILE", raising=False)
    with pytest.raises(RuntimeError, match="LOVEWORK_HERMES_PROFILE"):
        hermes_context.resolve_hermes_home()


def test_root_home_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    (root / "config.yaml").write_text("{}")
    monkeypatch.setenv("LOVEWORK_HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    with pytest.raises(RuntimeError, match="not a profile"):
        hermes_context.resolve_hermes_home()
