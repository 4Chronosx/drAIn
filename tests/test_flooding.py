"""Tests for assembling the node flooding payload."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from drain import flooding
from drain.flooding import NO_OVERFLOW_MINUTES, _minutes_until_overflow, build_flooding_summary
from drain.paths import BASE_OUT, BASE_RPT


@pytest.fixture(scope="module")
def baseline():
    return build_flooding_summary(BASE_RPT, BASE_OUT)


def series_of(losses):
    """A stand-in for pyswmm's NodeSeries with one node's overflow series."""
    return {"N": SimpleNamespace(flooding_losses=losses)}


class TestMinutesUntilOverflow:
    def test_an_empty_series_is_no_data_not_zero_minutes(self):
        """Regression: an empty series reported the node overflowing at 0.0
        minutes, a measurement nobody made."""
        assert _minutes_until_overflow(series_of({}), "N") == NO_OVERFLOW_MINUTES

    def test_the_first_positive_rate_sets_the_time(self):
        start = datetime(2024, 1, 1, 0, 0)
        losses = {
            start: 0.0,
            datetime(2024, 1, 1, 0, 5): 0.0,
            datetime(2024, 1, 1, 0, 15): 0.2,
        }
        assert _minutes_until_overflow(series_of(losses), "N") == 15.0

    def test_a_series_that_never_rises_is_the_sentinel(self):
        losses = {datetime(2024, 1, 1, 0, 0): 0.0, datetime(2024, 1, 1, 0, 5): 0.0}
        assert _minutes_until_overflow(series_of(losses), "N") == NO_OVERFLOW_MINUTES

    def test_an_empty_series_is_served_as_null(self, monkeypatch):
        class EmptySeries:
            def __init__(self, out):
                pass

            def __getitem__(self, node_id):
                return SimpleNamespace(flooding_losses={})

        monkeypatch.setattr(flooding, "NodeSeries", EmptySeries)
        rows = build_flooding_summary(BASE_RPT, BASE_OUT)["nodes_list"]
        flooded = [r for r in rows if r["Hours_Flooded"] > 0]
        assert flooded
        assert all(r["Time_After_Raining_min"] is None for r in flooded)
