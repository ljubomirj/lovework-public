"""Model calibration: make a model swap a measurement change, not a tier wipe.

`mimo-v2.6-flash` is the model LJ wants to keep (2026-09-22), and it scores on a
different scale from the `mimo-v2.5` it replaced. Measured on the same 20 VJ
adverts, same profile, same prompt:

| model | mean | median | decisions |
|---|---|---|---|
| `mimo-v2.5` | 5.01 | 5.05 | GO 1 · MAYBE 11 · FLAG 3 · DROP 5 |
| `mimo-v2.6-flash` | 2.46 | 3.00 | GO 0 · MAYBE 2 · FLAG 6 · DROP 12 |

Left alone, that offset silently empties the GO tier: VJ's full-corpus
reassessment went 22 GO → 1 GO, purely from the model. So scores are shifted
onto the reference scale before anything reads them, and the GO gate is applied
to the *calibrated* score rather than left to the model's own action choice
(`_decision_from_action` — which is how a harsher model could delete a tier with
nothing in the code noticing).

The floor itself is empirical, not invented: among 133 adverts LJ/VJ actually
applied to, the score distribution is min 2.4 · p25 7.20 · median 7.70, and a
7.0 floor admits 83 % of them. That is the "known-to-be-ok" anchor — the score at
which past leads earned action.

Recalibrate with `model_compare.py` whenever a model is added (it prints the
per-model means), then update `MODEL_OFFSETS` here with the measurement date.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

#: reference model the offsets are expressed against — the scale the empirical
#: floor was measured on.
REFERENCE_MODEL = os.getenv("LOVEWORK_CALIBRATION_REFERENCE", "mimo-v2.5")

#: Offset added to every score axis to bring a model onto the reference scale.
#: Measured with `model_compare.py` on VJ adverts (same corpus, profile and
#: prompt), calibration switched off so the measurement is not circular:
#:
#:   2026-09-22  n=20   mimo-v2.6-flash +2.55   (first estimate)
#:   2026-09-22  n=58   mimo-v2.6-flash +1.69   deepseek-v4.1-flash +1.30
#:                      — tie-excluded means over 41 live pairs (58 minus the
#:                      adverts every model kills at 0.0, which are not signal).
#:
#: The first estimate was ~50 % high: a small sample plus the zero-tie mass. Use
#: the larger figure. The offset corrects the *level* only — per-advert spread is
#: ±1.9, so the models still disagree advert by advert (decision agreement 0.65-
#: 0.72) — but it aligns the tier: on that sample the new model had 1 advert at
#: or above the 7.0 floor against the reference's 7, and +1.7 brings it to 6.
MODEL_OFFSETS: dict[str, float] = {
    "mimo-v2.5": 0.0,             # reference
    "mimo-v2.6-flash": 1.7,       # measured 2026-09-22, n=58 (41 live pairs)
    "deepseek-v4.1-flash": 1.3,   # measured 2026-09-22, n=58 (41 live pairs)
}

#: Scores are 0-10; a shift is clamped to that range.
SCORE_MIN, SCORE_MAX = 0.0, 10.0

#: The calibrated score each tier requires. GO is anchored on pursued adverts
#: (p25 = 7.20; 7.0 admits 83 % of them — see the module docstring); MAYBE and
#: FLAG come from the observed bands in the reports before the tiers were
#: banded at all (VJ's MAYBE band was 5.4-6.6, KJ's 5.4-6.6).
GO_FLOOR = float(os.getenv("LOVEWORK_GO_FLOOR", "7.0"))
MAYBE_FLOOR = float(os.getenv("LOVEWORK_MAYBE_FLOOR", "5.5"))
FLAG_FLOOR = float(os.getenv("LOVEWORK_FLAG_FLOOR", "4.0"))


def calibration_disabled() -> bool:
    """True while a tool is measuring raw model output.

    `model_compare.py` sets `LOVEWORK_CALIBRATION=off`: measuring the scale while
    applying the offset would be circular, and it would report the offset as if
    the models agreed. Read on every call (not at import) so a tool can toggle it.
    """
    return os.getenv("LOVEWORK_CALIBRATION", "").strip().lower() in ("off", "0", "raw", "none", "false")


def offset_for(model: str) -> float:
    """The measured calibration offset for a model (0.0 when unknown)."""
    if calibration_disabled():
        return 0.0
    return MODEL_OFFSETS.get((model or "").strip(), 0.0)


def shift(score: Optional[float], offset: float) -> Optional[float]:
    """Move one axis onto the reference scale, clamped to 0-10."""
    if score is None:
        return None
    return round(max(SCORE_MIN, min(SCORE_MAX, float(score) + offset)), 2)


def calibration_version() -> str:
    """A short fingerprint of the offsets in force.

    Caching callers put this in their key: a cached score was produced *under a
    particular offset*, so re-measuring must not keep serving the old one. The
    tier floors are deliberately absent — bands are re-applied on every cache
    read (see assessment_cache), so tuning a floor costs nothing.
    """
    payload = json.dumps({"offsets": MODEL_OFFSETS, "reference": REFERENCE_MODEL}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def calibrate_result(result, model: str) -> float:
    """Shift a `MatchResult` onto the reference scale in place; return the offset.

    The axes move together with the combined score, so the report cannot show a
    calibrated total next to uncalibrated parts. `recommended_action` is the
    model's own advice and is deliberately left alone — the offset corrects the
    *scale*, not the judgement.
    """
    offset = offset_for(model)
    if not offset:
        return 0.0
    # Already shifted? The **field** says so now; the old text marker is honoured for
    # rows cached before 2026-10-20, and never written again (see the note below).
    if float(getattr(result, "calibration_offset", 0.0) or 0.0):
        return offset
    if "CALIBRATED" in str(getattr(result, "reasoning", "")):
        return offset
    for field in ("fit_score", "reach_score", "flourish_score", "combined_score", "score"):
        value = getattr(result, field, None)
        if value is not None:
            setattr(result, field, shift(value, offset))
    # **Nothing is written into the prose.** LJ, 2026-10-20: *"Even if the model used
    # is no longer mimo-v2.5 — so any adjustment we are doing for 'mimo-v2.5' is wrong,
    # and the above is misleading, cryptic and needless. Even if we were doing the same
    # adjustment, and correctly — it should not be part of the user message and
    # reporting!"* The 2026-10-18 change only shortened the prefix; it kept the marker
    # in the reasoning, in the dossier, and on every pack's front page (155 files at the
    # time of writing). The idempotence fact belongs in a field, where the cache can
    # round-trip it and no reader has to parse a sentence.
    result.calibration_offset = offset
    logger.debug("[calibration] %s shifted by +%.2f", model, offset)
    return offset


def apply_tier_bands(result, *, go: Optional[float] = None,
                     maybe: Optional[float] = None,
                     flag: Optional[float] = None) -> bool:
    """Set the tier from the calibrated score. True when the decision changed.

    A model swap moves scores; bands keep the *labels* meaning one thing. So the
    tier is a deterministic function of the calibrated score, whether or not the
    model itself said `APPLY_NOW` — and a model claiming GO below the floor is
    demoted, because "GO" has to mean the same thing across runs.

    Banding *every* tier, not just GO, is what makes a weak-market run usable:
    with only GO banded, a harsh model on a thin week produced reports with no
    GO and no MAYBE at all — KJ's 2026-09-22 sweep put a 6.4 advert (his own
    historical MAYBE band is 5.4-6.6) in FLAG purely because the model's action
    word said `USE_AS_GAP_SIGNAL`.

    Deterministic kills and caps are never promoted: their reasoning starts with
    `AUTO-DROP:`, and they carry a score at or below the FLAG floor anyway.
    """
    if calibration_disabled():
        return False
    go = GO_FLOOR if go is None else go
    maybe = MAYBE_FLOOR if maybe is None else maybe
    flag = FLAG_FLOOR if flag is None else flag
    score = getattr(result, "combined_score", None)
    if score is None:
        return False
    if str(getattr(result, "reasoning", "")).startswith("AUTO-DROP:"):
        return False

    score = float(score)
    tier = "GO" if score >= go else "MAYBE" if score >= maybe else "FLAG" if score >= flag else "DROP"
    if getattr(result, "decision", "") == tier:
        return False
    result.decision = tier
    return True
