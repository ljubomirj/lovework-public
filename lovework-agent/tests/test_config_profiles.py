"""
Tests for profile loading.

The matcher is only as good as the profile. These tests verify:
- Profiles load from the correct locations
- Missing files are handled gracefully
- Role selection works
- The combined profile text is properly assembled
"""

import pytest

import config


def test_list_roles_for_known_profiles():
    """The bundled profiles (lj, vj, kj, pk, example) should have known roles."""
    for profile in ("lj", "vj", "kj", "pk", "example"):
        roles = config.list_roles(profile)
        assert len(roles) > 0, f"{profile} should have at least one role"


def test_lj_has_general_role():
    """LJ has a 'general' role."""
    assert "general" in config.list_roles("lj")


def test_vj_has_data_statistics_pricing_primary_role():
    """VJ's primary role is statistics/pricing, not the historical SRE track."""
    assert "data-statistics-pricing" in config.list_roles("vj")


def test_load_lj_default():
    """LJ's general profile loads without error."""
    text = config.load_profile_text("lj", role="general")
    assert "LJ" in text or "Ljubomir" in text or "Soul" in text
    # Soul section
    assert "PRINCIPAL SOUL" in text
    # CV section
    assert "PRINCIPAL CV" in text
    # Role section
    assert "ROLE: general" in text


def test_load_lj_all_roles():
    """All LJ roles load successfully."""
    for role in config.list_roles("lj"):
        text = config.load_profile_text("lj", role=role)
        assert f"ROLE: {role}" in text, f"Role section missing for {role}"


def test_load_vj_default():
    """VJ's default profile loads."""
    text = config.load_profile_text("vj", role="data-statistics-pricing")
    assert "PRINCIPAL SOUL" in text
    assert "ROLE: data-statistics-pricing" in text
    assert "not an ml/ai/nlp career" in text.lower()


def test_load_example_profile():
    """The example/anonymised profile loads."""
    text = config.load_profile_text("example", role="general")
    assert "Alex" in text or "Example" in text


def test_load_profile_text_returns_string():
    """The combined text is a non-empty string."""
    text = config.load_profile_text("lj", role="general")
    assert isinstance(text, str)
    assert len(text) > 1000, "Profile text should be substantial"


def test_unknown_role_raises_value_error():
    """Asking for a non-existent role raises a helpful error."""
    with pytest.raises(ValueError) as exc_info:
        config.load_profile_text("lj", role="nonexistent_role")
    assert "nonexistent_role" in str(exc_info.value)
    assert "Available" in str(exc_info.value)


def test_load_bio_returns_string():
    """The long bio is a string (possibly empty)."""
    bio = config.load_bio("lj")
    assert isinstance(bio, str)
    # LJ's bio should be substantial
    assert len(bio) > 1000


def test_profile_text_includes_separators():
    """Sections are separated by --- for clarity."""
    text = config.load_profile_text("lj", role="general")
    assert "---" in text
    sections = text.split("---")
    assert len(sections) >= 3, "Should have at least 3 sections separated by ---"



def test_lj_quant_track_is_paused_except_ai_ml_bridge():
    """LJ should not be steered back into plain quant trading/research."""
    text = config.load_profile_text("lj", role="ai-finance").lower()
    assert "plain quant" in text
    assert "paused" in text
    assert "ai/ml" in text
    assert "bridge" in text


def test_kj_is_kalen_and_profession_is_chemist():
    """KJ/Kalen has chosen chemist as profession; LoveWork is role search, not profession discovery."""
    text = config.load_profile_text("kj", role="cheminf").lower()
    assert "kalen" in text
    assert "chemist" in text


def test_vj_is_vedar_profession_and_job_search():
    """VJ/Vedar is looking for both a profession and a concrete job."""
    text = config.load_profile_text("vj", role="general").lower()
    assert "vedar" in text
    assert "profession" in text
    assert "job" in text
    assert "thicket" in text or "emergent" in text

# ── Layer 3: branching possibilities ────────────────────────────────────


def test_lj_possibilities_section_loaded():
    """LJ's profile includes the branching-possibilities section when the file exists."""
    text = config.load_profile_text("lj", role="general")
    assert "PRINCIPAL POSSIBILITIES" in text, (
        "possibilities.md should be loaded into the LJ profile text"
    )
    # At least one labelled branch from the structured file
    assert "(a)" in text or "matcher signal" in text.lower()


def test_example_possibilities_section_loaded():
    """The example profile demonstrates the 3-layer structure."""
    text = config.load_profile_text("example", role="general")
    assert "PRINCIPAL POSSIBILITIES" in text


def test_vj_possibilities_section_loaded():
    """VJ's profile now includes the branching-possibilities section."""
    text = config.load_profile_text("vj", role="data-statistics-pricing")
    assert "PRINCIPAL SOUL" in text
    assert "PRINCIPAL POSSIBILITIES" in text


def test_vj_profession_framing_present():
    """The 'profession not a job' framing is the key VJ signal — must be in the loaded profile."""
    text = config.load_profile_text("vj", role="general")
    # Both soul and possibilities carry the framing; check both
    assert "profession" in text.lower()
    assert "thicket" in text.lower() or "emergent" in text.lower()


