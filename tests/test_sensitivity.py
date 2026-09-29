"""Tests for the hazard-ranking sensitivity script."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from drain import hazard
from drain.hazard import hazard_for
from scripts.sensitivity import Flooding, load_baseline, report, run, scores, top


@pytest.fixture(scope="module")
def baseline():
    return load_baseline()


def test_it_scores_like_the_real_scorer(baseline):
    computed = scores(
        baseline,
        (hazard.VOLUME_WEIGHT, hazard.DURATION_WEIGHT, hazard.RATE_WEIGHT),
        hazard.VOLUME_FULL_SCALE,
        hazard.RATE_FULL_SCALE,
    )
    for i in range(0, len(baseline.node_ids), 37):
        expected = hazard_for(
            total_flood_volume=baseline.volume[i],
            hours_flooded=baseline.hours[i],
            maximum_rate_cms=baseline.rate[i],
        ).score
        assert computed[i] == pytest.approx(expected, abs=1e-6)


def test_only_flooded_nodes_are_ranked(baseline):
    assert len(baseline.node_ids) > 100
    assert np.all((baseline.volume > 0) | (baseline.hours > 0) | (baseline.rate > 0))


def test_no_perturbation_means_no_change(baseline):
    result = run(baseline, samples=3, concentration=1e9, scale_range=(1.0, 1.0))
    # Not exactly 1: float noise in the draws can break a tie or two.
    assert result.tau.min() > 0.9999
    assert np.allclose(result.jaccard, 1.0)
    assert set(result.survival.values()) == {1.0}


def test_a_seed_gives_the_same_answer(baseline):
    first = run(baseline, samples=20, seed=3)
    second = run(baseline, samples=20, seed=3)
    assert np.array_equal(first.tau, second.tau)
    assert first.survival == second.survival


def test_perturbing_moves_the_ranking_but_keeps_it_related(baseline):
    result = run(baseline, samples=40)
    assert result.tau.min() > 0
    assert result.tau.max() < 1
    assert 0 < result.jaccard.mean() <= 1
    assert len(result.survival) == 50


def test_ties_break_by_node_order():
    assert top(np.array([1.0, 2.0, 2.0, 0.5]), 2) == {1, 2}
    assert top(np.array([1.0, 1.0, 1.0]), 1) == {0}


def test_the_report_lists_the_nodes_that_drop_out():
    data = Flooding(
        node_ids=["A", "B"],
        volume=np.array([10.0, 20.0]),
        hours=np.array([1.0, 1.0]),
        rate=np.array([0.1, 0.1]),
    )
    result = run(data, samples=10, top_n=1)
    text = report(result, top_n=1, on=date(2026, 9, 29))
    assert text.startswith("# Hazard ranking sensitivity (2026-09-29)")
    assert "Kendall's τ" in text
    assert "10 draws" in text
