"""Request and response models for the simulation API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.jobs import JobStatus

#: Overrides are handed straight to SWMM, which does not check them: a
#: negative depth or an infinite area either produces nonsense or upsets
#: the engine. So every value must be a finite number inside a range that
#: is physically possible for this network, and a misspelt field is an
#: error rather than silently ignored. The ranges are a little wider than
#: the app's sliders, not a statement of what is sensible to try.
_STRICT = ConfigDict(allow_inf_nan=False, extra="forbid")

#: A node or link id, as the network names it (e.g. I-4, C-88).
ComponentId = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class NodeOverride(BaseModel):
    """Per-node property overrides applied before the simulation runs."""

    model_config = _STRICT

    # Every node in the shipped network sits between 0.26 m and 41.7 m.
    inv_elev: float | None = Field(None, ge=0, le=100, description="Invert elevation, m.")
    # Nodes are 1.2 m deep.
    init_depth: float | None = Field(None, ge=0, le=10, description="Initial water depth, m.")
    ponding_area: float | None = Field(None, ge=0, le=10_000, description="Ponded area, m².")
    surcharge_depth: float | None = Field(
        None, ge=0, le=10, description="Extra depth before flooding, m."
    )


class LinkOverride(BaseModel):
    """Per-conduit property overrides applied before the simulation runs."""

    model_config = _STRICT

    # Named init_flow on the wire, but it sets the conduit's flow limit.
    init_flow: float | None = Field(None, ge=0, le=100, description="Flow limit, m³/s.")
    upstrm_offset_depth: float | None = Field(
        None, ge=0, le=20, description="Inlet offset above the node invert, m."
    )
    downstrm_offset_depth: float | None = Field(
        None, ge=0, le=20, description="Outlet offset above the node invert, m."
    )
    avg_conduit_loss: float | None = Field(
        None, ge=0, le=100, description="Average minor-loss coefficient."
    )


class RainfallSpec(BaseModel):
    """A design storm, described by its total depth and duration."""

    model_config = _STRICT

    # 2,000 mm is past the heaviest 24 hours ever recorded anywhere; it
    # only catches nonsense.
    total_precip: float = Field(ge=0, le=2000, description="Total rainfall depth in mm.")
    # The shipped network runs a 24-hour window. A longer storm would be
    # silently truncated by SWMM, so reject it rather than return results that
    # do not correspond to the request.
    duration_hr: float = Field(gt=0, le=24, description="Storm duration in hours (max 24).")


class SimulationRequest(BaseModel):
    """A simulation request.

    All three sections are optional; omitting every one of them asks for the
    unmodified network, whose results are served from the shipped baseline.
    """

    model_config = ConfigDict(extra="forbid")

    nodes: dict[ComponentId, NodeOverride] = Field(default_factory=dict)
    links: dict[ComponentId, LinkOverride] = Field(default_factory=dict)
    rainfall: RainfallSpec | None = None

    @model_validator(mode="before")
    @classmethod
    def _empty_rainfall_means_none(cls, data: Any) -> Any:
        """Treat ``"rainfall": {}`` as "no storm".

        Existing clients send an empty object to request the unmodified
        network; without this it would fail validation on the missing fields.
        """
        if isinstance(data, dict) and data.get("rainfall") == {}:
            data = {**data, "rainfall": None}
        return data

    def node_overrides(self) -> dict[str, dict[str, Any]]:
        return {k: v.model_dump(exclude_none=True) for k, v in self.nodes.items()}

    def link_overrides(self) -> dict[str, dict[str, Any]]:
        return {k: v.model_dump(exclude_none=True) for k, v in self.links.items()}

    def rainfall_spec(self) -> dict[str, Any]:
        return self.rainfall.model_dump() if self.rainfall else {}


class JobAccepted(BaseModel):
    """Returned when a simulation has been queued."""

    job_id: str
    #: Always ``queued``: this describes the outcome of the request, not a
    #: live reading. A worker may already have picked the job up by the time
    #: this is serialised, so the authoritative state comes from polling.
    status: JobStatus = JobStatus.QUEUED
    #: Where to poll for the outcome.
    poll_url: str


class JobState(BaseModel):
    """The current state of a queued simulation.

    ``result`` is populated only once ``status`` is ``succeeded``, and
    ``error`` only once it is ``failed``.
    """

    job_id: str
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: str
