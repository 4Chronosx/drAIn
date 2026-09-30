"""Turning one simulation request into the payload the API sends.

Kept apart from :mod:`app.main` so a child process (:mod:`app.isolation`)
can import it without building the whole application.
"""

from __future__ import annotations

import threading
from typing import Any

from app.schemas import SimulationRequest
from drain.flooding import build_flooding_summary
from drain.hazard import DEFAULT_EVENT_HOURS
from drain.swmm_runner import run_simulation

#: What a stored run of the unmodified network keeps in place of its
#: result. Every such run has the same 0.7 MB result, so the table holds
#: this and the server fills the real one in when the run is read back.
BASELINE_MARKER: dict[str, Any] = {"unmodified_network": True}


def is_unmodified(request: SimulationRequest) -> bool:
    """Whether the request asks for the network as shipped: no overrides
    and no storm. Mirrors the check :func:`run_simulation` makes."""
    return not (request.node_overrides() or request.link_overrides() or request.rainfall_spec())


def served(summary: dict[str, Any]) -> dict[str, Any]:
    """What the API sends for a finished run: each node once, in nodes_list.

    build_flooding_summary also keys every node by id (nodes_dict) for the
    CLI and scripts. Sent as well, it doubled the payload: 1.39 MB against
    0.70 MB, or 40 KB against 21 KB gzipped, for the baseline. No client
    used it.
    """
    metadata = {k: v for k, v in summary["metadata"].items() if k != "structure_info"}
    return {**{k: v for k, v in summary.items() if k != "nodes_dict"}, "metadata": metadata}


def simulate(request: SimulationRequest) -> dict[str, Any]:
    """Run one simulation to completion, in this process."""
    rainfall = request.rainfall_spec()

    # Hazard scores duration as a share of the event, so the scorer needs
    # the storm's real length. Without it a 40-minute flood in a one-hour
    # storm reads as 3% of a day rather than two thirds of the event.
    event_hours = float(rainfall.get("duration_hr") or DEFAULT_EVENT_HOURS)

    with run_simulation(
        nodes=request.node_overrides(),
        links=request.link_overrides(),
        rainfall=rainfall,
    ) as (rpt_path, out_path):
        summary = build_flooding_summary(rpt_path, out_path, event_hours=event_hours)
    return served(summary)


def simulate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """:func:`simulate` for a request sent as plain data, as it reaches a
    child process."""
    return simulate(SimulationRequest.model_validate(payload))


_baseline: dict[str, Any] | None = None
_baseline_lock = threading.Lock()


def baseline_result() -> dict[str, Any]:
    """The unmodified network's result, built once and shared.

    Every job that asks for the unmodified network holds this same object
    rather than its own 0.7 MB copy, so nothing may modify it.
    """
    global _baseline
    with _baseline_lock:
        if _baseline is None:
            _baseline = simulate(SimulationRequest())
        return _baseline


def is_baseline(result: dict[str, Any] | None) -> bool:
    """Whether a result is the shared baseline (not merely equal to it)."""
    return result is not None and result is _baseline
