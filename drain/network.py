"""Reading the drainage network's own geometry out of the SWMM input file.

The ``.inp`` is the authoritative description of the network, including
where each node sits. Its coordinates are projected, so they are converted
to longitude/latitude here for anything that needs to relate a node to the
world -- which barangay it is in, for instance.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from drain.geo import utm_to_lonlat
from drain.paths import BASE_INP

logger = logging.getLogger(__name__)

SECTION_HEADER = "[COORDINATES]"


def _section_rows(inp_path: Path, section: str) -> list[list[str]]:
    """The data rows of one ``[SECTION]``, split on whitespace."""
    rows: list[list[str]] = []
    in_section = False
    with open(inp_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("["):
                in_section = stripped.upper() == section
                continue
            if in_section and stripped and not stripped.startswith(";"):
                rows.append(stripped.split())
    return rows


@lru_cache(maxsize=1)
def node_ids(inp_path: Path = BASE_INP) -> frozenset[str]:
    """Every junction and outfall in the network: what a node override may name."""
    return frozenset(
        row[0]
        for section in ("[JUNCTIONS]", "[OUTFALLS]")
        for row in _section_rows(inp_path, section)
    )


@lru_cache(maxsize=1)
def link_suffixes(inp_path: Path = BASE_INP) -> frozenset[str]:
    """What a link override may name.

    Conduits are named ``C_<n>-<pipe id>``, and the app knows pipes by the
    part after the first dash (``C-88`` for ``C_0-C-88``). The runner
    matches overrides on that suffix; the full name is accepted too.
    """
    names = [row[0] for row in _section_rows(inp_path, "[CONDUITS]")]
    return frozenset(names) | frozenset(name.split("-", 1)[1] for name in names if "-" in name)


@lru_cache(maxsize=1)
def node_locations(inp_path: Path = BASE_INP) -> dict[str, tuple[float, float]]:
    """Every node's (longitude, latitude), read from the network file.

    Cached: the file is well over a megabyte and its geometry does not
    change between requests.
    """
    locations: dict[str, tuple[float, float]] = {}
    in_section = False

    with open(inp_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("["):
                # Any other section header ends this one.
                in_section = stripped.upper() == SECTION_HEADER
                continue
            if not in_section or not stripped or stripped.startswith(";"):
                continue

            parts = stripped.split()
            if len(parts) < 3:
                continue
            try:
                locations[parts[0]] = utm_to_lonlat(float(parts[1]), float(parts[2]))
            except ValueError:
                logger.debug("Skipping unparseable coordinate row: %r", parts)

    logger.info("Read %d node locations from %s", len(locations), inp_path.name)
    return locations
