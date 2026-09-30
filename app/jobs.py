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
import re
import threading
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


#: An absolute path, Windows or POSIX: a server detail, not the caller's
#: business. The file name alone still says what went wrong.
_PATH = re.compile(r"(?:[A-Za-z]:)?[\\/](?:[^\s\\/:*?\"<>|]+[\\/])+(?=[^\s\\/]+)")


def public_message(error: BaseException) -> str:
    """What a failed job tells its caller (and the stored run): the error's
    text with any server paths cut to the file name, or its type if it has
    no text. The full error, paths included, goes to the log."""
    text = _PATH.sub("", str(error)).strip()
    return text or error.__class__.__name__


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
    #: Who asked for it. Only they may read it back.
    owner: str | None = None
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: The flooding summary, once the job succeeds.
    result: dict[str, Any] | None = None
    #: A message safe to show the caller, once the job fails.
    error: str | None = None
    #: What was asked for, kept so the run's durable record can say.
    request: dict[str, Any] | None = None

    @property
    def is_finished(self) -> bool:
        return self.status in TERMINAL_STATUSES


class QueueFullError(RuntimeError):
    """Raised when too much work is already outstanding to accept more."""


class UserLimitError(QueueFullError):
    """Raised when one person already has as much work as they may have."""


class JobStoreClosedError(RuntimeError):
    """Raised when work is submitted after the store has shut down."""


#: The window the per-person run allowance is counted over.
_RUN_WINDOW = timedelta(hours=1)


@dataclass(eq=False)
class _Entry:
    """A job plus what the store needs to run it."""

    job: SimulationJob
    #: The simulation to run. Dropped once the job starts or finishes, so a
    #: finished job does not keep its request alive.
    work: Callable[[], dict[str, Any]] | None
    future: Future[None] | None = None


