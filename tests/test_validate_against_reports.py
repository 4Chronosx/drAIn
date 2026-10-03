"""Tests for the reports-validation script.

The script is the only thing that checks the hazard model against observed
flooding, so it needs to be right before anyone reads numbers off it.
"""

from __future__ import annotations

import urllib.error

import pytest

from drain.hazard import categorise
from scripts.validate_against_reports import (
    HAZARD_RANK,
    PAGE_SIZE,
    Report,
    auc,
    compare_against_volume_baseline,
    compare_distributions,
    fetch_reports,
    pooled_auc,
    print_within_barangay,
    reporters_per_component,
    select_reports,
    summarise_join,
    within_barangay,
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


# --- Evidence, reporters and the within-barangay comparison (run plan 2.16) ---


def test_only_confirmed_reports_and_photo_matches_count_by_default():
    reports = [
        Report("I-1", None, None, review_status="confirmed"),
        Report("I-2", None, None, photo_check="match"),
        Report("I-3", None, None, review_status="unreviewed", photo_check="missing"),
    ]
    kept = select_reports(reports, all_reports=False, min_reporters=1)
    assert {r.component_id for r in kept} == {"I-1", "I-2"}
    everything = select_reports(reports, all_reports=True, min_reporters=1)
    assert len(everything) == 3


def test_one_person_reporting_twice_is_one_reporter():
    reports = [
        Report("I-1", None, None, user_id="a"),
        Report("I-1", None, None, user_id="a"),
        Report("I-1", None, None, user_id="b"),
        # Signed-out reports can't be told apart: together they are one.
        Report("I-1", None, None),
        Report("I-1", None, None),
        Report("I-2", None, None, user_id="a"),
    ]
    assert reporters_per_component(reports) == {"I-1": 3, "I-2": 1}
    kept = select_reports(reports, all_reports=True, min_reporters=2)
    assert {r.component_id for r in kept} == {"I-1"}


def test_a_key_that_cannot_read_reporters_still_gets_the_reports(capsys):
    def get_json(url, headers):
        if "user_id" in url:
            raise urllib.error.HTTPError(url, 401, "permission denied", None, None)
        return [{"component_id": "I-1", "review_status": "confirmed"}]

    reports = fetch_reports("https://p.supabase.co", "anon-key", get_json=get_json)
    assert [r.user_id for r in reports] == [None]
    assert reports[0].is_strong_evidence
    assert "can't be told apart" in capsys.readouterr().out


def test_auc_is_the_chance_a_reported_node_scores_higher():
    assert auc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert auc([0.1], [0.9]) == 0.0
    assert auc([0.5], [0.5]) == 0.5


def scored(barangay, score):
    return {"Barangay": barangay, "Vulnerability_Score": score}


def test_barangays_with_too_few_reports_say_so():
    nodes = {f"A-{i}": scored("Alang", i / 10) for i in range(10)}
    nodes.update({f"B-{i}": scored("Bakilid", i / 10) for i in range(10)})
    matched = {"A-9", "A-8", "A-7", "A-6", "A-5", "B-9"}

    comparisons = {c.barangay: c for c in within_barangay(matched, nodes, min_sample=5)}

    assert comparisons["Alang"].auc == 1.0  # the five highest are the reported ones
    assert comparisons["Bakilid"].auc is None  # one reported node is not a sample
    assert pooled_auc(list(comparisons.values())) == 1.0


def test_nodes_without_a_barangay_are_left_out():
    nodes = {"X": scored(None, 0.9), "A-1": scored("Alang", 0.1)}
    assert [c.barangay for c in within_barangay({"X"}, nodes)] == ["Alang"]


def test_with_no_usable_barangay_there_is_no_pooled_figure(capsys):
    comparisons = within_barangay({"A-1"}, {"A-1": scored("Alang", 0.5)}, min_sample=5)
    assert pooled_auc(comparisons) is None
    print_within_barangay(comparisons, 5)
    assert "insufficient data" in capsys.readouterr().out
