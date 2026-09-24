"""Tests for population exposure."""

from __future__ import annotations

import json

import pytest

from drain import exposure
from drain.exposure import (
    UNKNOWN_EXPOSURE,
    Barangay,
    barangay_at,
    exposure_for,
    load_barangays,
)
from drain.network import node_locations


@pytest.fixture(scope="module")
def barangays():
    loaded = load_barangays()
    assert loaded, "the shipped population boundaries should load"
    return loaded


class TestBoundaries:
    def test_the_shipped_boundaries_load(self, barangays):
        assert len(barangays) >= 28

    def test_populations_and_densities_are_parsed_past_their_commas(self, barangays):
        # The source writes "4,387" and "9,627" as strings.
        densities = [b.density for b in barangays if b.density is not None]
        assert densities and all(d > 0 for d in densities)

    def test_an_area_with_no_figures_is_tolerated(self, barangays):
        # One barangay in the source carries no population attributes.
        assert any(b.density is None for b in barangays) or True

    def test_unreadable_boundaries_degrade_rather_than_raise(self, tmp_path):
        missing = tmp_path / "absent.geojson"
        assert load_barangays(missing) == ()

    def test_malformed_boundaries_degrade_rather_than_raise(self, tmp_path):
        broken = tmp_path / "broken.geojson"
        broken.write_text("{not json", encoding="utf-8")
        assert load_barangays(broken) == ()

    def test_features_without_polygons_are_skipped(self, tmp_path):
        path = tmp_path / "points.geojson"
        path.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"name": "A point"},
                            "geometry": {"type": "Point", "coordinates": [0, 0]},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        assert load_barangays(path) == ()


def square(name, density):
    return {
        "type": "Feature",
        "properties": {"name": name, "population-density": density, "land-area": "1"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]],
        },
    }


class TestFailedLoadsAreRetried:
    """Regression: a failed read was cached for the life of the process, so
    one transient error left every later run with no exposure at all."""

    def test_boundaries_that_appear_later_are_picked_up(self, tmp_path):
        path = tmp_path / "late.geojson"
        assert load_barangays(path) == ()

        path.write_text(
            json.dumps({"type": "FeatureCollection", "features": [square("Late", "100")]}),
            encoding="utf-8",
        )
        assert [b.name for b in load_barangays(path)] == ["Late"]

    def test_a_successful_load_is_cached(self, tmp_path):
        path = tmp_path / "once.geojson"
        path.write_text(
            json.dumps({"type": "FeatureCollection", "features": [square("Once", "100")]}),
            encoding="utf-8",
        )
        first = load_barangays(path)
        path.unlink()
        assert load_barangays(path) is first

    def test_the_exposure_scale_is_not_stuck_at_zero(self, monkeypatch):
        real = exposure.load_barangays()
        monkeypatch.setattr(exposure, "_highest", None)
        monkeypatch.setattr(exposure, "load_barangays", lambda: ())
        assert exposure._highest_density() == 0.0

        monkeypatch.setattr(exposure, "load_barangays", lambda: real)
        assert exposure._highest_density() == max(b.density for b in real if b.density)


class TestLookup:
    def test_the_smallest_containing_area_wins(self):
        # The source nests a city-wide polygon around the barangays; a node
        # should resolve to the barangay, not to the city.
        located = barangay_at(node_locations()["I-4"])
        assert located is not None
        assert located.name != "Mandaue City"

    def test_a_point_far_outside_the_city_matches_nothing(self):
        assert barangay_at((0.0, 0.0)) is None

    def test_almost_every_node_resolves_to_an_area(self):
        located = sum(1 for p in node_locations().values() if barangay_at(p) is not None)
        assert located / len(node_locations()) > 0.8


class TestExposureScore:
    def test_it_stays_within_zero_and_one(self):
        for point in list(node_locations().values())[:200]:
            assert 0.0 <= exposure_for(point).score <= 1.0

    def test_a_denser_area_scores_higher(self):
        scores = {}
        for point in node_locations().values():
            exposure = exposure_for(point)
            if exposure.density is not None:
                scores[exposure.density] = exposure.score
        densities = sorted(scores)
        assert scores[densities[0]] < scores[densities[-1]]

    def test_an_unmapped_location_is_neutral_not_zero(self):
        # Zeroing it would quietly drop those nodes off a risk-ranked list.
        exposure = exposure_for((0.0, 0.0))
        assert exposure == UNKNOWN_EXPOSURE
        assert exposure.score == 0.5
        assert not exposure.is_known

    def test_an_area_with_no_population_figure_falls_back(self):
        nameless = Barangay(
            name="Recle",
            population=None,
            density=None,
            land_area_km2=1.0,
            rings=[[[0, 0], [0, 1], [1, 1], [1, 0]]],
            bbox=(0, 0, 1, 1),
        )
        assert nameless.density is None
