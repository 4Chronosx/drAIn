"""Tests for assembling the node flooding payload."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from drain import flooding
from drain.exposure import UNKNOWN_EXPOSURE, exposure_for
from drain.flooding import NO_OVERFLOW_MINUTES, _minutes_until_overflow, build_flooding_summary
from drain.hazard import hazard_for
from drain.network import node_locations
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


class TestScores:
    def test_risk_is_hazard_times_exposure(self, baseline):
        for row in baseline["nodes_list"]:
            if row["Exposure_Score"] is None:
                assert row["Risk_Score"] is None
                continue
            assert row["Risk_Score"] == pytest.approx(
                row["Vulnerability_Score"] * row["Exposure_Score"], abs=1e-4
            )

    def test_the_hazard_columns_come_from_the_scorer(self, baseline):
        row = max(baseline["nodes_list"], key=lambda r: r["Total_Flood_Volume_10e6_ltr"])
        hazard = hazard_for(
            total_flood_volume=row["Total_Flood_Volume_10e6_ltr"],
            hours_flooded=row["Hours_Flooded"],
            maximum_rate_cms=row["Maximum_Rate_CMS"],
            event_hours=baseline["metadata"]["event_hours"],
        )
        assert row["Vulnerability_Score"] == hazard.score
        assert row["Vulnerability_Category"] == hazard.category

    def test_the_exposure_columns_come_from_the_node_location(self, baseline):
        node_id, point = next(iter(node_locations().items()))
        exposure = exposure_for(point)
        row = baseline["nodes_dict"][node_id]
        assert row["Barangay"] == exposure.barangay
        assert row["Exposure_Score"] == (
            None if exposure.score is None else round(exposure.score, 4)
        )

    def test_a_node_without_coordinates_has_unknown_exposure_and_risk(self, monkeypatch):
        monkeypatch.setattr(flooding, "node_locations", lambda: {})
        summary = build_flooding_summary(BASE_RPT, BASE_OUT)
        rows = summary["nodes_list"]
        assert all(r["Barangay"] is None for r in rows)
        assert all(r["Exposure_Score"] is None for r in rows)
        assert all(r["Exposure_Basis"] == UNKNOWN_EXPOSURE.basis for r in rows)
        assert all(r["Risk_Score"] is None for r in rows)
        assert all(r["Population_Density"] is None for r in rows)
        assert summary["metadata"]["exposure_basis_counts"]["unknown"] == len(rows)

    def test_the_metadata_counts_how_exposure_was_found(self, baseline):
        counts = baseline["metadata"]["exposure_basis_counts"]
        assert sum(counts.values()) == baseline["metadata"]["total_nodes"]
        assert counts["inside"] > counts["nearest"] + counts["unknown"]


class TestInconsistentNodes:
    def test_it_counts_nodes_the_report_floods_but_the_output_does_not(self, baseline):
        rows = baseline["nodes_list"]
        expected = sum(
            1 for r in rows if r["Hours_Flooded"] > 0 and r["Time_After_Raining_min"] is None
        )
        assert expected > 0
        assert baseline["metadata"]["inconsistent_nodes"] == expected

    def test_consistent_nodes_are_not_counted(self, baseline):
        # Every node the output shows overflowing has a time; none of those
        # may be in the count, so it cannot exceed the flooded nodes left.
        rows = baseline["nodes_list"]
        timed = sum(1 for r in rows if r["Time_After_Raining_min"] is not None)
        flooded = baseline["metadata"]["flooded_nodes"]
        assert baseline["metadata"]["inconsistent_nodes"] == flooded - timed
