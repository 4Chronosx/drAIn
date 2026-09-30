"""Tests for synthetic design-storm generation."""

from __future__ import annotations

import pytest

from drain.rainfall import DEFAULT_INTERVAL_MIN, generate_rainfall_event


def total_depth_mm(series, interval_min=DEFAULT_INTERVAL_MIN):
    """Integrate a series of mm/hr intensities back into a total depth."""
    return sum(step.intensity_mm_hr for step in series) * (interval_min / 60)


@pytest.mark.parametrize("pattern", ["uniform", "triangular", "chicago"])
def test_series_integrates_back_to_the_requested_depth(pattern):
    series = generate_rainfall_event(120, 6, pattern=pattern)
    assert total_depth_mm(series) == pytest.approx(120, rel=1e-2)


def test_step_count_covers_the_full_duration():
    series = generate_rainfall_event(100, 24, interval_min=5)
    # 24 h at 5-minute steps, inclusive of both endpoints.
    assert len(series) == 289
    assert series[0].hours == 0.0
    assert series[-1].hours == 24.0


def test_triangular_storm_peaks_in_the_middle():
    series = generate_rainfall_event(100, 10, pattern="triangular")
    peak = max(range(len(series)), key=lambda i: series[i].intensity_mm_hr)
    assert 0.4 < peak / len(series) < 0.6


def test_chicago_storm_peaks_early():
    series = generate_rainfall_event(100, 10, pattern="chicago")
    peak = max(range(len(series)), key=lambda i: series[i].intensity_mm_hr)
    assert 0.3 < peak / len(series) < 0.5


def test_uniform_storm_has_one_intensity():
    series = generate_rainfall_event(100, 10, pattern="uniform")
    assert len({step.intensity_mm_hr for step in series}) == 1


def test_zero_depth_produces_a_dry_storm():
    series = generate_rainfall_event(0, 6)
    assert all(step.intensity_mm_hr == 0 for step in series)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"total_rain_mm": -1, "duration_hr": 1}, "total_rain_mm"),
        ({"total_rain_mm": 10, "duration_hr": 0}, "duration_hr"),
        ({"total_rain_mm": 10, "duration_hr": -3}, "duration_hr"),
    ],
)
def test_rejects_impossible_storms(kwargs, message):
    with pytest.raises(ValueError, match=message):
        generate_rainfall_event(**kwargs)


def test_rejects_unknown_pattern():
    with pytest.raises(ValueError, match="Unknown rainfall pattern"):
        generate_rainfall_event(10, 1, pattern="monsoon")
