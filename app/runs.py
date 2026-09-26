"""A durable record of simulation runs, kept in Supabase.

The job store (:mod:`app.jobs`) holds runs in this process's memory, which
is gone after a restart -- frequent on a free-tier host -- and after the
retention window. Each run is also written to the \`\`simulation_runs\`\` table
as it moves along, and read back from there when memory no longer has it.
So a result the app was told it could collect is still there to collect.

Writes use the service role (the table accepts no client writes) and go
through a single background thread, in order, so a slow database never
holds up a request or a simulation. A failed write is logged and dropped:
the run itself matters more than its record.

The table is defined in the frontend repository's
\`\`supabase/schemas/schema_ops.sql\`\`.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from app.jobs import JobStatus, SimulationJob

logger = logging.getLogger(__name__)

TABLE_PATH = "/rest/v1/simulation_runs"


class RunStoreError(RuntimeError):
    """Raised when the run table could not be read or written."""


class RunRepository(Protocol):
    def save(self, job: SimulationJob) -> None:
        """Insert or update a run's row."""

    def load(self, job_id: str) -> SimulationJob | None:
        """A stored run, or \`\`None\`\` if there is none."""

    def fail_unfinished(self, reason: str) -> None:
        """Mark every queued or running run failed (after a restart)."""

    def prune(self, older_than: datetime) -> None:
        """Delete runs created before \`\`older_than\`\`."""


#: Takes (method, url, headers, body) and returns (HTTP status, body).
Fetch = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


def _urllib_fetch(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
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
    """The result without \`\`nodes_dict\`\`, which repeats \`\`nodes_list\`\`."""
    if result is None:
        return None
    return {key: value for key, value in result.items() if key != "nodes_dict"}


def _expand(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Rebuild \`\`nodes_dict\`\` from \`\`nodes_list\`\`, as the API serves it."""
    if result is None or "nodes_list" not in result:
        return result
    nodes_dict = {
        row["Node"]: {key: value for key, value in row.items() if key != "Node"}
        for row in result["nodes_list"]
    }
    return {**result, "nodes_dict": nodes_dict}


@dataclass
class SupabaseRunRepository:
    """Reads and writes \`\`simulation_runs\`\` through Supabase's REST API."""

    supabase_url: str
    service_role_key: str
    fetch: Fetch = _urllib_fetch

    def _call(
        self,
        method: str,
        query: str = "",
        body: Any = None,
        prefer: str | None = None,
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
        status, response = self.fetch(method, url, headers, payload)
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
        rows = json.loads(self._call("GET", query) or b"[]")
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
            result=_expand(row["result"]),
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


_STOP = object()


class RunRecorder:
    """Writes run changes to a repository on one background thread, in order.

    The job store calls :meth:\`record\` with a snapshot each time a run is
    queued, starts or finishes, sometimes while holding its lock, so this
    only puts the snapshot on a queue.
    """

    def __init__(self, repository: RunRepository) -> None:
        self._repository = repository
        self._queue: queue.Queue[object] = queue.Queue()
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
        self._queue.put(job)

    def flush(self, timeout: float = 10.0) -> None:
        """Wait until everything recorded so far has been written."""
        done = threading.Event()
        self._queue.put(done)
        done.wait(timeout)

    def close(self, timeout: float = 10.0) -> None:
        self._queue.put(_STOP)
        self._thread.join(timeout)

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            if isinstance(item, threading.Event):
                item.set()
                continue
            assert isinstance(item, SimulationJob)
            try:
                self._repository.save(item)
            except Exception:
                logger.exception("Could not record simulation %s (%s)", item.id, item.status)
