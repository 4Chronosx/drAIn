"""Tests for the provenance block sent with every result."""

from __future__ import annotations

import hashlib

from drain import hazard
from drain.model_info import model_info, network_built_on
from drain.paths import BASE_INP


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


def _storm_line(**kwargs):
    return next(
        line
        for line in model_info(**kwargs)["not_modelled"]
        if line.startswith("Real storm timing")
    )


def test_a_result_of_the_shipped_series_says_so():
    """It used to say every storm was a triangle; the default view is the
    shipped 24 h series."""
    assert _storm_line() == (
        "Real storm timing: the storm is the shipped 24 h rain series (TS_Rain, 266 mm), "
        "not an observed event."
    )
    assert _storm_line(custom_storm=False) == _storm_line()


def test_a_result_of_a_custom_storm_says_it_is_a_triangle():
    assert _storm_line(custom_storm=True) == (
        "Real storm timing: the storm is an idealised triangle of the given depth and duration."
    )


def test_the_shipped_series_really_is_24_hours_and_266_mm():
    """The figures in the sentence, checked against the network file."""
    rows = []
    in_series = False
    for line in BASE_INP.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("["):
            in_series = line.strip().upper() == "[TIMESERIES]"
            continue
        parts = line.split()
        if in_series and len(parts) >= 3 and parts[0] == "TS_Rain":
            rows.append((parts[1], float(parts[2])))
    assert rows[-1][0] == "24:00"
    # Intensities in mm/h every 5 minutes.
    assert round(sum(intensity for _, intensity in rows) * 5 / 60) == 266


def test_the_exposure_line_names_the_nearest_barangay_fallback():
    exposure = model_info()["exposure"]
    assert "nearest one within 250 m" in exposure
    assert "unmapped" in exposure


def test_the_model_version_is_the_network_files_sha256():
    from drain.model_info import network_sha256
    from drain.paths import BASE_INP

    expected = hashlib.sha256(BASE_INP.read_bytes()).hexdigest()
    assert network_sha256() == expected
    assert model_info()["network_sha256"] == expected


def test_a_different_network_file_has_a_different_version(tmp_path):
    from drain.model_info import network_sha256

    edited = tmp_path / "edited.inp"
    edited.write_text("[TITLE]\nsomething else\n")
    assert network_sha256(edited) != network_sha256()
