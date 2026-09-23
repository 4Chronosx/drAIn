"""Request and response models for the simulation API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class NodeOverride(BaseModel):
    """Per-node property overrides applied before the simulation runs."""

    inv_elev: float | None = None
    init_depth: float | None = None
    ponding_area: float | None = None
    surcharge_depth: float | None = None


class LinkOverride(BaseModel):
    """Per-conduit property overrides applied before the simulation runs."""

    init_flow: float | None = None
    upstrm_offset_depth: float | None = None
    downstrm_offset_depth: float | None = None
    avg_conduit_loss: float | None = None


class RainfallSpec(BaseModel):
    """A design storm, described by its total depth and duration."""

    total_precip: float = Field(ge=0, description="Total rainfall depth in mm.")
    # The shipped network runs a 24-hour window. A longer storm would be
    # silently truncated by SWMM, so reject it rather than return results that
    # do not correspond to the request.
    duration_hr: float = Field(gt=0, le=24, description="Storm duration in hours (max 24).")


class SimulationRequest(BaseModel):
    """A simulation request.

    All three sections are optional; omitting every one of them asks for the
    unmodified network, whose results are served from the shipped baseline.
    """

    nodes: dict[str, NodeOverride] = Field(default_factory=dict)
    links: dict[str, LinkOverride] = Field(default_factory=dict)
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


class HealthResponse(BaseModel):
    status: str
    vulnerability_model_loaded: bool
