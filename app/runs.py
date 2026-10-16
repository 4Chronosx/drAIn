"""A durable record of simulation runs, kept in Supabase.

The job store (:mod:`app.jobs`) holds runs in this process's memory, which
is gone after a restart -- frequent on a free-tier host -- and after the
retention window. Each run is also written to the ``simulation_runs`` table
as it moves along, and read back from there when memory no longer has it.
So a result the app was told it could collect is still there to collect.

Writes use the service role (the table accepts no client writes) and go
through a single background thread, in order, so a slow database never
holds up a request or a simulation. A failed write is logged and dropped:
the run itself matters more than its record.

The same thread keeps the table bounded while the server stays up: each new
run trims its owner's rows to the newest few, and once an hour runs past
the retention window are deleted, whether or not anything was written in
that hour. Start-up used to be the only time anything was deleted, so a
server that never restarted never cleaned up.

The table is defined in the frontend repository's
``supabase/schemas/schema_ops.sql``.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.jobs import JobStatus, SimulationJob
from app.simulation import BASELINE_MARKER, baseline_result, is_baseline
from drain.model_info import network_sha256

logger = logging.getLogger(__name__)

TABLE_PATH = "/rest/v1/simulation_runs"


class RunStoreError(RuntimeError):
    """Raised when the run table could not be read or written."""


class RunRepository(Protocol):
    def save(self, job: SimulationJob) -> None:
        """Insert or update a run's row."""

    def load(self, job_id: str) -> SimulationJob | None:
        """A stored run, or ``None`` if there is none."""

    def fail_unfinished(self, reason: str) -> None:
        """Mark every queued or running run failed (after a restart)."""

    def prune(self, older_than: datetime) -> None:
        """Delete runs created before ``older_than``."""

    def count_recent(self, owner: str, since: datetime, limit: int) -> int:
        """How many runs ``owner`` created since ``since``, counting to ``limit``."""

    def trim_owner(self, owner: str, keep: int) -> None:
        """Delete ``owner``'s finished runs beyond their newest ``keep``."""


#: Takes (method, url, headers, body, timeout) and returns (HTTP status, body).
Fetch = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]

#: Seconds a write or clean-up may take. They run off the request path.
WRITE_TIMEOUT_SECONDS = 30.0

#: Seconds reading a run back may take. A poll waits on it.
LOAD_TIMEOUT_SECONDS = 5.0

#: Seconds the count behind a new run may take. A request waits on it, so
#: past this the server counts from memory instead.
COUNT_TIMEOUT_SECONDS = 3.0


#: The most rows one trim deletes, which bounds the length of its request.
#: An owner further over than this is brought down over their next runs.
TRIM_BATCH = 100

#: Seconds between prunes by age while the server is up.
PRUNE_INTERVAL_SECONDS = 3600.0


