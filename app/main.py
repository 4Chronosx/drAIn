"""FastAPI application exposing the drainage simulation."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware

from app.auth import (
    Authenticator,
    AuthUnavailableError,
    Caller,
    OpenAuthenticator,
    SupabaseAuthenticator,
)
from app.config import Settings, settings
from app.jobs import (
    JobStore,
    JobStoreClosedError,
    QueueFullError,
    SimulationJob,
    UserLimitError,
)
from app.logging_config import configure_logging
from app.runs import RunRecorder, RunRepository, RunStoreError, SupabaseRunRepository
from app.schemas import HealthResponse, JobAccepted, JobState, SimulationRequest
from drain.flooding import build_flooding_summary
from drain.hazard import DEFAULT_EVENT_HOURS
from drain.network import link_suffixes, node_ids
from drain.swmm_runner import run_simulation
from drain.vulnerability import load_model

logger = logging.getLogger(__name__)

#: Suggested seconds between polls. A run takes minutes, so there is nothing
#: to gain from asking more often than this.
POLL_INTERVAL_SECONDS = 3

#: Suggested wait after someone hits their own limit.
USER_LIMIT_RETRY_SECONDS = 60

#: How many unknown ids a 422 lists before it stops.
MAX_IDS_LISTED = 10

#: What a run caught mid-way by a restart says when it is read back.
RESTARTED_MESSAGE = "The simulation server restarted before this run finished. Please run it again."


def _build_run_repository(config: Settings) -> RunRepository | None:
    """Where runs are recorded durably, or ``None`` to keep them in memory only."""
    if config.supabase_url and config.supabase_service_role_key:
        return SupabaseRunRepository(config.supabase_url, config.supabase_service_role_key)
    logger.warning(
        "SUPABASE_SERVICE_ROLE_KEY is not set: simulation runs are kept in memory only "
        "and are lost when the server restarts."
    )
    return None


def _build_job_store(config: Settings, listener=None) -> JobStore:
    return JobStore(
        max_workers=config.max_concurrent_simulations,
        max_queued=config.max_queued_simulations,
        retention=timedelta(seconds=config.result_retention_seconds),
        max_runtime=timedelta(seconds=config.max_runtime_seconds),
        max_queue_wait=timedelta(seconds=config.max_queue_wait_seconds),
        max_jobs_per_owner=config.max_jobs_per_user,
        max_runs_per_owner_per_hour=config.max_runs_per_user_per_hour,
        listener=listener,
    )


def _build_authenticator(config: Settings) -> Authenticator | None:
    """How callers are checked, or ``None`` if the server can't check them.

    With no way to check, simulations are refused (503) rather than opened
    to everyone: a missing setting should not quietly remove the lock.
    """
    if not config.require_auth:
        logger.warning("REQUIRE_AUTH is off: anyone can run simulations. Local development only.")
        return OpenAuthenticator()
    if config.supabase_url and config.supabase_anon_key:
        return SupabaseAuthenticator(config.supabase_url, config.supabase_anon_key)
    logger.error(
        "SUPABASE_URL and SUPABASE_ANON_KEY are not set, so nobody can be signed in; "
        "simulation requests will be refused."
    )
    return None


def _reject_unknown_ids(request: SimulationRequest) -> None:
    """Refuse overrides for nodes or links the network doesn't have.

    SWMM used to skip them with a log line, so a typo ran a full simulation
    of the unmodified network and returned it as if the change had applied.
    """
    unknown_nodes = sorted(set(request.nodes) - node_ids())
    unknown_links = sorted(set(request.links) - link_suffixes())
    problems = []
    if unknown_nodes:
        problems.append(f"unknown nodes: {', '.join(unknown_nodes[:MAX_IDS_LISTED])}")
    if unknown_links:
        problems.append(f"unknown links: {', '.join(unknown_links[:MAX_IDS_LISTED])}")
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The network has no such " + "; ".join(problems) + ".",
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


def _queue_full(error: QueueFullError) -> HTTPException:
    logger.warning("Rejected simulation: %s", error)
    retry = USER_LIMIT_RETRY_SECONDS if isinstance(error, UserLimitError) else POLL_INTERVAL_SECONDS
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=str(error),
        headers={"Retry-After": str(retry)},
    )


def _shutting_down(error: JobStoreClosedError) -> HTTPException:
    # Only seen while the process is stopping. A client that retries will
    # reach the replacement instance.
    logger.warning("Rejected simulation: %s", error)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=str(error),
        headers={"Retry-After": str(POLL_INTERVAL_SECONDS)},
    )


def current_caller(request: Request) -> Caller:
    """The signed-in person making the request, or a 401/503.

    Reads the app's authenticator from ``app.state`` (set by
    :func:`create_app`), so tests can build an app with their own.
    """
    auth: Authenticator | None = request.app.state.authenticator
    if isinstance(auth, OpenAuthenticator):
        return auth.CALLER
    if auth is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sign-in for simulations is not configured on this server.",
        )

    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in to run simulations.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        caller = auth.authenticate(token.strip())
    except AuthUnavailableError:
        logger.exception("Could not check a caller's sign-in")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not check your sign-in. Try again shortly.",
            headers={"Retry-After": str(POLL_INTERVAL_SECONDS)},
        ) from None
    if caller is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your sign-in has expired. Sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return caller


#: A route parameter that requires a signed-in caller.
SignedIn = Annotated[Caller, Depends(current_caller)]


def create_app(
    config: Settings = settings,
    authenticator: Authenticator | None = None,
    runs: RunRepository | None = None,
) -> FastAPI:
    """Build the application. Kept separate from the module-level instance so
    tests can construct an app with their own settings, sign-in check and
    run record."""
    auth = authenticator if authenticator is not None else _build_authenticator(config)
    repository = runs if runs is not None else _build_run_repository(config)
    recorder = RunRecorder(repository) if repository is not None else None
    jobs = _build_job_store(config, listener=recorder.record if recorder else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Warm the legacy k-means model, and settle runs a restart cut short."""
        configure_logging(config.log_level)
        if load_model() is None:
            # Hazard, exposure and risk do not use this model; only the
            # Legacy_Cluster_* comparison fields do.
            logger.warning(
                "Legacy k-means model unavailable; Legacy_Cluster_* fields will read 'N/A'. "
                "Hazard and risk scores are unaffected."
            )
        if repository is not None:
            # This process has run nothing yet, so any run still marked
            # queued or running belonged to the one before it, and died with
            # it. Assumes one server instance, as the Procfile runs.
            try:
                repository.fail_unfinished(RESTARTED_MESSAGE)
                repository.prune(datetime.now(UTC) - timedelta(days=config.run_retention_days))
            except RunStoreError:
                logger.exception("Could not tidy recorded simulation runs")
        yield
        jobs.shutdown()
        if recorder is not None:
            recorder.close()

    app = FastAPI(
        title="DrAIn simulation API",
        description=(
            "Runs SWMM simulations of the Mandaue drainage network. A run "
            "takes minutes, so simulations are queued and polled rather than "
            "awaited on the request. Every simulation call needs the caller's "
            "Supabase access token as a Bearer token."
        ),
        lifespan=lifespan,
    )
    app.state.authenticator = auth

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.allowed_origins),
        allow_origin_regex=config.origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Browsers hide response headers from scripts unless they are listed
        # here, and the polling client needs both.
        expose_headers=["Retry-After", "Location"],
    )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Liveness probe that also reports whether the legacy k-means model loaded.

        ``vulnerability_model_loaded`` covers only the Legacy_Cluster_* fields.
        Hazard, exposure and risk scoring do not depend on it.
        """
        return HealthResponse(
            status="ok",
            vulnerability_model_loaded=load_model() is not None,
        )

    @app.post(
        "/simulations",
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_simulation(
        request: SimulationRequest,
        response: Response,
        caller: SignedIn,
    ) -> JobAccepted:
        """Queue a simulation and return where to poll for its result.

        Returns 202 immediately. A run takes minutes -- longer than browsers
        and platform proxies keep a request open -- so the result is
        collected from ``GET /simulations/{job_id}``.

        Returns 401 without a valid sign-in, 422 for an override the network
        can't take, 429 when the caller already has a run going or has used
        their hourly allowance, or the server is full, and 503 while the
        server is shutting down.
        """
        _reject_unknown_ids(request)
        try:
            job = jobs.submit(
                lambda: _simulate(request),
                owner=caller.user_id,
                request=request.model_dump(exclude_none=True),
            )
        except QueueFullError as error:
            raise _queue_full(error) from None
        except JobStoreClosedError as error:
            raise _shutting_down(error) from None

        poll_url = f"/simulations/{job.id}"
        response.headers["Location"] = poll_url
        response.headers["Retry-After"] = str(POLL_INTERVAL_SECONDS)
        return JobAccepted(job_id=job.id, poll_url=poll_url)

    @app.get("/simulations/{job_id}", response_model=JobState)
    def get_simulation(
        job_id: str,
        response: Response,
        caller: SignedIn,
    ) -> JobState:
        """Report a queued simulation's progress, and its result once done.

        Returns 404 once a finished job's result has expired, so a client
        that stops polling and comes back much later is told plainly rather
        than handed an empty success. Someone else's job is a 404 too.
        """
        job = jobs.get(job_id)
        if job is None and repository is not None:
            # Gone from memory -- expired, or a restart -- but recorded.
            try:
                job = repository.load(job_id)
            except RunStoreError:
                logger.exception("Could not read simulation %s back from Supabase", job_id)
        if job is None or job.owner != caller.user_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No such simulation. It may have expired.",
            )

        if not job.is_finished:
            response.headers["Retry-After"] = str(POLL_INTERVAL_SECONDS)
        return _as_state(job)

    return app


app = create_app()
