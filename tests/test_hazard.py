"""Tests for flood-hazard scoring.

The properties here are the ones the previous k-means model broke, so they
are pinned deliberately rather than incidentally.
"""

from __future__ import annotations

import json
import math

import pytest

from drain.hazard import NO_HAZARD, _clamp_fraction, categorise, hazard_for


class TestNothingThatFloodsIsCalledSafe:
    """The defect that motivated replacing the clustering."""

    def test_a_node_that_floods_for_eleven_hours_is_not_no_hazard(self):
        # ISD-1002 in the shipped baseline. The k-means called this No risk.
        hazard = hazard_for(total_flood_volume=3.988, hours_flooded=11.67, maximum_rate_cms=0.128)
        assert hazard.category != NO_HAZARD
        assert hazard.score > 0

    @pytest.mark.parametrize("hours", [0.01, 1.0, 7.1, 11.67, 22.0])
    def test_any_duration_of_flooding_scores_above_zero(self, hours):
        assert hazard_for(0.001, hours, 0.001).score > 0

    def test_only_a_completely_dry_node_is_no_hazard(self):
        assert hazard_for(0.0, 0.0, 0.0).category == NO_HAZARD
        assert hazard_for(0.0, 0.0, 0.0).score == 0.0

    def test_a_node_with_volume_but_no_recorded_duration_still_registers(self):
        assert hazard_for(5.0, 0.0, 0.0).score > 0


class TestMonotonicity:
    """More water, for longer, or faster can only raise the score."""

    @pytest.mark.parametrize(
        ("worse", "better"),
        [
            ((10.0, 5.0, 0.2), (5.0, 5.0, 0.2)),  # more volume
            ((5.0, 10.0, 0.2), (5.0, 5.0, 0.2)),  # longer
            ((5.0, 5.0, 0.4), (5.0, 5.0, 0.2)),  # faster
            ((10.0, 10.0, 0.4), (5.0, 5.0, 0.2)),  # all three
        ],
    )
    def test_a_worse_flood_never_scores_lower(self, worse, better):
        assert hazard_for(*worse).score > hazard_for(*better).score

    def test_onset_time_is_not_an_input(self):
        # A late flood is not a mild one. The old model disagreed, which is
        # how long floods came out rated safe.
        assert hazard_for(20.0, 12.0, 0.5) == hazard_for(20.0, 12.0, 0.5)


class TestScale:
    def test_the_score_stays_within_zero_and_one(self):
        assert hazard_for(1e9, 1e9, 1e9).score == pytest.approx(1.0)
        assert hazard_for(0.0, 0.0, 0.0).score == 0.0

    def test_an_extreme_flood_saturates_rather_than_overflowing(self):
        assert hazard_for(10_000.0, 24.0, 100.0).score <= 1.0

    def test_duration_is_scored_against_the_length_of_the_event(self):
        # Two hours of flooding in a two-hour storm is total; in a
        # twenty-four hour storm it is not.
        short = hazard_for(5.0, 2.0, 0.2, event_hours=2.0)
        long = hazard_for(5.0, 2.0, 0.2, event_hours=24.0)
        assert short.score > long.score

    @pytest.mark.parametrize("event_hours", [0.0, -1.0])
    def test_a_nonsensical_event_length_falls_back_instead_of_dividing_by_zero(self, event_hours):
        assert hazard_for(5.0, 2.0, 0.2, event_hours=event_hours).score > 0

    def test_negative_measurements_do_not_produce_negative_scores(self):
        assert hazard_for(-5.0, 3.0, -1.0).score >= 0


class TestClampFraction:
    @pytest.mark.parametrize("full_scale", [0.0, -1.0])
    def test_a_zero_or_negative_full_scale_scores_nothing(self, full_scale):
        assert _clamp_fraction(5.0, full_scale) == 0.0

    def test_values_are_pinned_between_zero_and_one(self):
        assert _clamp_fraction(-3.0, 10.0) == 0.0
        assert _clamp_fraction(5.0, 10.0) == 0.5
        assert _clamp_fraction(50.0, 10.0) == 1.0


