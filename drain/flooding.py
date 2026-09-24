"""Assembly of the node flooding payload served by the API.

Combines three sources: the flooded-node table from the ``.rpt`` report, the
overflow timing from the ``.out`` binary output, and risk categories from the
trained vulnerability model.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pyswmm import NodeSeries, Output

from drain.exposure import UNKNOWN_EXPOSURE, exposure_for
from drain.hazard import DEFAULT_EVENT_HOURS, hazard_for
from drain.network import node_locations
from drain.rpt_parser import FloodedNode, parse_flooding_summary
from drain.vulnerability import NodeFeatures, VulnerabilityModel, load_model

logger = logging.getLogger(__name__)

#: Sentinel used for "this node never overflowed". Kept as a large number
#: rather than None because the frontend sorts on this field numerically.
NO_OVERFLOW_MINUTES = 9999.0

#: Figures reported for a node that does not appear in the flooding summary.
_NO_FLOODING = FloodedNode(
    hours_flooded=0.0,
    maximum_rate_cms=0.0,
    time_of_max_days=0.0,
    time_of_max_minutes=0,
    total_flood_volume=0.0,
)


def _minutes_until_overflow(node_series: NodeSeries, node_id: str) -> float:
    """Minutes from simulation start until the node first overflows.

    Returns :data:`NO_OVERFLOW_MINUTES` if it never does.
    """
    losses = node_series[node_id].flooding_losses
    if not losses:
        return 0.0

    start = next(iter(losses))
    for timestamp, rate in losses.items():
        if rate > 0:
            return round((timestamp - start).total_seconds() / 60, 2)
    return NO_OVERFLOW_MINUTES


def build_flooding_summary(
    rpt_path: Path,
    out_path: Path,
    model: VulnerabilityModel | None = None,
    event_hours: float = DEFAULT_EVENT_HOURS,
) -> dict[str, Any]:
    """Build the full node flooding payload for a completed simulation.

    Every node in the ``.out`` file is present in the result; those that did
    not flood carry zeroes. The payload exposes the same data twice -- as a
    list for iteration and as a dict for lookup by node ID -- because the
    frontend uses both.
    """
    flooded = parse_flooding_summary(rpt_path)
    if model is None:
        model = load_model()

    node_ids: list[str] = []
    features: list[NodeFeatures] = []

    with Output(str(out_path)) as out:
        series = NodeSeries(out)
        for node_id in out.nodes:
            summary = flooded.get(node_id)

            # The overflow timing is only reported for nodes that actually
            # flooded, so skip reading the (large) time series for the rest.
            time_after_rain = (
                _minutes_until_overflow(series, node_id)
                if summary is not None
                else NO_OVERFLOW_MINUTES
            )
            summary = summary or _NO_FLOODING

            node_ids.append(node_id)
            features.append(
                NodeFeatures(
                    time_after_raining_min=time_after_rain,
                    hours_flooded=summary.hours_flooded,
                    maximum_rate_cms=summary.maximum_rate_cms,
                    time_of_max_hr_min=summary.time_of_max_minutes,
                    total_flood_volume=summary.total_flood_volume,
                )
            )

    predictions = model.predict_many(features) if model is not None else [None] * len(features)

    nodes_list: list[dict[str, Any]] = []
    nodes_dict: dict[str, dict[str, Any]] = {}

    locations = node_locations()
    inconsistent = 0

    for node_id, feature, prediction in zip(node_ids, features, predictions, strict=True):
        summary = flooded.get(node_id, _NO_FLOODING)

        hazard = hazard_for(
            total_flood_volume=summary.total_flood_volume,
            hours_flooded=summary.hours_flooded,
            maximum_rate_cms=summary.maximum_rate_cms,
            event_hours=event_hours,
        )
        exposure = exposure_for(locations[node_id]) if node_id in locations else UNKNOWN_EXPOSURE

        # The report's summary table and the binary output disagree for some
        # nodes: the first says the node flooded, the second shows no
        # positive overflow anywhere in its series. Counted so the mismatch
        # is visible rather than silently shaping the results.
        if summary.hours_flooded > 0 and feature.time_after_raining_min >= NO_OVERFLOW_MINUTES:
            inconsistent += 1

        row = {
            "Hours_Flooded": summary.hours_flooded,
            "Maximum_Rate_CMS": summary.maximum_rate_cms,
            "Time_of_Max_days": summary.time_of_max_days,
            "Time_of_Max_hr_min": summary.time_of_max_minutes,
            "Total_Flood_Volume_10e6_ltr": summary.total_flood_volume,
            # null, not the sentinel. 9999 is an internal marker for "never
            # overflowed"; served as-is it reads as a measurement, and the
            # results table showed "9,999" for three quarters of all nodes.
            "Time_After_Raining_min": (
                None
                if feature.time_after_raining_min >= NO_OVERFLOW_MINUTES
                else feature.time_after_raining_min
            ),
            # Hazard: how badly this node floods. Transparent and monotonic.
            "Vulnerability_Category": hazard.category,
            "Vulnerability_Score": hazard.score,
            # Exposure: roughly how many people are around it.
            "Barangay": exposure.barangay,
            "Population_Density": exposure.density,
            "Exposure_Score": round(exposure.score, 4),
            # Risk: the two together, which is what a work list should rank on.
            "Risk_Score": round(hazard.score * exposure.score, 6),
            # The previous k-means output, kept for comparison.
            "Legacy_Cluster_Category": prediction.category if prediction else "N/A",
            "Legacy_Cluster_Score": prediction.score if prediction else 0.0,
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
            "model_file": "N/A" if model is None else "vulnerability_model_k4.pkl",
            "event_hours": event_hours,
            # Nodes the report calls flooded but the binary output does not.
            "inconsistent_nodes": inconsistent,
            "scoring": {
                "hazard": "Vulnerability_Score: 0-1, from flood volume, duration and peak rate.",
                "exposure": (
                    "Exposure_Score: 0-1, from the population density of the containing barangay."
                ),
                "risk": "Risk_Score: hazard x exposure. Rank work lists on this.",
                "legacy": "Legacy_Cluster_*: the previous k-means output, retained for comparison.",
            },
            "structure_info": {
                "nodes_list": "Array format - use for iteration and listing all nodes",
                "nodes_dict": "Dictionary format - use for fast O(1) lookup by node ID",
            },
        },
        "nodes_list": nodes_list,
        "nodes_dict": nodes_dict,
    }
