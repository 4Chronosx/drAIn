"""Tests for SWMM report parsing."""

from __future__ import annotations

import pytest

from drain.paths import BASE_RPT
from drain.rpt_parser import parse_flooding_summary

SUMMARY_FIXTURE = """
  **************
  Analysis Options
  **************

  ****************
  Node Flooding Summary
  ****************

  ----------------------------------------------------------------------
                                                            Total
                         Hours    Maximum     Day of   Hour of  Flood
  Node                 Flooded  Rate (CMS)    Maximum  Maximum  Volume
  ----------------------------------------------------------------------
  I-4                    21.95      0.760          0    00:20   42.716
  I-7                     7.10      0.053          1    03:05    1.200
  J-9                     0.25      0.001          0    12:45    0.003

  ***************
  Next Section
  ***************
"""


@pytest.fixture
def summary_file(tmp_path):
    path = tmp_path / "sample.rpt"
    path.write_text(SUMMARY_FIXTURE, encoding="utf-8")
    return path


def test_parses_every_row_of_the_summary(summary_file):
    assert set(parse_flooding_summary(summary_file)) == {"I-4", "I-7", "J-9"}


def test_parses_the_columns_of_a_row(summary_file):
    node = parse_flooding_summary(summary_file)["I-4"]
    assert node.hours_flooded == 21.95
    assert node.maximum_rate_cms == 0.76
    assert node.time_of_max_days == 0.0
    assert node.total_flood_volume == 42.716


def test_keeps_only_the_minutes_of_the_time_of_max(summary_file):
    # "03:05" -> 5. The vulnerability model was trained on the minute field.
    assert parse_flooding_summary(summary_file)["I-7"].time_of_max_minutes == 5


def test_stops_at_the_next_section(summary_file):
    assert "Next" not in parse_flooding_summary(summary_file)


def test_missing_section_yields_no_nodes(tmp_path):
    path = tmp_path / "empty.rpt"
    path.write_text("  Analysis Options\n  ----------\n  nothing here\n", encoding="utf-8")
    assert parse_flooding_summary(path) == {}


def test_skips_malformed_rows(tmp_path):
    path = tmp_path / "ragged.rpt"
    good = "  I-7                     7.10      0.053          1    03:05    1.200"
    ragged = good.replace("7.10", "n/a ")
    path.write_text(SUMMARY_FIXTURE.replace(good, ragged), encoding="utf-8")
    parsed = parse_flooding_summary(path)
    assert "I-7" not in parsed
    assert {"I-4", "J-9"} <= set(parsed)


def test_parses_the_shipped_report():
    """Guards the parser against the real, fixed-width report format."""
    flooded = parse_flooding_summary(BASE_RPT)
    assert len(flooded) == 450
    assert flooded["I-4"].hours_flooded == 21.95
