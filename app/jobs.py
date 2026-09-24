"""In-process job store for long-running simulations.

A SWMM run takes minutes, which is far longer than a browser or a platform
proxy will hold a request open. Callers start a job and poll for it instead.

Jobs live in this process only. That is deliberate for a single-worker
deployment -- it needs no Redis or database -- but it means the server must
run one worker. With more, a poll can land on a process that has never heard
of the job. See the note in the README before scaling out.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED})


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class SimulationJob:
    """One queued or completed simulation."""

    id: str
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: The flooding summary, once the job succeeds.
    result: dict[str, Any] | None = None
    #: A message safe to show the caller, once the job fails.
    error: str | None = None

    @property
    def is_finished(self) -> bool:
        return self.status in TERMINAL_STATUSES


class QueueFullError(RuntimeError):
    """Raised when too much work is already outstanding to accept more."""


class JobStore:
    """Runs simulations on a small thread pool and remembers the results.

    Finished jobs are kept for ``retention`` so a client that polls slowly
    still sees the outcome, then dropped: each result is close to a megabyte
    of JSON, so holding them indefinitely would leak the process's memory.
    """

    def __init__(
        self,
        max_workers: int,
        max_queued: int,
        retention: timedelta,
        max_runtime: timedelta = timedelta(minutes=30),
    ) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="simulation"
        )
        self._max_queued = max_queued
        self._retention = retention
        self._max_runtime = max_runtime
        self._jobs: dict[str, SimulationJob] = {}
        self._lock = threading.Lock()

    def submit(self, work: Callable[[], dict[str, Any]]) -> SimulationJob:
        """Queue a simulation, or raise :class:`QueueFullError`.

        The cap counts work that has not finished yet. It is what stops a
        burst of requests from growing an unbounded backlog that nobody is
        still waiting on.
        """
        job, _ = self._enqueue(work)
        return job

    def submit_and_wait(self, work: Callable[[], dict[str, Any]]) -> SimulationJob:
        """Queue a simulation and block until it finishes.

        Only for the deprecated synchronous endpoint. Going through the same
        pool means it shares the worker limit and the queue cap rather than
        running unbounded alongside the queued ones.
        """
        job, future = self._enqueue(work)
        future.result()
        return job

    def _enqueue(self, work: Callable[[], dict[str, Any]]) -> tuple[SimulationJob, Future[None]]:
        with self._lock:
            self._drop_expired()
            outstanding = sum(1 for job in self._jobs.values() if not job.is_finished)
            if outstanding >= self._max_queued:
                raise QueueFullError(f"{outstanding} simulations are already queued or running.")

            job = SimulationJob(id=uuid.uuid4().hex)
            self._jobs[job.id] = job

        future = self._executor.submit(self._run, job, work)
        logger.info("Queued simulation %s", job.id)
        return job, future

    def get(self, job_id: str) -> SimulationJob | None:
        """Look a job up, or ``None`` if it never existed or has expired."""
        with self._lock:
            self._drop_expired()
            return self._jobs.get(job_id)

    def _run(self, job: SimulationJob, work: Callable[[], dict[str, Any]]) -> None:
        with self._lock:
            job.status = JobStatus.RUNNING
            job.started_at = _now()
        logger.info("Running simulation %s", job.id)

        try:
            result = work()
        except BaseException as error:
            # BaseException, not Exception: anything that leaves a job stuck
            # in RUNNING holds a queue slot for the life of the process,
            # because only finished jobs are ever reaped.
            with self._lock:
                job.status = JobStatus.FAILED
                job.finished_at = _now()
                job.error = str(error) or error.__class__.__name__
            logger.exception("Simulation %s failed", job.id)
            if isinstance(error, KeyboardInterrupt | SystemExit):
                raise
            return

        with self._lock:
            job.status = JobStatus.SUCCEEDED
            job.finished_at = _now()
            job.result = result
        logger.info(
            "Simulation %s finished in %.1fs",
            job.id,
            (job.finished_at - job.started_at).total_seconds(),
        )

    def _drop_expired(self) -> None:
        """Reap finished and abandoned jobs. Caller holds the lock."""
        now = _now()

        # A job still running long past any plausible simulation is not
        # coming back -- the worker hung, or died without unwinding. Left
        # alone it would hold its queue slot for the life of the process.
        abandoned = [
            job
            for job in self._jobs.values()
            if job.status is JobStatus.RUNNING
            and job.started_at is not None
            and now - job.started_at > self._max_runtime
        ]
        for job in abandoned:
            job.status = JobStatus.FAILED
            job.finished_at = now
            job.error = "The simulation did not finish in time and was abandoned."
            logger.error("Simulation %s exceeded %s; abandoned", job.id, self._max_runtime)

        cutoff = now - self._retention
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job.finished_at is not None and job.finished_at < cutoff
        ]
        for job_id in expired:
            del self._jobs[job_id]
        if expired:
            logger.debug("Dropped %d expired simulation results", len(expired))

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
