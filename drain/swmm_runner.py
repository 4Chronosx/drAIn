"""Running SWMM simulations against the bundled drainage network.

Each run happens in its own temporary directory. SWMM writes its report and
binary output next to the input file, so sharing one directory between
concurrent requests would let them overwrite each other's results -- and, for
an unmodified run, clobber the committed baseline artifacts.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pyswmm import Links, Nodes, Simulation, SimulationPreConfig

from drain.paths import BASE_INP, BASE_OUT, BASE_RPT, mod_artifacts
from drain.rainfall import RainfallStep, generate_rainfall_event

logger = logging.getLogger(__name__)

RAIN_TIMESERIES_NAME = "TS_Rain"

#: Number of rows in the [TIMESERIES] section of the shipped .inp file. A
#: shorter generated storm must blank out the leftover rows, or SWMM keeps
#: raining from the original 24-hour series.
BASE_TIMESERIES_ROWS = 289

DEFAULT_DURATION_HR = 1.0
DEFAULT_TOTAL_PRECIP_MM = 100.0
SIMULATION_START_DATE = "01/01/2024"

#: Fields read from each entry of the ``nodes`` request payload.
NODE_FIELDS = {
    "inv_elev": "invert_elevation",
    "init_depth": "initial_depth",
    "ponding_area": "ponding_area",
    "surcharge_depth": "surcharge_depth",
}

#: Fields read from each entry of the ``links`` request payload.
LINK_FIELDS = {
    "init_flow": "flow_limit",
    "upstrm_offset_depth": "upstrm_offset_depth",
    "downstrm_offset_depth": "downstrm_offset_depth",
    "avg_conduit_loss": "avg_conduit_loss",
}


def _apply_node_overrides(sim: Simulation, overrides: Mapping[str, Mapping[str, Any]]) -> None:
    """Apply per-node property overrides to a running simulation."""
    nodes = Nodes(sim)
    for node_id, properties in overrides.items():
        try:
            node = nodes[node_id]
        except (KeyError, IndexError):
            logger.warning("Ignoring override for unknown node %r", node_id)
            continue
        for key, attribute in NODE_FIELDS.items():
            if key in properties:
                setattr(node, attribute, properties[key])


def _apply_link_overrides(sim: Simulation, overrides: Mapping[str, Mapping[str, Any]]) -> None:
    """Apply per-link property overrides to a running simulation.

    Request keys are conduit name suffixes rather than full link IDs, so each
    one is matched against the network's links.
    """
    if not overrides:
        return

    links = list(Links(sim))
    for suffix, properties in overrides.items():
        # Exact: the whole name, or the part after the first dash (C-88 for
        # C_0-C-88). endswith("88") would also catch ...-C-188.
        matches = [
            link
            for link in links
            if link.linkid == suffix or link.linkid.split("-", 1)[-1] == suffix
        ]
        if not matches:
            logger.warning("Ignoring override for unmatched link suffix %r", suffix)
            continue
        for link in matches:
            for key, attribute in LINK_FIELDS.items():
                if key in properties:
                    setattr(link, attribute, properties[key])


def _end_time_tokens(duration_hr: float) -> tuple[str, str]:
    """Return the ``END_TIME``/``END_DATE`` values for a storm of this length.

    Rounded to the whole minute first and split afterwards. Rounding the
    minutes on their own turned 1.999 h into "01:60:00".
    """
    hours, minutes = divmod(round(duration_hr * 60), 60)
    return f"{hours:02d}:{minutes:02d}:00", SIMULATION_START_DATE


def _build_preconfig(rainfall: Mapping[str, Any]) -> SimulationPreConfig:
    """Translate a rainfall request into SWMM input-file edits."""
    config = SimulationPreConfig()

    duration_hr = float(rainfall.get("duration_hr", DEFAULT_DURATION_HR))
    total_precip = float(rainfall.get("total_precip", DEFAULT_TOTAL_PRECIP_MM))
    series: list[RainfallStep] = generate_rainfall_event(total_precip, duration_hr)

    logger.info(
        "Generated %d-step storm: %.1f mm over %.2f h", len(series), total_precip, duration_hr
    )

    if duration_hr < 24:
        end_time, end_date = _end_time_tokens(duration_hr)
        config.add_update_by_token("OPTIONS", "END_TIME", 1, end_time)
        config.add_update_by_token("OPTIONS", "END_DATE", 1, end_date)

    for row, step in enumerate(series):
        hours = int(step.hours)
        minutes = round((step.hours - hours) * 60)
        config.add_update_by_token(
            "TIMESERIES", RAIN_TIMESERIES_NAME, 1, f"{hours:02}:{minutes:02}", row
        )
        config.add_update_by_token(
            "TIMESERIES", RAIN_TIMESERIES_NAME, 2, f"{step.intensity_mm_hr:.2f}", row
        )

    # Blank any rows the shipped series has beyond the generated storm.
    leftover = BASE_TIMESERIES_ROWS - len(series)
    if leftover > 0:
        for row in range(len(series), BASE_TIMESERIES_ROWS):
            for column in (0, 1, 2):
                config.add_update_by_token("TIMESERIES", RAIN_TIMESERIES_NAME, column, "", row)
        logger.info("Cleared %d leftover timeseries rows", leftover)

    return config


@contextmanager
def run_simulation(
    nodes: Mapping[str, Mapping[str, Any]] | None = None,
    links: Mapping[str, Mapping[str, Any]] | None = None,
    rainfall: Mapping[str, Any] | None = None,
) -> Iterator[tuple[Path, Path]]:
    """Run a SWMM simulation and yield the paths to its ``.rpt`` and ``.out``.

    Both paths live in a temporary directory that is removed when the context
    exits, so the results must be consumed inside the ``with`` block.

    A request with no rainfall and no node or link overrides asks for the
    unmodified network, whose results are shipped pre-computed; those are
    yielded directly rather than spending minutes reproducing them.
    """
    nodes = nodes or {}
    links = links or {}

    if not rainfall and not nodes and not links:
        logger.info("Request matches the unmodified network; serving pre-computed results")
        yield BASE_RPT, BASE_OUT
        return

    with tempfile.TemporaryDirectory(prefix="drain-sim-") as workdir:
        # Copy the network in so SWMM's output lands in the temp directory.
        inp_path = Path(workdir) / BASE_INP.name
        shutil.copyfile(BASE_INP, inp_path)

        # Without a rainfall override there is nothing to rewrite in the input
        # file, so SWMM runs it as-is and names its output after the original
        # rather than the "_mod" copy a pre-config produces.
        config = _build_preconfig(rainfall) if rainfall else None
        if config is not None:
            rpt_path, out_path = mod_artifacts(inp_path)
        else:
            rpt_path = inp_path.with_suffix(".rpt")
            out_path = inp_path.with_suffix(".out")

        logger.info("Starting SWMM simulation in %s", workdir)
        with Simulation(str(inp_path), sim_preconfig=config) as sim:
            _apply_node_overrides(sim, nodes)
            _apply_link_overrides(sim, links)
            for _ in sim:
                pass
        logger.info("SWMM simulation finished")

        if not rpt_path.exists() or not out_path.exists():
            raise RuntimeError(
                f"SWMM produced no results; expected {rpt_path.name} and {out_path.name}."
            )
        yield rpt_path, out_path
