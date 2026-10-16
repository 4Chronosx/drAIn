"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

#: Origins always permitted, covering local development and the named
#: production deployments.
DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost:3000",
    "https://pjdsc-drain.vercel.app",
    "https://project-drain.vercel.app",
    "https://ai-drain.vercel.app",
)

#: Vercel gives every preview deployment a unique hostname, so they cannot be
#: enumerated. Match them by pattern, as tightly as their two real shapes
#: allow:
#:
#:   <project>-<9-character hash>-<team>.vercel.app   one deployment
#:   <project>-git-<branch>-<team>.vercel.app         a branch's latest
#:
#: The pattern used to accept any "drain-*" text before the team slug, so a
#: stranger's project named e.g. "drain-x-kiloumanjaros-projects" got the
#: hostname drain-x-kiloumanjaros-projects.vercel.app and was let in. Now
#: the part between project and team must be a hash or a git- branch.
#: Production hostnames are listed exactly, above, never matched.
#:
#: Off by default: only production may call the API. A stranger can still
#: name a project so its hostname has one of these shapes, so previews are
#: let in only when ALLOWED_ORIGIN_REGEX is set to this (or another) pattern.
VERCEL_TEAM_SLUG = "kiloumanjaros-projects"
VERCEL_PROJECTS = ("pjdsc-drain", "project-drain", "ai-drain", "drain")
VERCEL_PREVIEW_ORIGIN_REGEX = (
    r"https://(?:"
    + "|".join(VERCEL_PROJECTS)
    + r")-(?:[a-z0-9]{9}|git-[a-z0-9]+(?:-[a-z0-9]+)*)-"
    + VERCEL_TEAM_SLUG
    + r"\.vercel\.app"
)
#: No pattern: previews are refused.
DEFAULT_ORIGIN_REGEX = ""

#: The largest request body accepted, in bytes. The biggest real request --
#: every node and every link in the network, each with all four fields set
#: to long floats -- is about 495 KB as compact JSON and 600 KB
#: pretty-printed; this leaves room above that and nothing more.
DEFAULT_MAX_REQUEST_BYTES = 640 * 1024


