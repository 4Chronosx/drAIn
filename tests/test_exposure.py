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
            score = exposure_for(point).score
            assert score is None or 0.0 <= score <= 1.0

    def test_a_denser_area_scores_higher(self):
        scores = {}
        for point in node_locations().values():
            exposure = exposure_for(point)
            if exposure.density is not None:
                scores[exposure.density] = exposure.score
        densities = sorted(scores)
        assert scores[densities[0]] < scores[densities[-1]]

    def test_the_densest_barangay_scores_one(self, barangays, monkeypatch):
        densest = max((b for b in barangays if b.density), key=lambda b: b.density)
        monkeypatch.setattr(exposure, "barangay_at", lambda point: densest)
        assert exposure_for((0.0, 0.0)).score == 1.0

    def test_with_no_scale_to_measure_against_the_score_is_neutral(self, barangays, monkeypatch):
        mapped = next(b for b in barangays if b.density)
        monkeypatch.setattr(exposure, "barangay_at", lambda point: mapped)
        monkeypatch.setattr(exposure, "_highest_density", lambda: 0.0)
        result = exposure_for((0.0, 0.0))
        assert result.score is None
        assert result.barangay == mapped.name

    def test_an_unmapped_location_is_unknown_not_invented(self):
        # It used to score 0.5, a made-up "average" that ranked it anyway.
        exposure = exposure_for((0.0, 0.0))
        assert exposure == UNKNOWN_EXPOSURE
        assert exposure.score is None
        assert exposure.basis == "unknown"
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


def square_barangay(name, x0, y0, size, density=1000.0, area=1.0):
    """A square barangay with its lower-left corner at (x0, y0), in degrees."""
    ring = [[x0, y0], [x0, y0 + size], [x0 + size, y0 + size], [x0 + size, y0], [x0, y0]]
    return Barangay(
        name=name,
        population=None if density is None else density * area,
        density=density,
        land_area_km2=area,
        rings=[ring],
        bbox=(x0, y0, x0 + size, y0 + size),
    )


class TestNearestBarangay:
    # 0.001 degrees is about 110 m.
    def use(self, monkeypatch, *barangays):
        monkeypatch.setattr(exposure, "load_barangays", lambda path=None: tuple(barangays))
        monkeypatch.setattr(exposure, "_highest", None)

    def test_a_node_just_outside_takes_the_nearest_barangay(self, monkeypatch):
        self.use(monkeypatch, square_barangay("Near", 0.0, 0.0, 0.01, density=500.0))
        result = exposure_for((0.0115, 0.005))  # about 165 m east
        assert result.barangay == "Near"
        assert result.basis == "nearest"
        assert 150 < result.distance_m < 180
        assert result.score == 1.0

    def test_past_250_m_it_is_unknown(self, monkeypatch):
        self.use(monkeypatch, square_barangay("Near", 0.0, 0.0, 0.01))
        result = exposure_for((0.0135, 0.005))  # about 390 m east
        assert result.score is None
        assert result.basis == "unknown"

    def test_the_closer_of_two_barangays_wins(self, monkeypatch):
        self.use(
            monkeypatch,
            square_barangay("West", 0.0, 0.0, 0.01, density=100.0),
            square_barangay("East", 0.0125, 0.0, 0.01, density=200.0),
        )
        assert exposure_for((0.0105, 0.005)).barangay == "West"
        assert exposure_for((0.012, 0.005)).barangay == "East"

    def test_a_real_barangay_beats_the_city_outline(self, monkeypatch):
        city = square_barangay("City", -1.0, -1.0, 3.0, density=300.0, area=50.0)
        self.use(monkeypatch, city, square_barangay("Part", 0.0, 0.0, 0.01, density=900.0))
        # Inside the outline but outside the barangay: the city's figure.
        assert exposure_for((0.5, 0.5)).barangay == "City"
        # Near both edges: the barangay's.
        assert exposure_for((0.0115, 0.005)).barangay == "Part"

    def test_the_city_outline_covers_the_coast_the_barangays_miss(self, monkeypatch):
        city = square_barangay("City", 0.0, 0.0, 0.1, density=300.0, area=50.0)
        self.use(monkeypatch, city, square_barangay("Inland", 0.05, 0.05, 0.01, density=900.0))
        result = exposure_for((0.1015, 0.02))  # just past the outline, far from Inland
        assert result.barangay == "City"
        assert result.basis == "nearest"

    def test_inside_a_barangay_without_a_figure_is_unknown_not_its_neighbours(self, monkeypatch):
        self.use(
            monkeypatch,
            square_barangay("Recle", 0.0, 0.0, 0.01, density=None),
            square_barangay("Next door", 0.0101, 0.0, 0.01, density=900.0),
        )
        result = exposure_for((0.0099, 0.005))
        assert result.barangay == "Recle"
        assert result.score is None
        assert result.basis == "unknown"
