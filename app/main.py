"""FastAPI application exposing the drainage simulation."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, settings
from app.jobs import JobStore, QueueFullError, SimulationJob
from app.logging_config import configure_logging
from app.schemas import HealthResponse, JobAccepted, JobState, SimulationRequest
from drain.flooding import build_flooding_summary
from drain.hazard import DEFAULT_EVENT_HOURS
from drain.swmm_runner import run_simulation
from drain.vulnerability import load_model

logger = logging.getLogger(__name__)

#: Suggested seconds between polls. A run takes minutes, so there is nothing
#: to gain from asking more often than this.
POLL_INTERVAL_SECONDS = 3


def _build_job_store(config: Settings) -> JobStore:
    return JobStore(
        max_workers=config.max_concurrent_simulations,
        max_queued=config.max_queued_simulations,
        retention=timedelta(seconds=config.result_retention_seconds),
        max_runtime=timedelta(seconds=config.max_runtime_seconds),
        max_queue_wait=timedelta(seconds=config.max_queue_wait_seconds),
    )


def _simulate(request: SimulationRequest) -> dict[str, Any]:
    """Run one simulation to completion. Executed on a worker thread."""
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
        return build_flooding_summary(rpt_path, out_path, event_hours=event_hours)


def _as_state(job: SimulationJob) -> JobState:
    return JobState(
        job_id=job.id,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        result=job.result,
        error=job.error,
    )


def create_app(config: Settings = settings) -> FastAPI:
    """Build the application. Kept separate from the module-level instance so
    tests can construct an app with their own settings."""
    jobs = _build_job_store(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Warm the vulnerability model so the first run is not slower."""
        configure_logging(config.log_level)
        if load_model() is None:
            logger.warning("Vulnerability model unavailable; nodes will be scored as 'N/A'.")
        yield
        jobs.shutdown()

    app = FastAPI(
        title="DrAIn simulation API",
        description=(
            "Runs SWMM simulations of the Mandaue drainage network. A run "
            "takes minutes, so simulations are queued and polled rather than "
            "awaited on the request."
        ),
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

    @app.post(
        "/simulations",
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_simulation(request: SimulationRequest, response: Response) -> JobAccepted:
        """Queue a simulation and return where to poll for its result.

        Returns 202 immediately. A run takes minutes -- longer than browsers
        and platform proxies keep a request open -- so the result is
        collected from ``GET /simulations/{job_id}``.

        Returns 429 when too much work is already outstanding.
        """
        try:
            job = jobs.submit(lambda: _simulate(request))
        except QueueFullError as error:
            logger.warning("Rejected simulation: %s", error)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(error),
                headers={"Retry-After": str(POLL_INTERVAL_SECONDS)},
            ) from None

        poll_url = f"/simulations/{job.id}"
        response.headers["Location"] = poll_url
        response.headers["Retry-After"] = str(POLL_INTERVAL_SECONDS)
        return JobAccepted(job_id=job.id, poll_url=poll_url)

    @app.get("/simulations/{job_id}", response_model=JobState)
    def get_simulation(job_id: str, response: Response) -> JobState:
        """Report a queued simulation's progress, and its result once done.

        Returns 404 once a finished job's result has expired, so a client
        that stops polling and comes back much later is told plainly rather
        than handed an empty success.
        """
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No such simulation. It may have expired.",
            )

        if not job.is_finished:
            response.headers["Retry-After"] = str(POLL_INTERVAL_SECONDS)
        return _as_state(job)

    @app.post("/run-simulation", deprecated=True)
    def run_simulation_sync(request: SimulationRequest) -> dict[str, Any]:
        """Run a simulation and wait for it. **Deprecated.**

        Kept so a frontend deployed before the queued endpoints keeps working
        during the changeover. It holds the request open for the whole run,
        which is what POST /simulations exists to avoid. Remove it once no
        deployed client calls it.
        """
        logger.info("Deprecated synchronous endpoint called; prefer POST /simulations")
        try:
            job = jobs.submit_and_wait(lambda: _simulate(request))
        except QueueFullError as error:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(error),
                headers={"Retry-After": str(POLL_INTERVAL_SECONDS)},
            ) from None

        if job.result is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Simulation failed.",
            )
        return job.result

    return app


app = create_app()
