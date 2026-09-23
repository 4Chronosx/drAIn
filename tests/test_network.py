"""Tests for reading node geometry out of the SWMM input file."""

from __future__ import annotations

from drain.network import node_locations


def test_every_simulated_node_has_a_location():
    # The COORDINATES section should cover the same nodes the output reports.
    assert len(node_locations()) == 1413


def test_locations_are_longitude_latitude_around_mandaue():
    for longitude, latitude in list(node_locations().values())[:200]:
        assert 123.7 < longitude < 124.2
        assert 10.2 < latitude < 10.5


def test_it_is_cached_across_calls():
    assert node_locations() is node_locations()


def test_a_known_node_matches_the_shipped_geojson(tmp_path):
    # ISD-1 is listed in the frontend's storm_drains.geojson at these
    # coordinates, produced by a different toolchain.
    longitude, latitude = node_locations()["ISD-1"]
    assert abs(longitude - 123.92320028888473) < 1e-5
    assert abs(latitude - 10.314543963557362) < 1e-5


def test_comment_and_header_rows_are_skipped():
    for node_id in node_locations():
        assert not node_id.startswith(";")
        assert not node_id.startswith("[")
