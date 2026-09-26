"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Origins always permitted, covering local development and the named
#: production deployments.
DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost:3000",
    "https://pjdsc-drain.vercel.app",
    "https://project-drain.vercel.app",
    "https://ai-drain.vercel.app",
)

#: Vercel gives every preview deployment a unique hostname, so they cannot be
#: enumerated. Match them by pattern -- browsers reject a bare "*" when
#: credentials are allowed.
DEFAULT_ORIGIN_REGEX = r"https://(pjdsc-drain|project-drain|ai-drain|drain)-[a-z0-9-]+\.vercel\.app"


def _env_list(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


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

    #: Refuse simulations from anyone not signed in. Only a local developer
    #: without a Supabase project should turn this off.
    require_auth: bool = True

    #: Runs one person may have queued or running at once.
    max_jobs_per_user: int = 1

    #: Runs one person may start in an hour. With one worker and runs of
    #: about two minutes, ten an hour is a third of the server's time.
    max_runs_per_user_per_hour: int = 10

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            allowed_origins=_env_list("ALLOWED_ORIGINS") or DEFAULT_ALLOWED_ORIGINS,
            origin_regex=os.getenv("ALLOWED_ORIGIN_REGEX", DEFAULT_ORIGIN_REGEX),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            max_concurrent_simulations=int(os.getenv("MAX_CONCURRENT_SIMULATIONS", "1")),
            max_queued_simulations=int(os.getenv("MAX_QUEUED_SIMULATIONS", "8")),
            result_retention_seconds=int(os.getenv("RESULT_RETENTION_SECONDS", "900")),
            max_runtime_seconds=int(os.getenv("MAX_RUNTIME_SECONDS", "1800")),
            max_queue_wait_seconds=int(os.getenv("MAX_QUEUE_WAIT_SECONDS", "3600")),
            supabase_url=os.getenv("SUPABASE_URL") or None,
            supabase_anon_key=os.getenv("SUPABASE_ANON_KEY") or None,
            require_auth=_env_flag("REQUIRE_AUTH", True),
            max_jobs_per_user=int(os.getenv("MAX_JOBS_PER_USER", "1")),
            max_runs_per_user_per_hour=int(os.getenv("MAX_RUNS_PER_USER_PER_HOUR", "10")),
        )


settings = Settings.from_env()
