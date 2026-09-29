"""How many people a flooded node affects.

The hazard score says how badly a node floods. On its own that ranks a drain
in an empty lot level with one outside a hospital. This module supplies the
other half: roughly how many people are around it.

The measure is deliberately coarse and should be read as such. It is the
population density of the barangay a node falls in -- not a count of who is
inside the flood footprint, which would need building footprints and a
routed inundation surface the project does not have. It is enough to
separate a node in the densest part of the city from one on its edge, and
that is all it is claimed to do.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from drain.geo import Ring, bounding_box, distance_to_rings_m, point_in_polygon
from drain.paths import POPULATION_BOUNDARIES

logger = logging.getLogger(__name__)


def _to_number(raw: object) -> float | None:
    """Parse a GeoJSON property that may be a string with thousands commas."""
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class Barangay:
    """One administrative area, with the population living in it."""

    name: str
    population: float | None
    #: People per square kilometre.
    density: float | None
    land_area_km2: float | None
    rings: list[Ring]
    bbox: tuple[float, float, float, float]

    def contains(self, point: tuple[float, float]) -> bool:
        min_x, min_y, max_x, max_y = self.bbox
        # Cheap rejection first; the ray cast is the expensive part and most
        # nodes miss most barangays.
        if not (min_x <= point[0] <= max_x and min_y <= point[1] <= max_y):
            return False
        return point_in_polygon(point, self.rings)


#: Boundaries already read, by path. Only successful reads are kept.
_loaded: dict[Path, tuple[Barangay, ...]] = {}


def load_barangays(path: Path = POPULATION_BOUNDARIES) -> tuple[Barangay, ...]:
    """Read the barangay boundaries, or return empty if they are unavailable.

    A successful read is cached. A failed one is not: caching it would leave
    every later run without exposure until the process restarted, over what
    may have been a passing error.
    """
    path = Path(path)
    cached = _loaded.get(path)
    if cached is not None:
        return cached

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read population boundaries from %s", path)
        return ()

    barangays: list[Barangay] = []
    for feature in data.get("features", []):
        geometry = feature.get("geometry") or {}
        properties = feature.get("properties") or {}

        if geometry.get("type") == "Polygon":
            polygons = [geometry["coordinates"]]
        elif geometry.get("type") == "MultiPolygon":
            polygons = geometry["coordinates"]
        else:
            continue

        for rings in polygons:
            barangays.append(
                Barangay(
                    name=str(properties.get("name", "Unknown")),
                    population=_to_number(properties.get("population-count")),
                    density=_to_number(properties.get("population-density")),
                    land_area_km2=_to_number(properties.get("land-area")),
                    rings=rings,
                    bbox=bounding_box(rings),
                )
            )

    logger.info("Loaded %d barangay boundaries from %s", len(barangays), path.name)
    loaded = tuple(barangays)
    _loaded[path] = loaded
    return loaded


def barangay_at(point: tuple[float, float]) -> Barangay | None:
    """The barangay containing a point, or ``None`` if it falls outside them all.

    The boundaries nest: a city-wide polygon encloses the individual
    barangays. The smallest match wins, so a node resolves to the barangay
    it is actually in rather than to the city as a whole.
    """
    matches = [b for b in load_barangays() if b.contains(point)]
    if not matches:
        return None
    return min(matches, key=lambda b: b.land_area_km2 or float("inf"))


#: How far outside every barangay a node may be and still take the nearest
#: one's density. Some drains sit just past the mapped polygons, along the
#: coast and the city edge. Further out than this, a density would be a guess.
NEAREST_WITHIN_M = 250.0


@dataclass(frozen=True)
class Exposure:
    """What surrounds a node."""

    barangay: str | None
    density: float | None
    #: Density rescaled to 0-1 against the densest barangay in the dataset,
    #: so it can be multiplied against a hazard score. ``None`` when the
    #: density isn't known: an unmapped location is unknown, not uninhabited
    #: and not average, so it gets no score rather than an invented one.
    score: float | None
    #: How the barangay was found: "inside" it, the "nearest" one within
    #: NEAREST_WITHIN_M, or "unknown".
    basis: str = "inside"
    #: For "nearest": metres from the node to that barangay's boundary.
    distance_m: float | None = None

    @property
    def is_known(self) -> bool:
        return self.score is not None


#: A node too far from every barangay, or in one with no population figure.
UNKNOWN_EXPOSURE = Exposure(barangay=None, density=None, score=None, basis="unknown")


#: The exposure scale, once it has been worked out from real boundaries.
_highest: float | None = None


def _highest_density() -> float:
    """The densest barangay, which exposure is scaled against.

    Cached only once there are densities to scale against, for the same
    reason :func:`load_barangays` does not cache a failure.
    """
    global _highest
    if _highest is not None:
        return _highest
    densities = [b.density for b in load_barangays() if b.density is not None]
    if not densities:
        return 0.0
    _highest = max(densities)
    return _highest


def _is_city_outline(barangay: Barangay, barangays: tuple[Barangay, ...]) -> bool:
    """The city-wide polygon: larger than all the others together (34.87 km²
    against 30.84 in the shipped data)."""
    area = barangay.land_area_km2 or 0.0
    others = sum(b.land_area_km2 or 0.0 for b in barangays if b is not barangay)
    return area > others > 0


def _closest(
    point: tuple[float, float], candidates: list[Barangay], within_m: float
) -> tuple[Barangay, float] | None:
    """The closest candidate with a population figure, and its distance in
    metres, if one is within ``within_m``."""
    best: tuple[Barangay, float] | None = None
    for barangay in candidates:
        if barangay.density is None:
            continue
        min_x, min_y, max_x, max_y = barangay.bbox
        # A degree is over 100 km, so a box this much larger holds every
        # point within reach; the edge-by-edge distance is the slow part.
        margin = within_m / 100_000
        if not (min_x - margin <= point[0] <= max_x + margin):
            continue
        if not (min_y - margin <= point[1] <= max_y + margin):
            continue
        distance = distance_to_rings_m(point, barangay.rings)
        if distance <= within_m and (best is None or distance < best[1]):
            best = (barangay, distance)
    return best


def _scored(barangay: Barangay, basis: str, distance_m: float | None = None) -> Exposure:
    assert barangay.density is not None
    highest = _highest_density()
    return Exposure(
        barangay=barangay.name,
        density=barangay.density,
        score=barangay.density / highest if highest > 0 else None,
        basis=basis,
        distance_m=None if distance_m is None else round(distance_m, 1),
    )


def exposure_for(point: tuple[float, float]) -> Exposure:
    """Population exposure at a longitude/latitude.

    In order: the barangay the point is in; the nearest barangay within
    NEAREST_WITHIN_M; the city outline, if the point is inside it or within
    reach of it (the barangay polygons leave gaps along the coast that only
    the outline covers, and it carries the city's average density); else
    unknown. A barangay with no published population is unknown, not its
    neighbour's figure.
    """
    barangays = load_barangays()
    outline = [b for b in barangays if _is_city_outline(b, barangays)]
    parts = [b for b in barangays if b not in outline]

    inside = barangay_at(point)
    if inside is not None and inside not in outline:
        if inside.density is None:
            return replace(UNKNOWN_EXPOSURE, barangay=inside.name)
        return _scored(inside, "inside")

    nearest = _closest(point, parts, NEAREST_WITHIN_M)
    if nearest is not None:
        return _scored(nearest[0], "nearest", nearest[1])
    if inside is not None and inside.density is not None:
        return _scored(inside, "inside")
    nearest = _closest(point, outline, NEAREST_WITHIN_M)
    if nearest is not None:
        return _scored(nearest[0], "nearest", nearest[1])
    return UNKNOWN_EXPOSURE


#: Node exposures, worked out once per set of boundaries and node locations.
_node_exposures: tuple[object, object, dict[str, Exposure]] | None = None


def exposures_for_nodes(locations: Mapping[str, tuple[float, float]]) -> dict[str, Exposure]:
    """Exposure for every node, computed once and reused.

    Neither the boundaries nor the network change between runs, and
    matching ~1,400 nodes against every polygon was repeated on every
    request. Not cached while the boundaries fail to load, so a passing
    read error doesn't leave every later run without exposure.
    """
    global _node_exposures
    barangays = load_barangays()
    cached = _node_exposures
    if cached is not None and cached[0] is barangays and cached[1] is locations:
        return cached[2]
    exposures = {node_id: exposure_for(point) for node_id, point in locations.items()}
    if barangays:
        _node_exposures = (barangays, locations, exposures)
    return exposures