def _env_list(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _env_flag(name: str, default: bool, environ: Mapping[str, str] = os.environ) -> bool:
    """A switch from the environment; unset or empty means ``default``.

    Anything that is not a known spelling is an error. It used to count as
    on, so ``ENABLE_DOCS=flase`` served the docs.
    """
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(
        f"{name} must be one of true/false, yes/no, on/off or 1/0, not {raw.strip()!r}."
    )


@dataclass(frozen=True)
class Settings:
    """Settings for one process, resolved once at import time."""

    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    origin_regex: str = DEFAULT_ORIGIN_REGEX
    log_level: str = "INFO"

    #: How many SWMM runs may execute at once. A run is CPU-bound and takes
    #: minutes, so letting requests pile up on a small instance makes every
    #: one of them slower. Extra requests queue.
    max_concurrent_simulations: int = 1

    #: How much unfinished work the queue will hold before rejecting new
    #: requests with 429. Without a cap, a burst of callers -- or a bored
    #: one -- can grow a backlog nobody is waiting on any more.
    max_queued_simulations: int = 8

    #: The last places in that queue are kept for people who have started
    #: at most two runs in the past hour, so a few heavy users can't fill it
    #: against everyone else. 0 keeps none back.
    queue_slots_reserved: int = 2

    #: How long a finished job's result stays available to poll for. Each
    #: result is close to a megabyte, so they cannot be kept forever.
    result_retention_seconds: int = 900

    #: A run takes minutes. One still going after this is not coming back,
    #: and is failed so it stops holding a queue slot.
    max_runtime_seconds: int = 1800

    #: A job still waiting to start after this is failed. With one worker
    #: and a full queue the last job waits behind several runs, so this is
    #: generous; it is a backstop, not the normal path.
    max_queue_wait_seconds: int = 3600

    #: The Supabase project the app signs people in with. Simulations need
    #: a signed-in caller, checked against this project's Auth service.
    supabase_url: str | None = None
    #: The project's public (anon / publishable) key, sent with that check.
    supabase_anon_key: str | None = None
    #: The project's service-role key. With it, every run is also recorded in
    #: the simulation_runs table, so results survive a restart. A secret:
    #: set it only in the host's environment, and kept out of the repr so a
    #: logged or printed Settings doesn't carry it.
    supabase_service_role_key: str | None = field(default=None, repr=False)

    #: How long recorded runs are kept in Supabase.
    run_retention_days: int = 7

    #: Recorded runs kept per person; a new run deletes their oldest finished
    #: ones beyond it. A result is close to a megabyte, so ten runs an hour
    #: for a week would otherwise be a gigabyte from one account. 0 keeps
    #: them all until they age out.
    max_stored_runs_per_user: int = 20

    #: Refuse simulations from anyone not signed in. Only a local developer
    #: without a Supabase project should turn this off.
    require_auth: bool = True

    #: Runs one person may have queued or running at once.
    max_jobs_per_user: int = 1

    #: Runs one person may start in an hour. With one worker and runs of
    #: about two minutes, ten an hour is a third of the server's time.
    max_runs_per_user_per_hour: int = 10

    #: Refuse accounts Supabase has not confirmed an email address for,
    #: which includes anonymous sign-ins. Accounts are free, so without it
    #: a handful of throwaway sign-ups could hold every queue slot. Turn
    #: off only if the app signs people in by phone.
    require_confirmed_email: bool = True

    #: Runs one client address may have queued or running at once, across
    #: every account it signs in with. Kept above one for people sharing a
    #: network (an office, a campus).
    max_jobs_per_ip: int = 3

    #: Requests per minute one client address may make, per kind. A poll
    #: every three seconds is 20 a minute, so 120 leaves room for a few
    #: people behind one address. 0 turns a limit off.
    submit_rate_per_minute: int = 6
    poll_rate_per_minute: int = 120

    #: How many proxies in front of the server append to X-Forwarded-For.
    #: The client's address is read that many entries from the right; 0
    #: uses the connection's own address. Behind Render's proxy this must
    #: be 1, or every caller looks like the proxy and shares one limit.
    trusted_proxy_hops: int = 0

    #: The largest request body accepted, in bytes (413 above it).
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES

    #: Run each simulation in a child process, so one that overruns can be
    #: killed rather than abandoned still burning CPU. Tests turn it off to
    #: stub the simulation in-process.
    isolate_simulations: bool = True

    #: Serve /docs, /redoc and /openapi.json. Off in production: the schema
    #: is a map for whoever is probing the API.
    enable_docs: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        config = cls._read_env()
        # The hourly allowance is counted from the stored runs so a restart
        # doesn't reset it. Keeping fewer than an hour's worth deletes the
        # rows that count relies on.
        if 0 < config.max_stored_runs_per_user < config.max_runs_per_user_per_hour:
            raise ValueError(
                f"MAX_STORED_RUNS_PER_USER ({config.max_stored_runs_per_user}) must be at "
                f"least MAX_RUNS_PER_USER_PER_HOUR ({config.max_runs_per_user_per_hour}), "
                "or 0 to keep every run until it ages out."
            )
        return config

    @classmethod
    def _read_env(cls) -> Settings:
        return cls(
            allowed_origins=_env_list("ALLOWED_ORIGINS") or DEFAULT_ALLOWED_ORIGINS,
            origin_regex=os.getenv("ALLOWED_ORIGIN_REGEX", DEFAULT_ORIGIN_REGEX),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            max_concurrent_simulations=int(os.getenv("MAX_CONCURRENT_SIMULATIONS", "1")),
            max_queued_simulations=int(os.getenv("MAX_QUEUED_SIMULATIONS", "8")),
            queue_slots_reserved=int(os.getenv("QUEUE_SLOTS_RESERVED", "2")),
            result_retention_seconds=int(os.getenv("RESULT_RETENTION_SECONDS", "900")),
            max_runtime_seconds=int(os.getenv("MAX_RUNTIME_SECONDS", "1800")),
            max_queue_wait_seconds=int(os.getenv("MAX_QUEUE_WAIT_SECONDS", "3600")),
            supabase_url=os.getenv("SUPABASE_URL") or None,
            supabase_anon_key=os.getenv("SUPABASE_ANON_KEY") or None,
            supabase_service_role_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY") or None,
            run_retention_days=int(os.getenv("RUN_RETENTION_DAYS", "7")),
            max_stored_runs_per_user=int(os.getenv("MAX_STORED_RUNS_PER_USER", "20")),
            require_auth=_env_flag("REQUIRE_AUTH", True),
            max_jobs_per_user=int(os.getenv("MAX_JOBS_PER_USER", "1")),
            max_runs_per_user_per_hour=int(os.getenv("MAX_RUNS_PER_USER_PER_HOUR", "10")),
            require_confirmed_email=_env_flag("REQUIRE_CONFIRMED_EMAIL", True),
            max_jobs_per_ip=int(os.getenv("MAX_JOBS_PER_IP", "3")),
            submit_rate_per_minute=int(os.getenv("SUBMIT_RATE_LIMIT_PER_MINUTE", "6")),
            poll_rate_per_minute=int(os.getenv("POLL_RATE_LIMIT_PER_MINUTE", "120")),
            trusted_proxy_hops=int(os.getenv("TRUSTED_PROXY_HOPS", "0")),
            max_request_bytes=int(os.getenv("MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES))),
            isolate_simulations=_env_flag("ISOLATE_SIMULATIONS", True),
            enable_docs=_env_flag("ENABLE_DOCS", False),
        )


def deployment_problems(config: Settings, environ: Mapping[str, str]) -> list[str]:
    """What is unsafe about these settings for where the server is running.

    The defaults suit a developer's machine, and nothing stopped them, or a
    setting meant for one, from reaching the deployed server: it started
    and served, open or with every caller sharing one rate limit. Render
    sets ``RENDER`` in every service's environment; anywhere else there is
    nothing to check against, and this is empty.

    It also covers settings that leave the server up and useless: it
    started, passed its health check, and refused every simulation.

    A missing ``SUPABASE_SERVICE_ROLE_KEY`` is not one. The server works
    without it, keeping runs in memory only, and warns of that when it
    is built (app.main).
    """
    if not environ.get("RENDER", "").strip():
        return []
    problems = []
    if not config.require_auth:
        problems.append("REQUIRE_AUTH is off, so anyone can run simulations.")
    if config.trusted_proxy_hops < 1:
        problems.append(
            "TRUSTED_PROXY_HOPS is 0, so every caller is seen as Render's proxy and "
            "they all share one rate limit. Set it to 1."
        )
    if config.require_auth:
        missing = [
            name
            for name, value in (
                ("SUPABASE_URL", config.supabase_url),
                ("SUPABASE_ANON_KEY", config.supabase_anon_key),
            )
            if not value
        ]
        if missing:
            problems.append(
                f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set, "
                "so nobody can be signed in and every simulation is refused."
            )
    if not config.require_confirmed_email:
        problems.append(
            "REQUIRE_CONFIRMED_EMAIL is off, so throwaway and anonymous accounts can "
            "run simulations."
        )
    # A rate limit of 0 is off (app.middleware).
    for name, per_minute in (
        ("SUBMIT_RATE_LIMIT_PER_MINUTE", config.submit_rate_per_minute),
        ("POLL_RATE_LIMIT_PER_MINUTE", config.poll_rate_per_minute),
    ):
        if per_minute < 1:
            problems.append(f"{name} is {per_minute}, which turns that rate limit off.")
    # A cap of 0 is not off: nobody is ever under it, so every run is
    # refused (app.jobs).
    for name, cap in (
        ("MAX_JOBS_PER_USER", config.max_jobs_per_user),
        ("MAX_RUNS_PER_USER_PER_HOUR", config.max_runs_per_user_per_hour),
        ("MAX_JOBS_PER_IP", config.max_jobs_per_ip),
    ):
        if cap < 1:
            problems.append(f"{name} is {cap}, so every simulation is refused.")
    return problems


def insecure_deploy_allowed(environ: Mapping[str, str]) -> bool:
    """Whether ``ALLOW_INSECURE_DEPLOY`` says to start despite such problems."""
    return _env_flag("ALLOW_INSECURE_DEPLOY", False, environ)


settings = Settings.from_env()