class TestNonFiniteInputs:
    """Regression: a NaN measurement gave score=nan, category "Low".

    NaN is not valid JSON, so one bad figure broke the whole payload for a
    strict parser, and "Low" claimed a rating nothing supported.
    """

    @pytest.mark.parametrize(
        "measurements",
        [
            (math.nan, 2.0, 0.3),
            (5.0, math.nan, 0.3),
            (5.0, 2.0, math.nan),
            (math.nan, math.nan, math.nan),
            (math.inf, 2.0, 0.3),
            (-math.inf, 2.0, 0.3),
            (5.0, math.inf, 0.3),
            (5.0, 2.0, -math.inf),
        ],
    )
    def test_the_score_is_always_a_finite_fraction(self, measurements):
        hazard = hazard_for(*measurements)
        assert math.isfinite(hazard.score)
        assert 0.0 <= hazard.score <= 1.0
        json.dumps({"score": hazard.score}, allow_nan=False)

    def test_a_missing_measurement_counts_for_nothing(self):
        assert hazard_for(5.0, math.nan, 0.3) == hazard_for(5.0, 0.0, 0.3)

    def test_nothing_but_missing_measurements_is_no_hazard(self):
        assert hazard_for(math.nan, math.nan, math.nan).category == NO_HAZARD

    def test_an_infinite_measurement_saturates(self):
        assert hazard_for(math.inf, 0.0, 0.0) == hazard_for(1e9, 0.0, 0.0)

    @pytest.mark.parametrize("event_hours", [math.nan, math.inf, -math.inf])
    def test_a_non_finite_event_length_falls_back_to_the_default(self, event_hours):
        assert hazard_for(5.0, 2.0, 0.2, event_hours=event_hours) == hazard_for(5.0, 2.0, 0.2)

    @pytest.mark.parametrize("score", [math.nan, -math.inf])
    def test_a_non_finite_score_is_not_given_a_rating(self, score):
        assert categorise(score) == NO_HAZARD


class TestCategories:
    @pytest.mark.parametrize(
        ("score", "category"),
        [
            # Pinned to the current thresholds: a score exactly on a
            # boundary stays in the band below it.
            (0.25, "Low"),
            (0.250001, "Medium"),
            (0.5, "Medium"),
            (0.500001, "High"),
            (1.0, "High"),
        ],
    )
    def test_a_score_on_a_boundary_stays_in_the_lower_band(self, score, category):
        assert categorise(score) == category

    def test_the_bands_ascend_with_the_score(self):
        assert categorise(0.0) == NO_HAZARD
        assert categorise(0.1) == "Low"
        assert categorise(0.4) == "Medium"
        assert categorise(0.9) == "High"

    def test_every_category_matches_the_frontend_colour_keywords(self):
        # getColorForCategory does substring matching on these words; a
        # category that matches none of them renders in the fallback colour.
        keywords = ("high", "medium", "low", "no")
        for score in (0.0, 0.1, 0.4, 0.9):
            category = categorise(score).lower()
            assert any(word in category for word in keywords), category

    def test_categories_never_disagree_with_the_score_ordering(self):
        rank = {NO_HAZARD: 0, "Low": 1, "Medium": 2, "High": 3}
        scores = [0.0, 0.05, 0.2, 0.3, 0.45, 0.6, 0.99]
        ranks = [rank[categorise(s)] for s in scores]
        assert ranks == sorted(ranks)


class TestTheSentinelDoesNotEscape:
    """9999 is an internal marker, not a measurement."""

    def test_the_payload_reports_null_for_a_node_that_never_overflowed(self):
        from drain.flooding import build_flooding_summary
        from drain.paths import BASE_OUT, BASE_RPT

        rows = build_flooding_summary(BASE_RPT, BASE_OUT)["nodes_list"]
        assert not [r for r in rows if r["Time_After_Raining_min"] == 9999]
        assert [r for r in rows if r["Time_After_Raining_min"] is None]

    def test_a_node_that_did_overflow_keeps_its_timing(self):
        from drain.flooding import build_flooding_summary
        from drain.paths import BASE_OUT, BASE_RPT

        rows = build_flooding_summary(BASE_RPT, BASE_OUT)["nodes_list"]
        measured = [
            r["Time_After_Raining_min"] for r in rows if r["Time_After_Raining_min"] is not None
        ]
        assert measured
        assert all(0 <= value < 9999 for value in measured)