def _urllib_fetch(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise RunStoreError(f"Could not reach Supabase: {error}") from error


def is_uuid(value: str | None) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _compact(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """The result as stored.

    Without ``nodes_dict``, which repeats ``nodes_list``: the API no longer
    sends it (app.simulation.served); this still keeps it out of the table
    if a result ever carries it. And a run of the unmodified network stores
    :data:`BASELINE_MARKER` rather than its own copy of the same 0.7 MB.
    """
    if result is None:
        return None
    if is_baseline(result):
        return BASELINE_MARKER
    return {key: value for key, value in result.items() if key != "nodes_dict"}


def _restored(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """A stored result as the API sends it: the marker becomes the baseline."""
    return baseline_result() if result == BASELINE_MARKER else result


@dataclass
class SupabaseRunRepository:
    """Reads and writes ``simulation_runs`` through Supabase's REST API."""

    supabase_url: str
    #: Out of the repr, which ends up in logs and tracebacks.
    service_role_key: str = field(repr=False)
    fetch: Fetch = _urllib_fetch

    def _call(
        self,
        method: str,
        query: str = "",
        body: Any = None,
        prefer: str | None = None,
        timeout: float = WRITE_TIMEOUT_SECONDS,
    ) -> bytes:
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        url = f"{self.supabase_url.rstrip('/')}{TABLE_PATH}{query}"
        payload = json.dumps(body).encode() if body is not None else None
        status, response = self.fetch(method, url, headers, payload, timeout)
        if not 200 <= status < 300:
            raise RunStoreError(
                f"{method} simulation_runs answered HTTP {status}: {response[:200]!r}"
            )
        return response

    def save(self, job: SimulationJob) -> None:
        row: dict[str, Any] = {
            "id": job.id,
            "user_id": job.owner,
            "status": job.status.value,
            "created_at": _iso(job.created_at),
            "started_at": _iso(job.started_at),
            "finished_at": _iso(job.finished_at),
            "error": job.error,
            "result": _compact(job.result),
            # Which network file the run used (drain/model_info.py), so old
            # runs can be told apart after the model changes.
            "model_version": network_sha256(),
        }
        if job.request is not None:
            row["request"] = job.request
        # An upsert, so the columns sent replace the stored ones and the
        # request, sent only when queued, is kept.
        self._call(
            "POST",
            "?on_conflict=id",
            row,
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def load(self, job_id: str) -> SimulationJob | None:
        if not is_uuid(job_id):
            return None
        query = "?" + urllib.parse.urlencode({"id": f"eq.{job_id}", "select": "*"})
        rows = json.loads(self._call("GET", query, timeout=LOAD_TIMEOUT_SECONDS) or b"[]")
        if not rows:
            return None
        row = rows[0]
        return SimulationJob(
            id=job_id,
            owner=row["user_id"],
            status=JobStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            finished_at=(
                datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
            ),
            result=_restored(row["result"]),
            error=row["error"],
        )

    def fail_unfinished(self, reason: str) -> None:
        self._call(
            "PATCH",
            "?status=in.(queued,running)",
            {"status": "failed", "error": reason, "finished_at": _iso(datetime.now(UTC))},
            prefer="return=minimal",
        )

    def prune(self, older_than: datetime) -> None:
        query = "?" + urllib.parse.urlencode({"created_at": f"lt.{older_than.isoformat()}"})
        self._call("DELETE", query, prefer="return=minimal")

    def count_recent(self, owner: str, since: datetime, limit: int) -> int:
        if not is_uuid(owner):
            return 0
        query = "?" + urllib.parse.urlencode(
            {
                "select": "id",
                "user_id": f"eq.{owner}",
                "created_at": f"gte.{since.isoformat()}",
                "limit": str(limit),
            }
        )
        rows = json.loads(self._call("GET", query, timeout=COUNT_TIMEOUT_SECONDS) or b"[]")
        return len(rows)

    def trim_owner(self, owner: str, keep: int) -> None:
        if not is_uuid(owner):
            return
        beyond = "?" + urllib.parse.urlencode(
            {
                "select": "id",
                "user_id": f"eq.{owner}",
                "order": "created_at.desc",
                "offset": str(keep),
                "limit": str(TRIM_BATCH),
            }
        )
        rows = json.loads(self._call("GET", beyond) or b"[]")
        ids = [row["id"] for row in rows if is_uuid(row.get("id"))]
        if not ids:
            return
        # Never a run still queued or running, however far down the list:
        # its row is about to be written to again.
        query = "?" + urllib.parse.urlencode(
            {
                "id": f"in.({','.join(ids)})",
                "user_id": f"eq.{owner}",
                "status": "in.(succeeded,failed)",
            },
            safe="(),",
        )
        self._call("DELETE", query, prefer="return=minimal")


_STOP = object()

#: Snapshots waiting to be written. A finished one carries its whole result
#: (about a megabyte), so a long Supabase outage mustn't grow this without
#: limit.
MAX_PENDING_WRITES = 256


class RunRecorder:
    """Writes run changes to a repository on one background thread, in order.

    The job store calls :meth:`record` with a snapshot each time a run is
    queued, starts or finishes, sometimes while holding its lock, so this
    only puts the snapshot on a queue.

    With ``max_runs_per_owner``, each newly queued run trims its owner's
    rows to that many; with ``retention``, runs older than it are deleted
    once an hour. Both happen on the same thread. The prune used to wait
    for a new run to prompt it, so an idle server kept rows past their
    retention until its next run or restart; the thread now wakes for it.
    """

    def __init__(
        self,
        repository: RunRepository,
        max_pending: int = MAX_PENDING_WRITES,
        *,
        retention: timedelta | None = None,
        max_runs_per_owner: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        prune_interval: float = PRUNE_INTERVAL_SECONDS,
    ) -> None:
        self._repository = repository
        self._retention = retention
        self._max_runs_per_owner = max_runs_per_owner
        self._clock = clock
        self._prune_interval = prune_interval
        # Start-up prunes once itself (app.main), so the first prune from
        # here is an hour after that. Only the writer thread reads this.
        self._pruned_at = clock()
        self._queue: queue.Queue[object] = queue.Queue(maxsize=max_pending)
        self._closed = False
        self._thread = threading.Thread(target=self._drain, name="run-recorder", daemon=True)
        self._thread.start()

    def record(self, job: SimulationJob) -> None:
        # Runs by someone who isn't a real account (REQUIRE_AUTH=false) have
        # nobody to belong to in the table.
        if not is_uuid(job.owner):
            return
        # The request is written once, with the queued row.
        if job.status is not JobStatus.QUEUED:
            job = replace(job, request=None)
        if self._closed:
            logger.warning(
                "Simulation %s became %s after shutdown began; not recorded", job.id, job.status
            )
            return
        # Never block: the job store may be holding its lock.
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            logger.error(
                "Run recorder is %d writes behind; dropped simulation %s (%s). "
                "If it was unfinished, the next startup marks it failed.",
                self._queue.maxsize,
                job.id,
                job.status,
            )

    def flush(self, timeout: float = 10.0) -> None:
        """Wait until everything recorded so far has been written."""
        done = threading.Event()
        self._queue.put(done, timeout=timeout)
        done.wait(timeout)

    def close(self, timeout: float = 10.0) -> None:
        """Write everything recorded so far, then stop.

        Waits up to ``timeout`` seconds. Writes still pending after that are
        lost with the process, and logged; runs they left looking unfinished
        are marked failed by the next startup.
        """
        self._closed = True
        try:
            self._queue.put(_STOP, timeout=timeout)
        except queue.Full:
            logger.error(
                "Run recorder still %d writes behind at shutdown; they were not saved",
                self._queue.qsize(),
            )
            return
        self._thread.join(timeout)
        if self._thread.is_alive():
            logger.error(
                "Run recorder did not finish within %.0f s at shutdown; "
                "about %d writes were not saved",
                timeout,
                self._queue.qsize(),
            )

    def _until_prune(self) -> float | None:
        """Seconds the writer may sleep before a prune is due, or ``None``
        if one never is."""
        if self._retention is None:
            return None
        return max(0.0, self._prune_interval - (self._clock() - self._pruned_at))

    def _drain(self) -> None:
        while True:
            # Sleeps until there is something to write or a prune is due,
            # whichever comes first. A prune stamps the time whether or not
            # it worked, so the wait after one is a whole interval and this
            # never spins.
            try:
                item = self._queue.get(timeout=self._until_prune())
            except queue.Empty:
                self._prune_if_due()
                continue
            if item is _STOP:
                return
            if isinstance(item, threading.Event):
                item.set()
                continue
            assert isinstance(item, SimulationJob)
            try:
                self._repository.save(item)
            except RunStoreError as error:
                # One line, no traceback: during an outage every write lands
                # here. Still an error: this run's record is lost.
                logger.error("Could not record simulation %s (%s): %s", item.id, item.status, error)
                continue
            except Exception:
                logger.exception("Could not record simulation %s (%s)", item.id, item.status)
                continue
            if item.status is JobStatus.QUEUED:
                self._trim(item)
            self._prune_if_due()

    def _trim(self, job: SimulationJob) -> None:
        """Keep ``job``'s owner to their newest runs, now that it added one."""
        if self._max_runs_per_owner is None or job.owner is None:
            return
        try:
            self._repository.trim_owner(job.owner, self._max_runs_per_owner)
        except Exception:
            logger.exception("Could not trim the stored runs of %s", job.owner)

    def _prune_if_due(self) -> None:
        """Delete runs past the retention window, if an interval has passed
        since the last time."""
        now = self._clock()
        if self._retention is None or now - self._pruned_at < self._prune_interval:
            return
        # Stamped first, so a table that can't be pruned is tried again in
        # an hour rather than on every run.
        self._pruned_at = now
        try:
            self._repository.prune(datetime.now(UTC) - self._retention)
        except Exception:
            logger.exception("Could not prune recorded simulation runs")
