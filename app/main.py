"""FastAPI application exposing the drainage simulation."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, settings
from app.logging_config import configure_logging
from app.schemas import HealthResponse, SimulationRequest
from drain.flooding import build_flooding_summary
from drain.swmm_runner import run_simulation
from drain.vulnerability import load_model

logger = logging.getLogger(__name__)

#: SWMM runs are CPU-bound and take minutes. Bound how many proceed at once so
#: a burst of requests does not starve every one of them.
_simulation_slots = threading.BoundedSemaphore(settings.max_concurrent_simulations)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm the vulnerability model so the first request is not slower."""
    configure_logging(settings.log_level)
    if load_model() is None:
        logger.warning("Vulnerability model unavailable; nodes will be scored as 'N/A'.")
    yield


def create_app(config: Settings = settings) -> FastAPI:
    """Build the application. Kept separate from the module-level instance so
    tests can construct an app with their own settings."""
    app = FastAPI(
        title="DrAIn simulation API",
        description="Runs SWMM simulations of the Mandaue drainage network.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.allowed_origins),
        allow_origin_regex=config.origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Liveness probe that also reports whether scoring is available."""
        return HealthResponse(
            status="ok",
            vulnerability_model_loaded=load_model() is not None,
        )

    @app.post("/run-simulation")
    def simulate(request: SimulationRequest) -> dict[str, Any]:
        """Run a simulation and return per-node flooding and vulnerability.

        Failures return 5xx rather than an empty object: the previous
        implementation returned ``{}`` with a 200, which surfaced downstream
        as a confusing "cannot read nodes_list of undefined".
        """
        with _simulation_slots:
            try:
                with run_simulation(
                    nodes=request.node_overrides(),
                    links=request.link_overrides(),
                    rainfall=request.rainfall_spec(),
                ) as (rpt_path, out_path):
                    return build_flooding_summary(rpt_path, out_path)
            except FileNotFoundError:
                logger.exception("Simulation inputs or results were missing")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Simulation results could not be read.",
                ) from None
            except Exception:
                logger.exception("Simulation failed")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Simulation failed.",
                ) from None

    return app


app = create_app()