def test_profile_without_possibilities_is_graceful():
    """A profile dir with no possibilities.md must load without error.

    Synthetic check: temporarily point at a temp dir to guarantee absence.
    Verifies the loader's `if poss.exists()` guard works for future profiles
    that don't have a Layer 3 yet.
    """
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td) / "noprof"
        tdir.mkdir()
        (tdir / "soul.md").write_text("# stub soul\n", encoding="utf-8")
        orig = config.PROFILES_DIR
        try:
            config.PROFILES_DIR = Path(td)
            text = config.load_profile_text("noprof")
            assert "PRINCIPAL SOUL" in text
            assert "PRINCIPAL POSSIBILITIES" not in text
        finally:
            config.PROFILES_DIR = orig


def test_possibilities_section_sits_between_cv_and_role():
    """Layer ordering: soul → work-auth → cv-short → possibilities → role."""
    text = config.load_profile_text("lj", role="general")
    cv_pos = text.find("PRINCIPAL CV (short)")
    poss_pos = text.find("PRINCIPAL POSSIBILITIES")
    role_pos = text.find("ROLE: general")
    assert cv_pos != -1 and poss_pos != -1 and role_pos != -1
    assert cv_pos < poss_pos < role_pos, (
        "possibilities section must appear after CV and before the role section"
    )


def test_bio_long_resolves():
    """Layer 1 (bio-long.md) resolves to the long CV — for LJ a plain copy of
    the canonical CV. Must load as a substantial string."""
    bio = config.load_bio("lj")
    assert isinstance(bio, str)
    assert len(bio) > 1000, "bio-long.md should resolve to the long CV"
    # Sanity: the long CV opens with LJ's name
    assert "Ljubomir" in bio or "JOSIFOVSKI" in bio


# ── KJ (Kalen) profile ──────────────────────────────────────────────────


def test_kj_has_cheminf_role():
    """KJ's primary track is cheminf."""
    assert "cheminf" in config.list_roles("kj")


def test_load_kj_cheminf():
    """KJ's cheminf profile loads with all 3 layers + work-auth + role."""
    text = config.load_profile_text("kj", role="cheminf")
    assert "PRINCIPAL SOUL" in text
    assert "WORK AUTHORIZATION" in text
    assert "PRINCIPAL CV" in text
    assert "PRINCIPAL POSSIBILITIES" in text
    assert "ROLE: cheminf" in text
    assert "Kalen" in text


def test_load_kj_bio():
    """KJ's Layer 1 (bio-long.md) loads as a substantial CV string."""
    bio = config.load_bio("kj")
    assert isinstance(bio, str)
    assert len(bio) > 1000
    assert "Kalen" in bio


# ── VJ (Vedar) profile — 3-layer restructure ────────────────────────────


def test_load_vj_all_sections():
    """VJ's profile loads with all 5 sections in order."""
    text = config.load_profile_text("vj", role="general")
    assert "PRINCIPAL SOUL" in text
    assert "WORK AUTHORIZATION" in text
    assert "PRINCIPAL CV" in text
    assert "PRINCIPAL POSSIBILITIES" in text
    assert "ROLE: general" in text
    # Layer 2 (cv-short) should now differ from Layer 1 (bio-long) — it's the tip
    assert "Vedar" in text
    # Ordering
    cv = text.find("PRINCIPAL CV (short)")
    po = text.find("PRINCIPAL POSSIBILITIES")
    ro = text.find("ROLE: general")
    assert cv < po < ro


def test_vj_layer2_differs_from_layer1():
    """VJ's cv-short.md (tip) must be a tightened composite, not identical to bio-long."""
    bio = config.load_bio("vj")
    import importlib
    # Read cv-short directly via the config path for a clean size comparison
    from pathlib import Path
    cv_path = config.PROFILES_DIR / "vj" / "cv-short.md"
    bio_path = config.PROFILES_DIR / "vj" / "bio-long.md"
    assert cv_path.exists() and bio_path.exists()
    # Layer 2 should be substantially shorter than Layer 1 (it's the tip)
    assert cv_path.stat().st_size < bio_path.stat().st_size


# ── PK (Petroula) profile — 3-layer model, third-profession mode ────────


def test_pk_has_digital_art_role():
    """PK's primary principal third profession is digital-art."""
    assert "digital-art" in config.list_roles("pk")


def test_load_pk_all_sections():
    """PK's profile loads with all 5 sections in order."""
    text = config.load_profile_text("pk", role="digital-art")
    assert "PRINCIPAL SOUL" in text
    assert "WORK AUTHORIZATION" in text
    assert "PRINCIPAL CV" in text
    assert "PRINCIPAL POSSIBILITIES" in text
    assert "ROLE: digital-art" in text
    assert "Petroula" in text
    cv = text.find("PRINCIPAL CV (short)")
    po = text.find("PRINCIPAL POSSIBILITIES")
    ro = text.find("ROLE: digital-art")
    assert cv < po < ro


def test_pk_profession_framing_present():
    """The 'third profession' framing is the key PK signal."""
    text = config.load_profile_text("pk", role="general")
    assert "profession" in text.lower()
    assert "third profession" in text.lower() or "thicket" in text.lower()


def test_load_pk_bio():
    """PK's Layer 1 (bio-long.md) loads as a substantial CV string."""
    bio = config.load_bio("pk")
    assert isinstance(bio, str)
    assert len(bio) > 1000
    assert "Petroula" in bio
