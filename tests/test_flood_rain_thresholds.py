"""Tests for the flood-history scripts: rain thresholds and the barangay model check."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

import pytest

from drain.rain_history import RainFeatures
from scripts.flood_rain_thresholds import (
    auc,
    exceedance,
    per_barangay,
    separation,
    split_days,
    windowed,
    youden,
)
from scripts.validate_against_flood_history import (
    barangay_rows,
    bootstrap_interval,
    partial_spearman,
    spearman,
)

START = date(2020, 1, 1)


def day(n: int) -> date:
    return START + timedelta(days=n)


def features(values: list[float]) -> dict[date, RainFeatures]:
    return {day(i): RainFeatures(v, v, v, v, None) for i, v in enumerate(values)}


class TestSeparation:
    def test_auc_counts_ties_half(self):
        assert auc([3.0, 2.0], [1.0, 2.0]) == pytest.approx(0.875)

    def test_auc_is_one_for_perfect_separation(self):
        assert auc([5.0, 6.0], [1.0, 2.0, 3.0]) == 1.0

    def test_youden_picks_the_cleanest_cut(self):
        threshold, hit, false_alarm = youden([10.0, 12.0, 3.0], [1.0, 2.0, 4.0, 5.0, 11.0])
        assert (threshold, hit, false_alarm) == (10.0, pytest.approx(2 / 3), 0.2)

    def test_youden_ties_go_to_the_higher_threshold(self):
        # J is 0.5 at both 5 and 6.
        assert youden([5.0, 6.0], [1.0, 5.5])[0] == 6.0

    def test_separation_is_none_without_both_groups(self):
        assert separation([], [1.0]) is None
        assert separation([1.0], []) is None


class TestWindows:
    def test_window_zero_leaves_values_alone(self):
        values = {day(0): 1.0, day(1): 5.0}
        assert windowed(values, 0) is values

    def test_window_takes_the_wettest_neighbour(self):
        values = {day(0): 1.0, day(1): 5.0, day(2): 0.0, day(3): 0.0}
        assert windowed(values, 1) == {day(0): 5.0, day(1): 5.0, day(2): 5.0, day(3): 0.0}

    def test_days_near_a_flood_are_left_out_of_the_others(self):
        values = {day(i): float(i) for i in range(6)}
        flood, other = split_days(values, [day(2)], window=1)
        assert flood == [3.0]
        assert other == [1.0, 5.0, 5.0]  # days 0, 4, 5, each the max of its neighbours

    def test_flood_days_without_rain_data_are_skipped(self):
        flood, _ = split_days({day(0): 1.0}, [day(0), day(9)], window=0)
        assert flood == [1.0]


def test_exceedance_counts_flood_days_above_each_level():
    rows = exceedance(features([5.0, 25.0, 35.0, 0.0]), [day(2), day(3)], levels=(20, 50))
    assert rows == [
        {"mm_in_3h": 20, "days": 2, "flood_days": 1, "share": 0.5},
        {"mm_in_3h": 50, "days": 0, "flood_days": 0, "share": None},
    ]


def test_barangays_below_the_minimum_get_insufficient_data():
    rain = features([float(i % 7) for i in range(60)])
    days = {day(i): {"Tipolo"} for i in range(0, 40, 4)}
    days[day(1)] = {"Tipolo", "Looc"}
    result = per_barangay(rain, days, min_days=8)
    assert result["Tipolo"]["flood_days"] == 11
    assert result["Tipolo"]["windows"][0]["max_3h"].flood_days == 11
    assert result["Looc"] == {"flood_days": 1, "windows": None}


class TestModelCheck:
    def test_spearman_on_a_monotonic_toy(self):
        assert spearman([1, 2, 3, 4], [10, 20, 25, 90]) == pytest.approx(1.0)
        assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)

    def test_partial_correlation_removes_a_shared_driver(self):
        # x and y both follow z; once z is removed, what is left is unrelated.
        z = [1, 2, 3, 4, 5, 6, 7, 8]
        x = [1, 3, 2, 4, 6, 5, 8, 7]
        y = [2, 1, 3, 5, 4, 6, 7, 8]
        assert spearman(x, y) > 0.8
        assert abs(partial_spearman(x, y, z)) < abs(spearman(x, y))

    def test_partial_correlation_keeps_a_real_link(self):
        z = [1, 1, 2, 2, 3, 3, 4, 4]
        x = [1, 2, 3, 4, 5, 6, 7, 8]
        assert partial_spearman(x, x, z) == pytest.approx(1.0)

    def test_bootstrap_is_reproducible_with_a_fixed_seed(self):
        x, y = [1, 2, 3, 4, 5, 6], [2, 1, 4, 3, 6, 5]
        first = bootstrap_interval(spearman, [x, y], draws=200, seed=1)
        assert first == bootstrap_interval(spearman, [x, y], draws=200, seed=1)
        low, high = first
        assert -1.0 <= low <= spearman(x, y) <= high <= 1.0

    def test_barangay_rows(self):
        nodes = {
            "a": {
                "Barangay": "Tipolo",
                "Vulnerability_Score": 0.6,
                "Vulnerability_Category": "High",
            },
            "b": {
                "Barangay": "Tipolo",
                "Vulnerability_Score": 0.0,
                "Vulnerability_Category": "No hazard",
            },
            "c": {
                "Barangay": "Umapad",
                "Vulnerability_Score": 0.3,
                "Vulnerability_Category": "Medium",
            },
            "d": {
                "Barangay": "Mandaue City",
                "Vulnerability_Score": 0.9,
                "Vulnerability_Category": "High",
            },
            "e": {"Barangay": None, "Vulnerability_Score": 0.9, "Vulnerability_Category": "High"},
        }
        rows, left_out = barangay_rows(
            nodes,
            Counter({"Tipolo": 4}),
            {"Tipolo": 100.0, "Umapad": None},
            names=("Looc", "Tipolo", "Umapad"),
            min_nodes=2,
        )
        assert [
            (r.barangay, r.flood_days, r.nodes, r.mean_hazard, r.share_medium_high) for r in rows
        ] == [("Tipolo", 4, 2, 0.3, 0.5)]
        assert left_out == {"Looc": 0, "Umapad": 1}
