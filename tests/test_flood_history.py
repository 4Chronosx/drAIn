"""Tests for the Groundsource flood records."""

from __future__ import annotations

from datetime import date

import pytest

from drain.flood_history import (
    FloodRecord,
    barangay_names,
    flood_days,
    load_events,
    merge_repeats,
    normalise_name,
)

NAMES = ("Pakna-an", "Tipolo", "Umapad")


def write_csv(tmp_path, rows):
    path = tmp_path / "events.csv"
    lines = ["start_date,end_date,area_km2,barangay_of_centroid", *rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def record(day: int, barangay: str = "Tipolo", end_day: int | None = None) -> FloodRecord:
    return FloodRecord(date(2020, 1, day), date(2020, 1, end_day or day), 0.5, barangay)


class TestLoad:
    def test_keeps_barangay_sized_records_only(self, tmp_path):
        path = write_csv(
            tmp_path,
            [
                "2020-01-01,2020-01-01,0.5,Tipolo",
                "2020-01-02,2020-01-02,30.2,Tipolo",
                "2020-01-03,2020-01-03,9.99,Umapad",
                "2020-01-04,2020-01-04,0.05,(none)",
            ],
        )
        records = load_events(path, names=NAMES)
        assert [(r.start.day, r.barangay) for r in records] == [(1, "Tipolo"), (3, "Umapad")]

    def test_a_smaller_cut_keeps_fewer(self, tmp_path):
        path = write_csv(
            tmp_path, ["2020-01-01,2020-01-01,0.5,Tipolo", "2020-01-03,2020-01-03,7.0,Umapad"]
        )
        assert len(load_events(path, max_area_km2=5.0, names=NAMES)) == 1

    def test_names_are_normalised(self, tmp_path):
        path = write_csv(tmp_path, ["2020-01-01,2020-01-01,0.5,Paknaan"])
        assert load_events(path, names=NAMES)[0].barangay == "Pakna-an"

    def test_an_unknown_name_is_an_error(self, tmp_path):
        path = write_csv(tmp_path, ["2020-01-01,2020-01-01,0.5,Atlantis"])
        with pytest.raises(ValueError, match="Atlantis"):
            load_events(path, names=NAMES)

    def test_normalise_ignores_case_and_punctuation(self):
        assert normalise_name(" pakna an", NAMES) == "Pakna-an"

    def test_the_shipped_names_are_the_28_mapped_areas(self):
        # Mandaue's 27 barangays plus "Recle", a mapped area with no population figure.
        names = barangay_names()
        assert len(names) == 28
        assert "Mandaue City" not in names

    def test_the_shipped_records_load_with_known_names(self):
        # 299 records under 10 km², 3 of them in no barangay polygon.
        assert len(load_events()) == 296


class TestMerge:
    def test_records_within_three_days_merge(self):
        events = merge_repeats([record(1), record(3), record(6)])
        assert len(events) == 1
        assert (events[0].start, events[0].end, events[0].records) == (
            date(2020, 1, 1),
            date(2020, 1, 6),
            3,
        )

    def test_the_window_counts_from_the_end_of_a_multi_day_record(self):
        events = merge_repeats([record(1, end_day=5), record(8)])
        assert len(events) == 1

    def test_four_to_fourteen_days_apart_is_flagged_not_merged(self):
        events = merge_repeats([record(1), record(5), record(30)])
        assert [(e.start.day, e.possible_follow_up) for e in events] == [
            (1, False),
            (5, True),
            (30, False),
        ]

    def test_barangays_merge_separately(self):
        events = merge_repeats([record(1, "Tipolo"), record(2, "Umapad")])
        assert {e.barangay for e in events} == {"Tipolo", "Umapad"}


def test_flood_days_count_each_event_on_its_first_day():
    events = merge_repeats([record(1, "Tipolo"), record(2, "Tipolo"), record(1, "Umapad")])
    assert flood_days(events) == {date(2020, 1, 1): {"Tipolo", "Umapad"}}
