"""Tests for translating a rainfall request into SWMM input edits."""

from __future__ import annotations

import pytest

from drain import swmm_runner
from drain.paths import BASE_INP
from drain.swmm_runner import BASE_TIMESERIES_ROWS, RAIN_TIMESERIES_NAME, _build_preconfig


def shipped_options():
    """The [OPTIONS] of the network file, by name."""
    options = {}
    in_options = False
    for line in BASE_INP.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("["):
            in_options = line.strip().upper() == "[OPTIONS]"
            continue
        parts = line.split()
        if in_options and len(parts) >= 2 and not parts[0].startswith(";"):
            options[parts[0]] = parts[1]
    return options


class RecordingPreConfig:
    """Stands in for pyswmm's SimulationPreConfig and records every edit."""

    def __init__(self):
        self.updates = []

    def add_update_by_token(self, section, obj_id, index, new_val, row_num=0):
        self.updates.append((section, obj_id, index, new_val, row_num))


@pytest.fixture
def recorded(monkeypatch):
    monkeypatch.setattr(swmm_runner, "SimulationPreConfig", RecordingPreConfig)

    def build(rainfall):
        return _build_preconfig(rainfall).updates

    return build


def rain_rows(updates):
    """Map each timeseries row to the values written into it, by column."""
    rows = {}
    for section, obj_id, index, value, row in updates:
        if section == "TIMESERIES" and obj_id == RAIN_TIMESERIES_NAME:
            rows.setdefault(row, {})[index] = value
    return rows


class TestBuildPreconfig:
    def test_a_short_storm_blanks_the_rows_the_shipped_series_has_beyond_it(self, recorded):
        """Otherwise SWMM keeps raining from the original 24-hour series."""
        rows = rain_rows(recorded({"total_precip": 50, "duration_hr": 1}))
        generated = [row for row, values in rows.items() if values.get(1)]
        cleared = [row for row, values in rows.items() if values == {0: "", 1: "", 2: ""}]

        assert generated, "the storm itself should be written"
        assert max(generated) < min(cleared)
        assert sorted(generated + cleared) == list(range(BASE_TIMESERIES_ROWS))

    @pytest.mark.parametrize("duration_hr", [1, 1.5, 24])
    def test_every_storm_keeps_the_files_24_hour_end(self, recorded, duration_hr):
        """A short storm used to end the run with the rain, so flooding after
        it was never counted and its results did not compare with the 24 h
        baseline."""
        updates = recorded({"total_precip": 50, "duration_hr": duration_hr})
        assert not [u for u in updates if u[0] == "OPTIONS"]

    def test_the_files_end_is_24_hours_after_its_start(self):
        options = shipped_options()
        assert (options["START_DATE"], options["START_TIME"]) == ("01/01/2024", "00:00:00")
        assert (options["END_DATE"], options["END_TIME"]) == ("01/02/2024", "00:00:00")

    def test_a_one_hour_storm_is_dry_from_hour_one_to_the_end(self, recorded):
        rows = rain_rows(recorded({"total_precip": 50, "duration_hr": 1}))
        generated = {row: values for row, values in rows.items() if values.get(1)}
        last = generated[max(generated)]
        assert (last[1], last[2]) == ("01:00", "0.00")
        for row in range(max(generated) + 1, BASE_TIMESERIES_ROWS):
            assert rows[row] == {0: "", 1: "", 2: ""}


class TestLinkOverrideMatching:
    """A key names a conduit by its full name or by the part after the first
    dash. It used to match by endswith, so "C-88" would also have caught
    a conduit named ...-C-188."""

    class Link:
        def __init__(self, linkid):
            self.linkid = linkid
            self.flow_limit = None

    def run(self, monkeypatch, names, overrides):
        from drain import swmm_runner

        links = [self.Link(name) for name in names]
        monkeypatch.setattr(swmm_runner, "Links", lambda sim: links)
        swmm_runner._apply_link_overrides(object(), overrides)
        return {link.linkid: link.flow_limit for link in links}

    def test_the_part_after_the_first_dash_matches_every_segment(self, monkeypatch):
        limits = self.run(
            monkeypatch,
            ["C_0-C-88", "C_1-C-88", "C_2-C-188"],
            {"C-88": {"init_flow": 0.5}},
        )
        assert limits == {"C_0-C-88": 0.5, "C_1-C-88": 0.5, "C_2-C-188": None}

    def test_a_full_name_matches_only_itself(self, monkeypatch):
        limits = self.run(monkeypatch, ["C_0-C-88", "C_1-C-88"], {"C_1-C-88": {"init_flow": 1.0}})
        assert limits == {"C_0-C-88": None, "C_1-C-88": 1.0}