class JobStore:
    """Runs simulations on a small thread pool and remembers the results.

    Finished results are kept by reference: jobs for the unmodified network
    all hold the one shared baseline (:func:`app.simulation.baseline_result`)
    rather than a copy each.

    Finished jobs are kept for ``retention`` so a client that polls slowly
    still sees the outcome, then dropped: each result is close to a megabyte
    of JSON, so holding them indefinitely would leak the process's memory.

    A thread cannot be killed, so a run that hangs keeps its worker. When one
    passes ``max_runtime`` the store fails it and moves to a fresh pool,
    taking the jobs still queued with it; the hung thread is left to finish
    or not on its own, and whatever it returns is ignored. Without that, one
    hung run held the only worker and everything behind it waited forever.
    The server runs each SWMM run in a child process (:mod:`app.isolation`)
    that is killed at the same limit, so in practice the thread is freed too.
    """

    def __init__(
        self,
        max_workers: int,
        max_queued: int,
        retention: timedelta,
        max_runtime: timedelta = timedelta(minutes=30),
        max_queue_wait: timedelta = timedelta(hours=1),
        max_jobs_per_owner: int | None = None,
        max_runs_per_owner_per_hour: int | None = None,
        listener: Callable[[SimulationJob], None] | None = None,
        max_jobs_per_group: int | None = None,
    ) -> None:
        self._max_workers = max_workers
        self._executor = self._new_executor()
        self._max_queued = max_queued
        self._retention = retention
        self._max_runtime = max_runtime
        self._max_queue_wait = max_queue_wait
        self._entries: dict[str, _Entry] = {}
        # One slow user shouldn't be able to fill the queue for everyone.
        self._max_jobs_per_owner = max_jobs_per_owner
        self._max_runs_per_owner_per_hour = max_runs_per_owner_per_hour
        self._recent_runs: dict[str, deque[datetime]] = {}
        # Nor should one address cycling through accounts. A job's group is
        # the address that submitted it.
        self._max_jobs_per_group = max_jobs_per_group
        self._groups: dict[str, str] = {}
        # Told of every change (queued, started, finished), with a snapshot.
        # Called while holding the lock, so it must not block.
        self._listener = listener
        self._closed = False
        self._lock = threading.Lock()

    def _new_executor(self) -> ThreadPoolExecutor:
        return ThreadPoolExecutor(max_workers=self._max_workers, thread_name_prefix="simulation")

    def submit(
        self,
        work: Callable[[], dict[str, Any]],
        owner: str | None = None,
        request: dict[str, Any] | None = None,
        group: str | None = None,
        prior_runs: int | None = None,
    ) -> SimulationJob:
        """Queue a simulation, or raise :class:`QueueFullError`.

        The cap counts work that has not finished yet. It is what stops a
        burst of requests from growing an unbounded backlog that nobody is
        still waiting on. An ``owner`` is also held to their own allowance
        (:class:`UserLimitError`), and a ``group`` to its share of the queue.

        ``prior_runs`` is how many runs the owner started in the past hour
        by an outside count (the run table). This process forgets its own
        count when it restarts, so the larger of the two is used.
        """
        _, accepted = self._enqueue(work, owner, request, group, prior_runs)
        return accepted

    def get(self, job_id: str) -> SimulationJob | None:
        """Look a job up, or ``None`` if it never existed or has expired.

        Returns a copy taken under the lock. The worker thread keeps updating
        the live job, so a caller reading that one field by field could see
        ``succeeded`` before the result had been stored.
        """
        with self._lock:
            self._drop_expired()
            entry = self._entries.get(job_id)
            return replace(entry.job) if entry is not None else None

    def outstanding(self) -> int:
        """How many jobs are queued or running."""
        with self._lock:
            return self._outstanding()

    def shutdown(self) -> None:
        """Stop accepting work and fail whatever has not started.

        Jobs already running are left to finish; their threads cannot be
        stopped.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for entry in self._entries.values():
                if entry.job.status is JobStatus.QUEUED:
                    self._finish(entry, error="The server shut down before the simulation ran.")
            executor = self._executor
        executor.shutdown(wait=False, cancel_futures=True)

    def _enqueue(
        self,
        work: Callable[[], dict[str, Any]],
        owner: str | None = None,
        request: dict[str, Any] | None = None,
        group: str | None = None,
        prior_runs: int | None = None,
    ) -> tuple[_Entry, SimulationJob]:
        """Queue work. Returns its entry and a snapshot of the job as accepted."""
        with self._lock:
            if self._closed:
                raise JobStoreClosedError("The simulation service is shutting down.")
            self._drop_expired()
            if owner is not None:
                self._check_owner_allowance(owner, prior_runs or 0)
            if group is not None:
                self._check_group_allowance(group)
            outstanding = self._outstanding()
            if outstanding >= self._max_queued:
                raise QueueFullError(f"{outstanding} simulations are already queued or running.")

            entry = _Entry(
                job=SimulationJob(id=str(uuid.uuid4()), owner=owner, request=request),
                work=work,
            )
            if owner is not None:
                self._recent_runs.setdefault(owner, deque()).append(entry.job.created_at)
            self._entries[entry.job.id] = entry
            if group is not None:
                self._groups[entry.job.id] = group
            accepted = replace(entry.job)
            # Before dispatch, so "queued" is reported before "running".
            self._notify(entry.job)
            self._dispatch(entry)

        logger.info("Queued simulation %s", entry.job.id)
        return entry, accepted

    def _outstanding(self) -> int:
        """Caller holds the lock."""
        return sum(1 for entry in self._entries.values() if not entry.job.is_finished)

    def _check_group_allowance(self, group: str) -> None:
        """Refuse a group at its limit. Caller holds the lock."""
        if self._max_jobs_per_group is None:
            return
        held = sum(
            1
            for job_id, member in self._groups.items()
            if member == group and not self._entries[job_id].job.is_finished
        )
        if held >= self._max_jobs_per_group:
            raise UserLimitError(
                "Too many simulations are already queued or running from your network. "
                "Wait for one to finish."
            )

    def _check_owner_allowance(self, owner: str, prior_runs: int = 0) -> None:
        """Refuse an owner at their limit. Caller holds the lock."""
        if self._max_jobs_per_owner is not None:
            mine = sum(
                1
                for entry in self._entries.values()
                if entry.job.owner == owner and not entry.job.is_finished
            )
            if mine >= self._max_jobs_per_owner:
                raise UserLimitError(
                    "You already have a simulation queued or running. "
                    "Wait for it to finish before starting another."
                )

        if self._max_runs_per_owner_per_hour is not None:
            recent = self._recent_runs.get(owner)
            cutoff = _now() - _RUN_WINDOW
            while recent and recent[0] < cutoff:
                recent.popleft()
            if recent is not None and not recent:
                del self._recent_runs[owner]
            started = max(len(recent or ()), prior_runs)
            if started >= self._max_runs_per_owner_per_hour:
                raise UserLimitError(
                    f"You have started {started} simulations in the past hour, "
                    "the most allowed. Try again later."
                )

    def _notify(self, job: SimulationJob) -> None:
        """Tell the listener about a change. Caller holds the lock."""
        if self._listener is None:
            return
        try:
            self._listener(replace(job))
        except Exception:
            logger.exception("Job listener failed for simulation %s", job.id)

    def _dispatch(self, entry: _Entry) -> None:
        """Hand a queued job to the current pool. Caller holds the lock."""
        entry.future = self._executor.submit(self._run, entry)

    def _run(self, entry: _Entry) -> None:
        job = entry.job
        with self._lock:
            # A job that timed out in the queue, or was failed by shutdown,
            # must not run once a worker finally reaches it.
            if job.status is not JobStatus.QUEUED or entry.work is None:
                return
            work, entry.work = entry.work, None
            job.status = JobStatus.RUNNING
            job.started_at = _now()
            self._notify(job)
        logger.info("Running simulation %s", job.id)

        try:
            result = work()
        except BaseException as error:
            # BaseException, not Exception: anything that leaves a job stuck
            # in RUNNING holds a queue slot for the life of the process,
            # because only finished jobs are ever reaped.
            with self._lock:
                recorded = self._finish(entry, error=public_message(error))
            if recorded:
                logger.exception("Simulation %s failed", job.id)
            else:
                logger.warning("Simulation %s failed after it was abandoned: %s", job.id, error)
            if isinstance(error, KeyboardInterrupt | SystemExit):
                raise
            return

        with self._lock:
            recorded = self._finish(entry, result=result)
            elapsed = (_now() - job.started_at).total_seconds() if job.started_at else 0.0
        if recorded:
            logger.info("Simulation %s finished in %.1fs", job.id, elapsed)
        else:
            logger.warning(
                "Simulation %s returned after %.1fs, but was already abandoned; "
                "discarding its result",
                job.id,
                elapsed,
            )

    def _finish(
        self,
        entry: _Entry,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        """Record a job's outcome, unless it already has one. Caller holds the lock.

        A job can be settled twice: the reaper fails a job that ran too long,
        and then the thread it abandoned may still return. The first outcome
        stands, so a poll never sees a timed-out job flip to succeeded.
        Returns whether this outcome was the one recorded.
        """
        job = entry.job
        if job.is_finished:
            return False
        job.status = JobStatus.FAILED if error is not None else JobStatus.SUCCEEDED
        job.finished_at = _now()
        job.result = None if error is not None else result
        job.error = error
        entry.work = None
        self._notify(job)
        if entry.future is not None:
            # Only takes effect while the job is still waiting for a worker.
            entry.future.cancel()
        return True

    def _drop_expired(self) -> None:
        """Reap finished and abandoned jobs. Caller holds the lock."""
        now = _now()

        # A job left waiting this long is behind work that is not moving, or
        # has outlived any caller still polling for it.
        stale = [
            entry
            for entry in self._entries.values()
            if entry.job.status is JobStatus.QUEUED
            and now - entry.job.created_at > self._max_queue_wait
        ]
        for entry in stale:
            self._finish(
                entry, error="The simulation waited too long in the queue and was dropped."
            )
            logger.error(
                "Simulation %s waited more than %s to start; dropped",
                entry.job.id,
                self._max_queue_wait,
            )

        # A job still running long past any plausible simulation is not
        # coming back -- the worker hung, or died without unwinding. Left
        # alone it would hold its queue slot for the life of the process.
        abandoned = [
            entry
            for entry in self._entries.values()
            if entry.job.status is JobStatus.RUNNING
            and entry.job.started_at is not None
            and now - entry.job.started_at > self._max_runtime
        ]
        for entry in abandoned:
            self._finish(entry, error="The simulation did not finish in time and was abandoned.")
            logger.error("Simulation %s exceeded %s; abandoned", entry.job.id, self._max_runtime)
        if abandoned and not self._closed:
            self._replace_executor()

        cutoff = now - self._retention
        expired = [
            job_id
            for job_id, entry in self._entries.items()
            if entry.job.finished_at is not None and entry.job.finished_at < cutoff
        ]
        for job_id in expired:
            del self._entries[job_id]
            self._groups.pop(job_id, None)
        if expired:
            logger.debug("Dropped %d expired simulation results", len(expired))

    def _replace_executor(self) -> None:
        """Move queued work to a fresh pool. Caller holds the lock.

        The abandoned thread still occupies a worker in the old pool, and
        would keep every job queued there from ever starting. A job the old
        pool has already begun cannot be cancelled and is left to run there.
        """
        old = self._executor
        self._executor = self._new_executor()
        moved = 0
        for entry in self._entries.values():
            if (
                entry.job.status is JobStatus.QUEUED
                and entry.future is not None
                and entry.future.cancel()
            ):
                self._dispatch(entry)
                moved += 1
        old.shutdown(wait=False)
        logger.warning(
            "Replaced the simulation pool after a run was abandoned; moved %d queued job(s)",
            moved,
        )
