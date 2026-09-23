"""Tests for flood-hazard scoring.

The properties here are the ones the previous k-means model broke, so they
are pinned deliberately rather than incidentally.
"""

from __future__ import annotations

import pytest

from drain.hazard import NO_HAZARD, categorise, hazard_for


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


class TestCategories:
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
