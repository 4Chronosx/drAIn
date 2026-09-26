"""Tests for the reports-validation script.

The script is the only thing that checks the hazard model against observed
flooding, so it needs to be right before anyone reads numbers off it.
"""

from __future__ import annotations

import pytest

from drain.hazard import categorise
from scripts.validate_against_reports import (
    HAZARD_RANK,
    PAGE_SIZE,
    Report,
    compare_against_volume_baseline,
    compare_distributions,
    fetch_reports,
    summarise_join,
)


def node(category: str, volume: float) -> dict:
    return {"Vulnerability_Category": category, "Total_Flood_Volume_10e6_ltr": volume}


@pytest.fixture
def nodes():
    return {
        "I-1": node("High", 100.0),
        "I-2": node("Medium", 10.0),
        "I-3": node("No hazard", 0.0),
        "I-4": node("No hazard", 0.0),
    }


def test_every_category_the_model_emits_has_a_rank():
    assert set(HAZARD_RANK) == {"No hazard", "Low", "Medium", "High"}


def test_the_ranks_cover_what_the_scorer_actually_says():
    """Regression: the ranks said "No risk", the scorer says "No hazard"."""
    emitted = {categorise(score / 100) for score in range(0, 101)}
    assert emitted <= set(HAZARD_RANK)
    assert HAZARD_RANK["High"] > HAZARD_RANK["Medium"] > HAZARD_RANK["Low"] > 0


def test_reports_are_read_a_page_at_a_time_without_rejected_ones():
    urls = []

    def get_json(url, headers):
        urls.append(url)
        size = PAGE_SIZE if len(urls) == 1 else 5
        return [{"component_id": "I-1", "category": "inlets", "created_at": None}] * size

    reports = fetch_reports("https://p.supabase.co", "key", get_json=get_json)

    assert len(reports) == PAGE_SIZE + 5
    assert len(urls) == 2
    assert all("review_status=neq.rejected" in url for url in urls)
    assert "offset=1000" in urls[1]


def test_join_matches_components_that_exist_in_the_model(nodes, capsys):
    reports = [Report("I-1", "flooding", None), Report("I-2", "flooding", None)]
    assert summarise_join(reports, nodes) == {"I-1", "I-2"}


def test_join_reports_components_missing_from_the_model(nodes, capsys):
    # Reports pinned to a pipe will not match, since the model scores nodes.
    matched = summarise_join([Report("C-99", "flooding", None)], nodes)
    assert matched == set()
    assert "not in the model:           1" in capsys.readouterr().out


def test_repeated_reports_on_one_component_count_once(nodes):
    reports = [Report("I-1", "flooding", None) for _ in range(5)]
    assert summarise_join(reports, nodes) == {"I-1"}


def test_it_says_so_when_the_model_separates_reported_components(nodes, capsys):
    compare_distributions({"I-1"}, nodes)
    assert "the model separates them" in capsys.readouterr().out


def test_it_says_so_when_the_model_fails_to_separate(nodes, capsys):
    # People report the components the model calls safe.
    compare_distributions({"I-3", "I-4"}, nodes)
    assert "does NOT rank reported components higher" in capsys.readouterr().out


def test_it_handles_no_matches_without_dividing_by_zero(nodes, capsys):
    compare_distributions(set(), nodes)
    assert "nothing to compare" in capsys.readouterr().out


def parse_top_n_table(output: str) -> dict[int, tuple[int, int]]:
    """Read the N / model / volume-only table back out of the output."""
    rows = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            n, model, volume = (int(part) for part in parts)
            rows[n] = (model, volume)
    return rows


def test_the_baseline_comparison_counts_hits_in_both_rankings(nodes, capsys):
    compare_against_volume_baseline({"I-1"}, nodes)
    table = parse_top_n_table(capsys.readouterr().out)
    # The one reported component is the worst flooder, so both rankings find
    # it immediately -- which is exactly the "model adds nothing" signal.
    assert table[10] == (1, 1)


def test_the_baseline_comparison_can_show_the_model_winning(nodes, capsys):
    # A reported component the model rates High but that floods least, so
    # only the model surfaces it early.
    nodes["I-5"] = node("High", 0.1)
    compare_against_volume_baseline({"I-5"}, nodes)
    table = parse_top_n_table(capsys.readouterr().out)
    assert table[10] == (1, 1)  # both find it within ten of five nodes
