"""Tests for the simulation job store."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from app.jobs import JobStatus, JobStore, QueueFullError


@pytest.fixture
def store():
    store = JobStore(max_workers=2, max_queued=4, retention=timedelta(minutes=5))
    yield store
    store.shutdown()


def wait_for(job, store, timeout=5.0):
    """Block until a job reaches a terminal state, or fail the test."""
    deadline = threading.Event()
    for _ in range(int(timeout * 100)):
        current = store.get(job.id)
        if current is not None and current.is_finished:
            return current
        deadline.wait(0.01)
    raise AssertionError(f"job {job.id} did not finish within {timeout}s")


def test_a_queued_job_starts_out_queued(store):
    job = store.submit(lambda: {"ok": True})
    assert job.id
    assert job.created_at is not None


def test_a_successful_job_keeps_its_result(store):
    job = wait_for(store.submit(lambda: {"nodes": 3}), store)
    assert job.status is JobStatus.SUCCEEDED
    assert job.result == {"nodes": 3}
    assert job.error is None
    assert job.finished_at >= job.started_at


def test_a_failing_job_records_the_message_not_the_result(store):
    def explode():
        raise RuntimeError("SWMM exploded")

    job = wait_for(store.submit(explode), store)
    assert job.status is JobStatus.FAILED
    assert job.result is None
    assert "SWMM exploded" in job.error


def test_a_failure_with_no_message_still_reports_something(store):
    def explode():
        raise RuntimeError()

    job = wait_for(store.submit(explode), store)
    assert job.error == "RuntimeError"


def test_one_job_failing_does_not_stop_the_next(store):
    def explode():
        raise RuntimeError("boom")

    wait_for(store.submit(explode), store)
    assert wait_for(store.submit(lambda: {"ok": True}), store).result == {"ok": True}


def test_unknown_jobs_are_not_found(store):
    assert store.get("nope") is None


def test_jobs_get_distinct_ids(store):
    ids = {store.submit(lambda: {}).id for _ in range(5)}
    assert len(ids) == 5


def test_the_queue_refuses_more_than_its_cap():
    # One worker, so the first job occupies it and the rest queue behind it.
    store = JobStore(max_workers=1, max_queued=2, retention=timedelta(minutes=5))
    release = threading.Event()
    try:
        store.submit(lambda: (release.wait(5), {})[1])
        store.submit(lambda: {})

        with pytest.raises(QueueFullError, match="already queued or running"):
            store.submit(lambda: {})
    finally:
        release.set()
        store.shutdown()


def test_finished_jobs_free_up_queue_space(store):
    # The cap counts outstanding work, not everything ever submitted.
    for _ in range(6):
        wait_for(store.submit(lambda: {"ok": True}), store)


def test_results_expire_so_they_do_not_accumulate():
    # Each real result is close to a megabyte, so they cannot be kept forever.
    store = JobStore(max_workers=1, max_queued=4, retention=timedelta(seconds=-1))
    try:
        job = store.submit(lambda: {"ok": True})
        for _ in range(500):
            if store.get(job.id) is None:
                break
            threading.Event().wait(0.01)
        assert store.get(job.id) is None
    finally:
        store.shutdown()


def test_submit_and_wait_returns_a_finished_job(store):
    job = store.submit_and_wait(lambda: {"ok": True})
    assert job.status is JobStatus.SUCCEEDED
    assert job.result == {"ok": True}


def test_submit_and_wait_surfaces_failures_as_a_failed_job(store):
    def explode():
        raise RuntimeError("nope")

    job = store.submit_and_wait(explode)
    assert job.status is JobStatus.FAILED
    assert job.result is None
