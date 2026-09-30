"""FastAPI application exposing the drainage simulation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app import isolation
from app.auth import (
    AccountRefusedError,
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
from app.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    client_ip,
)
from app.polling import (
    FINISHED_CACHE_CONTROL,
    FINISHED_VARY,
    RenderedState,
    RenderedStates,
    render,
)
from app.runs import (
    RunRecorder,
    RunRepository,
    RunStoreError,
    SupabaseRunRepository,
    is_uuid,
)
from app.schemas import HealthResponse, JobAccepted, JobState, SimulationRequest
from app.simulation import baseline_result, is_unmodified, simulate, simulate_payload
from drain.network import link_suffixes, node_ids

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
        max_jobs_per_group=config.max_jobs_per_ip,
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
        return SupabaseAuthenticator(
            config.supabase_url,
            config.supabase_anon_key,
            require_confirmed_email=config.require_confirmed_email,
        )
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
    """Run one simulation to completion, in this process.

    The unmodified network is answered from the shared baseline, built once
    from the shipped results; every job asking for it holds that one object
    rather than a 0.7 MB copy of its own.
    """
    if is_unmodified(request):
        return baseline_result()
    return simulate(request)


def _work_for(request: SimulationRequest, config: Settings) -> Callable[[], dict[str, Any]]:
    """What a worker thread runs for this request.

    A real simulation runs in a child process that is killed once it passes
    ``max_runtime_seconds`` (:mod:`app.isolation`), rather than left burning
    a core after the job store gave up on it. The baseline needs no SWMM, so
    it stays in this process, where it is shared.
    """
    if config.isolate_simulations and not is_unmodified(request):
        payload = request.model_dump(exclude_none=True)
        timeout = float(config.max_runtime_seconds)
        return lambda: isolation.run_isolated(simulate_payload, payload, timeout)
    return lambda: _simulate(request)


def _recent_runs(repository: RunRepository | None, owner: str, config: Settings) -> int | None:
    """How many runs the run table says ``owner`` started in the past hour.

    The job store's own count starts from zero after every restart, which
    handed everyone a fresh allowance. ``None`` when there is no table or it
    can't be read in time; the store's own count then stands alone.
    """
    if repository is None or not is_uuid(owner):
        return None
    try:
        return repository.count_recent(
            owner,
            since=datetime.now(UTC) - timedelta(hours=1),
            limit=config.max_runs_per_user_per_hour,
        )
    except RunStoreError:
        logger.warning("Could not count recent runs for %s; using this process's count", owner)
        return None


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


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="No such simulation. It may have expired.",
    )


def _finished_response(finished: RenderedState, request: Request) -> Response:
    """A finished job's response: 304 if the client already has it, else the
    body, gzipped if the client takes that."""
    gzipped = "gzip" in request.headers.get("accept-encoding", "")
    headers = {
        "ETag": finished.etag_for(gzipped),
        "Cache-Control": FINISHED_CACHE_CONTROL,
        "Vary": FINISHED_VARY,
    }
    if finished.matches(request.headers.get("if-none-match")):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    if gzipped:
        headers["Content-Encoding"] = "gzip"
        return Response(finished.gzipped, media_type="application/json", headers=headers)
    return Response(finished.body(), media_type="application/json", headers=headers)


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
    """The signed-in person making the request, or a 401/403/503.

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
    except AccountRefusedError as error:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from None
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
    rendered = RenderedStates(ttl_seconds=config.result_retention_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Set up logging, and settle runs a restart cut short."""
        configure_logging(config.log_level)
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
        isolation.kill_all()
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
        # Off unless ENABLE_DOCS is set: the schema is a map for whoever is
        # probing the API, and the app doesn't read it.
        docs_url="/docs" if config.enable_docs else None,
        redoc_url="/redoc" if config.enable_docs else None,
        openapi_url="/openapi.json" if config.enable_docs else None,
    )
    app.state.authenticator = auth

    # Middleware added last runs first. From the outside in: security
    # headers on everything; CORS, so a browser can read even a refusal
    # made below it; the per-address rate limit and the body size limit,
    # both before a body is read or a sign-in checked; then gzip.
    #
    # A finished run's result is about 0.7 MB of JSON, 40 KB gzipped. It is
    # compressed once (app.polling) and passes through gzip untouched.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=config.max_request_bytes)
    app.add_middleware(
        RateLimitMiddleware,
        submit_per_minute=config.submit_rate_per_minute,
        poll_per_minute=config.poll_rate_per_minute,
        proxy_hops=config.trusted_proxy_hops,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.allowed_origins),
        allow_origin_regex=config.origin_regex or None,
        # No cookies: sign-in travels as a Bearer header, which CORS allows
        # without credentials mode.
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        # Browsers hide response headers from scripts unless they are listed
        # here, and the polling client needs both.
        expose_headers=["Retry-After", "Location"],
    )
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Liveness probe."""
        return HealthResponse(status="ok")

    @app.post(
        "/simulations",
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_simulation(
        request: SimulationRequest,
        response: Response,
        caller: SignedIn,
        http_request: Request,
    ) -> JobAccepted:
        """Queue a simulation and return where to poll for its result.

        Returns 202 immediately. A run takes minutes -- longer than browsers
        and platform proxies keep a request open -- so the result is
        collected from ``GET /simulations/{job_id}``.

        Returns 401 without a valid sign-in, 403 for an account without a
        confirmed email, 413 for a body over the size limit, 422 for an
        override the network can't take, 429 when the caller already has a
        run going or has used their hourly allowance, their address is
        sending too much or has too many runs waiting, or the server is
        full, and 503 while the server is shutting down.
        """
        _reject_unknown_ids(request)
        try:
            job = jobs.submit(
                _work_for(request, config),
                owner=caller.user_id,
                request=request.model_dump(exclude_none=True),
                group=client_ip(http_request.scope, config.trusted_proxy_hops),
                prior_runs=_recent_runs(repository, caller.user_id, config),
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
        http_request: Request,
    ) -> Any:
        """Report a queued simulation's progress, and its result once done.

        Returns 404 once a finished job's result has expired, so a client
        that stops polling and comes back much later is told plainly rather
        than handed an empty success. Someone else's job is a 404 too.

        A finished job's response carries an ETag; sending it back in
        ``If-None-Match`` gets a bodiless 304 instead of the result again.
        """
        finished = rendered.get(job_id)
        if finished is None:
            job = jobs.get(job_id)
            if job is None and repository is not None and not rendered.is_unknown(job_id):
                # Gone from memory -- expired, or a restart -- but recorded.
                try:
                    job = repository.load(job_id)
                except RunStoreError:
                    logger.exception("Could not read simulation %s back from Supabase", job_id)
                else:
                    if job is None:
                        # Asked again within seconds, the answer is the same.
                        rendered.mark_unknown(job_id)
            if job is None or job.owner != caller.user_id:
                raise _not_found()
            if not job.is_finished:
                response.headers["Retry-After"] = str(POLL_INTERVAL_SECONDS)
                return _as_state(job)
            # A finished job never changes, so its response is built once.
            finished = render(job)
            rendered.put(job_id, finished)

        if finished.owner != caller.user_id:
            raise _not_found()
        return _finished_response(finished, http_request)

    return app


app = create_app()
