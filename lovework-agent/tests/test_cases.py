"""
Tests for the lead → case terminology (cases.py).

No filesystem pollution — the `tmp_path` fixture isolates each test.
"""

from datetime import date
from pathlib import Path

import pytest

from cases import (
    SLUG_RE,
    case_dir,
    case_status,
    is_case_open,
    make_case_dir,
    parse_slug,
    slug_for,
)


# ── slug_for ─────────────────────────────────────────────────────────────

def test_slug_for_basic():
    s = slug_for(date(2026, 6, 23), "Anthropic", "AI Research Engineer")
    assert s == "20260623-Anthropic-AI_Research_Engineer"


def test_slug_for_unsafe_chars():
    s = slug_for(date(2026, 6, 23), "Acme/Co & Sons", "Senior ML/AI Engineer (Remote)")
    assert "/" not in s
    assert "&" not in s
    assert "(" not in s
    assert s.startswith("20260623-AcmeCo_Sons-")


def test_slug_for_truncates_long_role():
    s = slug_for(date(2026, 6, 23), "Acme", "X" * 200)
    # Role part caps at 60.
    parts = s.split("-")
    role_part = "-".join(parts[2:])
    assert len(role_part) <= 60


def test_slug_for_empty_inputs_fall_back():
    s = slug_for(date(2026, 6, 23), "", "")
    # We still get a date prefix and a non-empty body.
    assert s.startswith("20260623-")
    parts = s.split("-")
    assert len(parts) >= 3


def test_slug_for_requires_date_type():
    with pytest.raises(TypeError):
        slug_for("2026-06-23", "Acme", "Engineer")  # type: ignore[arg-type]


# ── parse_slug ───────────────────────────────────────────────────────────

def test_parse_slug_basic():
    p = parse_slug("20260623-Anthropic-AI_Engineer")
    assert p == {"date": "20260623", "org": "Anthropic", "role": "AI_Engineer"}


def test_parse_slug_invalid():
    assert parse_slug("not-a-slug") is None
    assert parse_slug("2026-06-23-Acme-Eng") is None  # dashes in date
    assert parse_slug("") is None
    assert parse_slug(None) is None  # type: ignore[arg-value]


# ── case_dir / is_case_open / make_case_dir ──────────────────────────────

def test_make_case_dir_creates_readme(tmp_path):
    slug = "20260623-Anthropic-AI_Engineer"
    d = make_case_dir(
        slug,
        title="AI Engineer",
        url="https://anthropic.com/careers/123",
        source="hn_hiring",
        score=7.5,
        decision="GO",
        reasoning="Strong fit.",
        cases_root=tmp_path,
    )
    assert d == tmp_path / slug
    assert d.is_dir()
    readme = d / "README.md"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8")
    assert "Anthropic" in text
    assert "AI Engineer" in text
    assert "hn_hiring" in text
    assert "7.5" in text
    assert "Strong fit." in text


def test_is_case_open_false_then_true(tmp_path):
    slug = "20260623-Acme-Role"
    assert not is_case_open(slug, cases_root=tmp_path)
    make_case_dir(slug, cases_root=tmp_path)
    assert is_case_open(slug, cases_root=tmp_path)


def test_make_case_dir_idempotent(tmp_path):
    """Re-running does not delete the CV / cover letter that LJ wrote."""
    slug = "20260623-Acme-Role"
    d = make_case_dir(slug, cases_root=tmp_path, reasoning="first pass")
    cv = d / "cvlj-acme.md"
    cv.write_text("CV goes here", encoding="utf-8")
    # Re-run with different metadata.
    make_case_dir(slug, cases_root=tmp_path, reasoning="second pass")
    # CV untouched.
    assert cv.read_text(encoding="utf-8") == "CV goes here"
    # README now reflects the latest reasoning.
    assert "second pass" in (d / "README.md").read_text(encoding="utf-8")


def test_make_case_dir_rejects_invalid_slug(tmp_path):
    with pytest.raises(ValueError):
        make_case_dir("not-a-slug", cases_root=tmp_path)


# ── case_status ──────────────────────────────────────────────────────────

def test_case_status_none_when_missing(tmp_path):
    assert case_status("20260623-Nowhere-Role", cases_root=tmp_path) == "none"


def test_case_status_open_by_default(tmp_path):
    make_case_dir("20260623-Acme-Role", cases_root=tmp_path)
    assert case_status("20260623-Acme-Role", cases_root=tmp_path) == "open"


def test_case_status_submitted(tmp_path):
    slug = "20260623-Acme-Role"
    d = make_case_dir(slug, cases_root=tmp_path)
    readme = d / "README.md"
    text = readme.read_text(encoding="utf-8")
    text = text.replace("- [ ] Submitted", "- [x] Submitted")
    readme.write_text(text, encoding="utf-8")
    assert case_status(slug, cases_root=tmp_path) == "submitted"


def test_case_status_closed(tmp_path):
    slug = "20260623-Acme-Role"
    d = make_case_dir(slug, cases_root=tmp_path)
    readme = d / "README.md"
    text = readme.read_text(encoding="utf-8")
    # Filling the Outcome line counts as closed.
    text = text.replace(
        "- [ ] Outcome (rejected / withdrawn / accepted)",
        "- [x] Outcome: rejected — role filled internally",
    )
    readme.write_text(text, encoding="utf-8")
    assert case_status(slug, cases_root=tmp_path) == "closed"
