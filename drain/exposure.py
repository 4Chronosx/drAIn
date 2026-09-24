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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from drain.geo import Ring, bounding_box, point_in_polygon
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


@lru_cache(maxsize=1)
def load_barangays(path: Path = POPULATION_BOUNDARIES) -> tuple[Barangay, ...]:
    """Read the barangay boundaries, or return empty if they are unavailable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
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
    return tuple(barangays)


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


@dataclass(frozen=True)
class Exposure:
    """What surrounds a node."""

    barangay: str | None
    density: float | None
    #: Density rescaled to 0-1 against the densest barangay in the dataset,
    #: so it can be multiplied against a hazard score.
    score: float

    @property
    def is_known(self) -> bool:
        return self.barangay is not None


#: Used where a node falls outside every mapped barangay. Neutral rather than
#: zero: an unmapped location is unknown, not uninhabited, and zeroing it
#: would quietly drop those nodes off any risk-ranked list.
UNKNOWN_EXPOSURE = Exposure(barangay=None, density=None, score=0.5)


@lru_cache(maxsize=1)
def _highest_density() -> float:
    """The densest barangay, which exposure is scaled against."""
    densities = [b.density for b in load_barangays() if b.density is not None]
    return max(densities) if densities else 0.0


def exposure_for(point: tuple[float, float]) -> Exposure:
    """Population exposure at a longitude/latitude."""
    barangay = barangay_at(point)
    if barangay is None or barangay.density is None:
        return UNKNOWN_EXPOSURE

    highest = _highest_density()
    score = barangay.density / highest if highest > 0 else 0.5

    return Exposure(barangay=barangay.name, density=barangay.density, score=score)
