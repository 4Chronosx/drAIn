"""Small geodesy helpers, written out rather than pulled in.

The backend needs two things: to turn the SWMM network's projected
coordinates into longitude/latitude, and to test whether a node falls inside
a barangay polygon. Both are short and well-defined, and neither justifies
adding pyproj and shapely -- with their compiled dependencies -- to a
deployment that installs from requirements.txt on every push.
"""

from __future__ import annotations

import math

# WGS 84 ellipsoid.
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_E_PRIME_SQ = _E2 / (1 - _E2)
_K0 = 0.9996
_FALSE_EASTING = 500000.0

#: The Mandaue network is projected in UTM zone 51N.
MANDAUE_UTM_ZONE = 51


def utm_zone_central_meridian(zone: int) -> float:
    """Longitude, in degrees, that a UTM zone is measured from."""
    return zone * 6 - 183


def utm_to_lonlat(
    easting: float, northing: float, zone: int = MANDAUE_UTM_ZONE
) -> tuple[float, float]:
    """Convert northern-hemisphere UTM coordinates to (longitude, latitude).

    The standard inverse transverse-Mercator series, accurate to well under
    a metre over a single zone -- far finer than the question being asked of
    it, which is which barangay a drain sits in.
    """
    x = easting - _FALSE_EASTING
    meridional_arc = northing / _K0

    mu = meridional_arc / (_A * (1 - _E2 / 4 - 3 * _E2**2 / 64 - 5 * _E2**3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))

    footprint_lat = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
        + (151 * e1**3 / 96) * math.sin(6 * mu)
        + (1097 * e1**4 / 512) * math.sin(8 * mu)
    )

    sin_phi = math.sin(footprint_lat)
    cos_phi = math.cos(footprint_lat)
    tan_phi = math.tan(footprint_lat)

    c1 = _E_PRIME_SQ * cos_phi**2
    t1 = tan_phi**2
    n1 = _A / math.sqrt(1 - _E2 * sin_phi**2)
    r1 = _A * (1 - _E2) / (1 - _E2 * sin_phi**2) ** 1.5
    d = x / (n1 * _K0)

    latitude = footprint_lat - (n1 * tan_phi / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * _E_PRIME_SQ) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * _E_PRIME_SQ - 3 * c1**2) * d**6 / 720
    )
    longitude = (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * _E_PRIME_SQ + 24 * t1**2) * d**5 / 120
    ) / cos_phi

    return (
        utm_zone_central_meridian(zone) + math.degrees(longitude),
        math.degrees(latitude),
    )


Point = tuple[float, float]
#: GeoJSON positions may carry a third ordinate (elevation), which KML
#: exports routinely do. Only the first two are used.
Ring = list[list[float]]


def point_in_ring(point: Point, ring: Ring) -> bool:
    """Ray-casting test for a point inside a single closed ring."""
    x, y = point[0], point[1]
    inside = False
    for i in range(len(ring)):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i - 1][0], ring[i - 1][1]
        # Does the edge straddle the horizontal line through the point, and
        # does it cross to the left of it?
        if (y1 > y) != (y2 > y):
            crossing_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < crossing_x:
                inside = not inside
    return inside


def point_in_polygon(point: Point, rings: list[Ring]) -> bool:
    """Test a GeoJSON polygon: inside the outer ring, outside every hole."""
    if not rings or not point_in_ring(point, rings[0]):
        return False
    return not any(point_in_ring(point, hole) for hole in rings[1:])


def bounding_box(rings: list[Ring]) -> tuple[float, float, float, float]:
    """(min_x, min_y, max_x, max_y) of a polygon's outer ring."""
    xs = [position[0] for position in rings[0]]
    ys = [position[1] for position in rings[0]]
    return min(xs), min(ys), max(xs), max(ys)
