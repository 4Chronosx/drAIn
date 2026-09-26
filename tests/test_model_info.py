"""Tests for the provenance block sent with every result."""

from __future__ import annotations

from drain import hazard
from drain.model_info import model_info, network_built_on


def test_the_network_date_comes_from_the_model_file():
    assert network_built_on() == "2025-11-18"


def test_it_never_claims_calibration():
    assert model_info()["calibrated"] is False
    assert model_info()["hazard_score"]["provisional"] is True


def test_the_weights_are_the_ones_the_scorer_uses():
    weights = model_info()["hazard_score"]["weights"]
    assert weights == {
        "flood_volume": hazard.VOLUME_WEIGHT,
        "duration_share_of_storm": hazard.DURATION_WEIGHT,
        "peak_rate": hazard.RATE_WEIGHT,
    }
    assert sum(weights.values()) == 1.0


def test_it_names_what_the_model_leaves_out():
    missing = " ".join(model_info()["not_modelled"]).lower()
    for gap in ("blocked", "tide", "wet", "storm"):
        assert gap in missing
