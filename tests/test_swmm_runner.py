"""Tests for translating a rainfall request into SWMM input edits."""

from __future__ import annotations

import pytest

from drain import swmm_runner
from drain.swmm_runner import (
    BASE_TIMESERIES_ROWS,
    RAIN_TIMESERIES_NAME,
    SIMULATION_START_DATE,
    _build_preconfig,
    _end_time_tokens,
)


class TestEndTime:
    @pytest.mark.parametrize(
        ("duration_hr", "expected"),
        [
            (1.0, "01:00:00"),
            (1.5, "01:30:00"),
            (0.25, "00:15:00"),
            # Regression: rounding the minutes on their own turned a duration
            # just under a whole hour into "00:60:00" and "01:60:00".
            (0.999, "01:00:00"),
            (1.999, "02:00:00"),
            (24.0, "24:00:00"),
        ],
    )
    def test_the_end_time_is_a_valid_clock_time(self, duration_hr, expected):
        assert _end_time_tokens(duration_hr) == (expected, SIMULATION_START_DATE)

    def test_minutes_never_reach_sixty(self):
        for step in range(1, 24000):
            duration_hr = step / 1000
            end_time, _ = _end_time_tokens(duration_hr)
            hours, minutes, seconds = (int(part) for part in end_time.split(":"))
            assert 0 <= minutes < 60, (duration_hr, end_time)
            assert seconds == 0
            assert hours * 60 + minutes == round(duration_hr * 60)


class RecordingPreConfig:
    """Stands in for pyswmm's SimulationPreConfig and records every edit."""

    def __init__(self):
        self.updates = []

    def add_update_by_token(self, section, obj_id, index, new_val, row_num=0):
        self.updates.append((section, obj_id, index, new_val, row_num))


@pytest.fixture
def recorded(monkeypatch):
    monkeypatch.setattr(swmm_runner, "SimulationPreConfig", RecordingPreConfig)

    def build(rainfall):
        return _build_preconfig(rainfall).updates

    return build


def rain_rows(updates):
    """Map each timeseries row to the values written into it, by column."""
    rows = {}
    for section, obj_id, index, value, row in updates:
        if section == "TIMESERIES" and obj_id == RAIN_TIMESERIES_NAME:
            rows.setdefault(row, {})[index] = value
    return rows


class TestBuildPreconfig:
    def test_a_short_storm_blanks_the_rows_the_shipped_series_has_beyond_it(self, recorded):
        """Otherwise SWMM keeps raining from the original 24-hour series."""
        rows = rain_rows(recorded({"total_precip": 50, "duration_hr": 1}))
        generated = [row for row, values in rows.items() if values.get(1)]
        cleared = [row for row, values in rows.items() if values == {0: "", 1: "", 2: ""}]

        assert generated, "the storm itself should be written"
        assert max(generated) < min(cleared)
        assert sorted(generated + cleared) == list(range(BASE_TIMESERIES_ROWS))

    def test_a_short_storm_sets_the_end_time(self, recorded):
        updates = recorded({"total_precip": 50, "duration_hr": 1.5})
        assert ("OPTIONS", "END_TIME", 1, "01:30:00", 0) in updates
        assert ("OPTIONS", "END_DATE", 1, SIMULATION_START_DATE, 0) in updates

    def test_a_full_day_storm_keeps_the_shipped_end_time(self, recorded):
        updates = recorded({"total_precip": 50, "duration_hr": 24})
        assert not [u for u in updates if u[0] == "OPTIONS"]
