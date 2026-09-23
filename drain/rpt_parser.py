"""Parsing of SWMM ``.rpt`` report files.

SWMM reports are fixed-width text. Only the "Node Flooding Summary" table is
needed here: it lists the nodes that flooded, and omits those that did not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SECTION_HEADING = "Node Flooding Summary"

#: Columns: node, hours flooded, max rate, day of max, time of max, volume, ...
MIN_COLUMNS = 6


@dataclass(frozen=True)
class FloodedNode:
    """One row of the Node Flooding Summary."""

    hours_flooded: float
    maximum_rate_cms: float
    time_of_max_days: float
    #: Minute-of-hour at which the maximum occurred. The report prints this as
    #: ``HH:MM``; only the minutes are kept, matching how the vulnerability
    #: model was trained.
    time_of_max_minutes: int
    total_flood_volume: float


def _parse_row(parts: list[str]) -> tuple[str, FloodedNode] | None:
    """Parse one whitespace-split summary row, or ``None`` if it is malformed."""
    try:
        node_id = parts[0].strip()
        _hours, minutes = (int(x) for x in parts[4].split(":"))
        return node_id, FloodedNode(
            hours_flooded=float(parts[1]),
            maximum_rate_cms=float(parts[2]),
            time_of_max_days=float(parts[3]),
            time_of_max_minutes=minutes,
            total_flood_volume=float(parts[5]),
        )
    except (ValueError, IndexError):
        logger.debug("Skipping unparseable flooding summary row: %r", parts)
        return None


def parse_flooding_summary(rpt_path: Path) -> dict[str, FloodedNode]:
    """Extract the Node Flooding Summary from a SWMM report.

    Returns a mapping of node ID to its flooding figures. Nodes absent from
    the table did not flood; the caller supplies zeroes for them.
    """
    lines = Path(rpt_path).read_text(encoding="utf-8", errors="replace").splitlines()

    data_start = _find_data_start(lines)
    if data_start is None:
        logger.warning("No %r section found in %s", SECTION_HEADING, rpt_path)
        return {}

    flooded: dict[str, FloodedNode] = {}
    for line in lines[data_start:]:
        stripped = line.strip()
        if not stripped:
            continue
        # A new section heading or a rule of dashes ends the table.
        if stripped.startswith("*") or (stripped.startswith("-") and len(stripped) > 10):
            break

        parts = stripped.split()
        if len(parts) < MIN_COLUMNS:
            logger.debug("Skipping short flooding summary row: %r", parts)
            continue

        parsed = _parse_row(parts)
        if parsed is not None:
            flooded[parsed[0]] = parsed[1]

    logger.info("Parsed %d flooded nodes from %s", len(flooded), rpt_path)
    return flooded


def _find_data_start(lines: list[str]) -> int | None:
    """Return the index of the first data row of the flooding summary.

    The table is laid out as a heading, a rule of dashes, column labels, a
    second rule, then the rows -- so data begins after the second rule.
    """
    heading_seen = False
    rules_seen = 0
    for index, line in enumerate(lines):
        if SECTION_HEADING in line:
            heading_seen = True
            continue
        if heading_seen and "----------" in line:
            rules_seen += 1
            if rules_seen == 2:
                return index + 1
    return None
