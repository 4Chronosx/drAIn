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


class TestAbandonedJobs:
    """A job stuck RUNNING holds its queue slot until the process restarts.

    Only finished jobs are reaped, so anything that leaves a job in RUNNING
    -- a hung worker, or a BaseException that skipped the handler -- used to
    consume a slot permanently.
    """

    def test_a_job_running_too_long_is_failed(self):
        store = JobStore(
            max_workers=1,
            max_queued=4,
            retention=timedelta(minutes=5),
            max_runtime=timedelta(seconds=-1),
        )
        release = threading.Event()
        try:
            job = store.submit(lambda: (release.wait(5), {})[1])
            # Any lookup reaps first.
            for _ in range(200):
                current = store.get(job.id)
                if current is not None and current.status is JobStatus.FAILED:
                    break
                threading.Event().wait(0.01)
            current = store.get(job.id)
            assert current is not None
            assert current.status is JobStatus.FAILED
            assert "did not finish in time" in current.error
        finally:
            release.set()
            store.shutdown()

    def test_an_abandoned_job_frees_its_queue_slot(self):
        store = JobStore(
            max_workers=1,
            max_queued=1,
            retention=timedelta(minutes=5),
            max_runtime=timedelta(seconds=-1),
        )
        release = threading.Event()
        try:
            store.submit(lambda: (release.wait(5), {})[1])
            # The slot is occupied, but the occupant is past its limit, so
            # the next submission is accepted rather than rejected.
            for _ in range(200):
                try:
                    store.submit(lambda: {})
                    break
                except QueueFullError:
                    threading.Event().wait(0.01)
            else:
                raise AssertionError("the queue never freed the abandoned slot")
        finally:
            release.set()
            store.shutdown()

    def test_a_base_exception_still_finishes_the_job(self):
        store = JobStore(max_workers=1, max_queued=2, retention=timedelta(minutes=5))
        try:

            def explode():
                raise MemoryError("out of memory")

            job = wait_for(store.submit(explode), store)
            assert job.status is JobStatus.FAILED
            assert "out of memory" in job.error
        finally:
            store.shutdown()


class TestLateResults:
    """Regression: a job that returned after being timed out flipped to
    SUCCEEDED and kept the timeout message in ``error``."""

    def test_a_late_success_does_not_overwrite_a_timeout(self):
        store = JobStore(
            max_workers=1,
            max_queued=4,
            retention=timedelta(minutes=5),
            max_runtime=timedelta(milliseconds=100),
        )
        release = threading.Event()
        returned = threading.Event()

        def slow():
            release.wait(10)
            returned.set()
            return {"late": True}

        try:
            job = wait_for(store.submit(slow), store)
            assert job.status is JobStatus.FAILED

            release.set()
            assert returned.wait(5)
            threading.Event().wait(0.1)

            after = store.get(job.id)
            assert after.status is JobStatus.FAILED
            assert after.result is None
            assert "did not finish in time" in after.error
        finally:
            release.set()
            store.shutdown()

    def test_a_late_failure_does_not_overwrite_a_timeout(self):
        store = JobStore(
            max_workers=1,
            max_queued=4,
            retention=timedelta(minutes=5),
            max_runtime=timedelta(milliseconds=100),
        )
        release = threading.Event()

        def slow():
            release.wait(10)
            raise RuntimeError("late failure")

        try:
            job = wait_for(store.submit(slow), store)
            release.set()
            threading.Event().wait(0.2)
            assert "did not finish in time" in store.get(job.id).error
        finally:
            release.set()
            store.shutdown()
