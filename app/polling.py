"""Answering polls for finished runs without redoing the work each time.

A client polls every few seconds, and a finished run's response is about
0.7 MB of JSON. Each poll used to rebuild the response model, serialise it
and gzip it again, for the same bytes as last time. A finished run never
changes (its first outcome stands), so its response is built and
compressed once, kept for a while, and tagged with an ETag. A client that
already has it sends ``If-None-Match`` and gets a bodiless 304.
"""

from __future__ import annotations

import gzip
import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pydantic_core

from app.cache import MISSING, ExpiringLru
from app.jobs import SimulationJob
from app.schemas import JobState
from app.simulation import is_baseline

#: Finished responses kept. Each holds only its gzipped body, about 40 KB.
MAX_RENDERED = 64

#: How long an unknown job id is remembered as unknown, so repeated polls
#: for it don't each ask Supabase.
UNKNOWN_JOB_SECONDS = 30.0
MAX_UNKNOWN_JOBS = 10_000

#: Sent with a finished result: the browser may keep it, but must check the
#: ETag before reusing it, and no shared cache may keep it at all.
FINISHED_CACHE_CONTROL = "private, no-cache"
#: The body differs by encoding, and belongs to one signed-in user.
FINISHED_VARY = "Accept-Encoding, Authorization"


@dataclass(frozen=True)
class RenderedState:
    """A finished job's response, ready to send."""

    owner: str | None
    gzipped: bytes
    #: Of the uncompressed body, quoted. The gzipped form is tagged with a
    #: suffix, since the two are different bytes.
    etag: str

    def body(self) -> bytes:
        return gzip.decompress(self.gzipped)

    def etag_for(self, gzipped: bool) -> str:
        return f'{self.etag[:-1]}-gzip"' if gzipped else self.etag

    def matches(self, if_none_match: str | None) -> bool:
        """Whether an ``If-None-Match`` header names this response."""
        if not if_none_match:
            return False
        tags = {tag.strip().removeprefix("W/") for tag in if_none_match.split(",")}
        return "*" in tags or bool(tags & {self.etag_for(False), self.etag_for(True)})


_baseline_json: bytes | None = None
_baseline_lock = threading.Lock()


def _result_json(result: dict[str, Any] | None) -> bytes:
    """A result as JSON. The shared baseline is serialised only once."""
    global _baseline_json
    if not is_baseline(result):
        return pydantic_core.to_json(result)
    with _baseline_lock:
        if _baseline_json is None:
            _baseline_json = pydantic_core.to_json(result)
        return _baseline_json


def render(job: SimulationJob) -> RenderedState:
    """Serialise and compress a finished job's response once."""
    state = JobState(
        job_id=job.id,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
    )
    # The result is spliced in rather than put through the model, which
    # would validate and copy 0.7 MB for nothing.
    head = state.model_dump_json(exclude={"result", "error"}).encode()
    body = b"".join(
        [
            head[:-1],
            b',"result":',
            _result_json(job.result),
            b',"error":',
            pydantic_core.to_json(job.error),
            b"}",
        ]
    )
    return RenderedState(
        owner=job.owner,
        gzipped=gzip.compress(body, compresslevel=6),
        etag=f'"{hashlib.sha256(body).hexdigest()[:32]}"',
    )


class RenderedStates:
    """Finished jobs' responses by job id, and job ids known not to exist."""

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = MAX_RENDERED,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._rendered = ExpiringLru(max_entries)
        self._unknown = ExpiringLru(MAX_UNKNOWN_JOBS)

    def get(self, job_id: str) -> RenderedState | None:
        found = self._rendered.get(job_id, self._clock())
        return None if found is MISSING else found

    def put(self, job_id: str, rendered: RenderedState) -> None:
        self._rendered.put(job_id, rendered, self._clock() + self._ttl)

    def is_unknown(self, job_id: str) -> bool:
        return self._unknown.get(job_id, self._clock()) is not MISSING

    def mark_unknown(self, job_id: str) -> None:
        self._unknown.put(job_id, True, self._clock() + UNKNOWN_JOB_SECONDS)
