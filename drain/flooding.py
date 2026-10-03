"""Assembly of the node flooding payload served by the API.

Combines three sources: the flooded-node table from the ``.rpt`` report, the
overflow timing from the ``.out`` binary output, the hazard score
(drain/hazard.py) and population exposure (drain/exposure.py).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pyswmm import NodeSeries, Output

from drain.exposure import UNKNOWN_EXPOSURE, exposures_for_nodes
from drain.hazard import DEFAULT_EVENT_HOURS, hazard_for
from drain.model_info import model_info
from drain.network import node_locations
from drain.rpt_parser import FloodedNode, parse_flooding_summary

logger = logging.getLogger(__name__)

#: Figures reported for a node that does not appear in the flooding summary.
_NO_FLOODING = FloodedNode(
    hours_flooded=0.0,
    maximum_rate_cms=0.0,
    time_of_max_days=0.0,
    time_of_max_minutes=0,
    total_flood_volume=0.0,
)


def _minutes_until_overflow(node_series: NodeSeries, node_id: str) -> float | None:
    """Minutes from simulation start until the node first overflows.

    None if it never does, or if the output has no series for it at all:
    that is missing data, not an overflow at minute zero.
    """
    losses = node_series[node_id].flooding_losses
    if not losses:
        return None

    start = next(iter(losses))
    for timestamp, rate in losses.items():
        if rate > 0:
            return round((timestamp - start).total_seconds() / 60, 2)
    return None


def build_flooding_summary(
    rpt_path: Path,
    out_path: Path,
    event_hours: float = DEFAULT_EVENT_HOURS,
) -> dict[str, Any]:
    """Build the full node flooding payload for a completed simulation.

    Every node in the ``.out`` file is present in the result; those that did
    not flood carry zeroes. The payload exposes the same data twice -- as a
    list for iteration and as a dict for lookup by node ID -- because the
    frontend uses both.
    """
    flooded = parse_flooding_summary(rpt_path)

    node_ids: list[str] = []
    #: Minutes until each node first overflows, or None if it never does.
    overflow_minutes: dict[str, float | None] = {}

    with Output(str(out_path)) as out:
        series = NodeSeries(out)
        for node_id in out.nodes:
            summary = flooded.get(node_id)

            # The overflow timing is only reported for nodes that actually
            # flooded, so skip reading the (large) time series for the rest.
            node_ids.append(node_id)
            overflow_minutes[node_id] = (
                _minutes_until_overflow(series, node_id) if summary is not None else None
            )

    nodes_list: list[dict[str, Any]] = []
    nodes_dict: dict[str, dict[str, Any]] = {}

    exposures = exposures_for_nodes(node_locations())
    inconsistent = 0

    for node_id in node_ids:
        summary = flooded.get(node_id, _NO_FLOODING)
        minutes = overflow_minutes[node_id]

        hazard = hazard_for(
            total_flood_volume=summary.total_flood_volume,
            hours_flooded=summary.hours_flooded,
            maximum_rate_cms=summary.maximum_rate_cms,
            event_hours=event_hours,
        )
        exposure = exposures.get(node_id, UNKNOWN_EXPOSURE)

        # The report's summary table and the binary output disagree for some
        # nodes: the first says the node flooded, the second shows no
        # positive overflow anywhere in its series. SWMM counts flooding at
        # every routing step but writes the output once a minute, so brief or
        # flickering overflows miss it (docs/findings/2026-09-29-rpt-vs-out-
        # flooding.md). Counted so the mismatch is visible.
        if summary.hours_flooded > 0 and minutes is None:
            inconsistent += 1

        row = {
            "Hours_Flooded": summary.hours_flooded,
            "Maximum_Rate_CMS": summary.maximum_rate_cms,
            "Time_of_Max_days": summary.time_of_max_days,
            "Time_of_Max_hr_min": summary.time_of_max_minutes,
            "Total_Flood_Volume_10e6_ltr": summary.total_flood_volume,
            # null when the node never overflowed.
            "Time_After_Raining_min": minutes,
            # Hazard: how badly this node floods. Transparent and monotonic.
            "Vulnerability_Category": hazard.category,
            "Vulnerability_Score": hazard.score,
            # Exposure: roughly how many people are around it.
            "Barangay": exposure.barangay,
            "Population_Density": exposure.density,
            # null when the population around the node isn't known; see
            # Exposure_Basis. Risk is then null too rather than a guess.
            "Exposure_Score": None if exposure.score is None else round(exposure.score, 4),
            "Exposure_Basis": exposure.basis,
            "Exposure_Distance_m": exposure.distance_m,
            # Risk: the two together, which is what a work list should rank on.
            "Risk_Score": (
                None if exposure.score is None else round(hazard.score * exposure.score, 6)
            ),
        }
        nodes_dict[node_id] = row
        nodes_list.append({"Node": node_id, **row})

    flooded_count = sum(1 for row in nodes_list if row["Hours_Flooded"] > 0)
    logger.info("Built flooding summary: %d nodes, %d flooded", len(nodes_list), flooded_count)
    if inconsistent:
        logger.warning(
            "%d nodes are reported as flooded in %s but show no overflow in %s; "
            "their time-to-overflow is unusable.",
            inconsistent,
            Path(rpt_path).name,
            Path(out_path).name,
        )

    return {
        "metadata": {
            "total_nodes": len(nodes_list),
            "flooded_nodes": flooded_count,
            "non_flooded_nodes": len(nodes_list) - flooded_count,
            # Basenames only: the absolute paths are server-side detail and,
            # for a simulated run, point into a temporary directory.
            "rpt_file": Path(rpt_path).name,
            "out_file": Path(out_path).name,
            "event_hours": event_hours,
            # Nodes the report calls flooded but the binary output does not.
            "inconsistent_nodes": inconsistent,
            # How each node's exposure was found: inside a barangay, from
            # the nearest one, or not at all.
            "exposure_basis_counts": {
                basis: sum(1 for row in nodes_list if row["Exposure_Basis"] == basis)
                for basis in ("inside", "nearest", "unknown")
            },
            # What the ratings can and cannot claim; shown where they are read.
            "model_info": model_info(),
            "scoring": {
                "hazard": "Vulnerability_Score: 0-1, from flood volume, duration and peak rate.",
                "exposure": (
                    "Exposure_Score: 0-1, from the population density of the barangay the "
                    "node is in, or of the nearest one within 250 m (Exposure_Basis "
                    "'nearest'). null when neither is known (Exposure_Basis 'unknown')."
                ),
                "risk": "Risk_Score: hazard x exposure, null when exposure is. Rank on this.",
            },
            "structure_info": {
                "nodes_list": "Array format - use for iteration and listing all nodes",
                "nodes_dict": "Dictionary format - use for fast O(1) lookup by node ID",
            },
        },
        "nodes_list": nodes_list,
        "nodes_dict": nodes_dict,
    }
