"""Tests for the hand-written geodesy helpers.

The reprojection is checked against the drainage GeoJSON the frontend ships,
which carries both the projected coordinates and the longitude/latitude for
the same node -- 1,369 independently produced pairs.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from drain.geo import (
    bounding_box,
    point_in_polygon,
    point_in_ring,
    utm_to_lonlat,
    utm_zone_central_meridian,
)

FRONTEND_DRAINAGE = (
    Path(__file__).resolve().parent.parent.parent / "drAIn-frontend" / "public" / "drainage"
)

UNIT_SQUARE = [[[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]]]


def metres_apart(a: tuple[float, float], b: tuple[float, float]) -> float:
    mean_lat = math.radians((a[1] + b[1]) / 2)
    dx = (a[0] - b[0]) * 111_320 * math.cos(mean_lat)
    dy = (a[1] - b[1]) * 110_540
    return math.hypot(dx, dy)


class TestUtmReprojection:
    def test_zone_51_is_measured_from_123_degrees_east(self):
        assert utm_zone_central_meridian(51) == 123

    def test_a_point_on_the_central_meridian_keeps_its_longitude(self):
        longitude, _ = utm_to_lonlat(500_000.0, 1_140_000.0, zone=51)
        assert longitude == pytest.approx(123.0, abs=1e-9)

    def test_it_places_mandaue_where_mandaue_is(self):
        longitude, latitude = utm_to_lonlat(600_940.79, 1_142_974.63)
        assert 123.8 < longitude < 124.1
        assert 10.2 < latitude < 10.5

    @pytest.mark.skipif(
        not FRONTEND_DRAINAGE.exists(),
        reason="frontend drainage GeoJSON not checked out alongside",
    )
    def test_it_agrees_with_the_shipped_geojson_to_within_a_metre(self):
        errors = []
        for name in ("inlets", "storm_drains"):
            data = json.loads((FRONTEND_DRAINAGE / f"{name}.geojson").read_text(encoding="utf-8"))
            for feature in data["features"]:
                properties = feature["properties"]
                easting = properties.get("X", properties.get("x"))
                northing = properties.get("Y", properties.get("y"))
                expected = tuple(feature["geometry"]["coordinates"])
                errors.append(metres_apart(utm_to_lonlat(easting, northing), expected))

        assert len(errors) > 1000
        assert max(errors) < 2.0


class TestPointInRing:
    def test_a_point_inside_is_inside(self):
        assert point_in_ring((0.5, 0.5), UNIT_SQUARE[0])

    @pytest.mark.parametrize("point", [(1.5, 0.5), (-0.5, 0.5), (0.5, 1.5), (0.5, -0.5)])
    def test_points_outside_are_outside(self, point):
        assert not point_in_ring(point, UNIT_SQUARE[0])

    def test_it_ignores_a_third_ordinate(self):
        # KML exports carry elevation; only the first two are meaningful.
        ring = [[0.0, 0.0, 12.0], [0.0, 1.0, 12.0], [1.0, 1.0, 12.0], [1.0, 0.0, 12.0]]
        assert point_in_ring((0.5, 0.5), ring)


class TestPointInPolygon:
    def test_a_point_in_the_outer_ring_is_inside(self):
        assert point_in_polygon((0.5, 0.5), UNIT_SQUARE)

    def test_a_point_in_a_hole_is_outside(self):
        hole = [[0.4, 0.4], [0.4, 0.6], [0.6, 0.6], [0.6, 0.4], [0.4, 0.4]]
        assert not point_in_polygon((0.5, 0.5), [UNIT_SQUARE[0], hole])

    def test_a_point_outside_a_hole_but_inside_the_ring_is_inside(self):
        hole = [[0.4, 0.4], [0.4, 0.6], [0.6, 0.6], [0.6, 0.4], [0.4, 0.4]]
        assert point_in_polygon((0.1, 0.1), [UNIT_SQUARE[0], hole])

    def test_an_empty_polygon_contains_nothing(self):
        assert not point_in_polygon((0.5, 0.5), [])


def test_bounding_box_covers_the_outer_ring():
    assert bounding_box(UNIT_SQUARE) == (0.0, 0.0, 1.0, 1.0)
