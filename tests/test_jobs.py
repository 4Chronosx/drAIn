"""Tests for the simulation job store."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from app.jobs import JobStatus, JobStore, JobStoreClosedError, QueueFullError, UserLimitError


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


def test_jobs_get_distinct_ids():
    # The cap is set above the burst: this is about ids, and whether the
    # first jobs finish before the fifth is submitted is down to scheduling.
    store = JobStore(max_workers=2, max_queued=5, retention=timedelta(minutes=5))
    try:
        ids = {store.submit(lambda: {}).id for _ in range(5)}
        assert len(ids) == 5
    finally:
        store.shutdown()


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


class TestPerOwnerLimits:
    """One person can't fill the queue, or the hour, for everyone else."""

    def test_an_owner_is_held_to_their_outstanding_limit(self):
        store = JobStore(
            max_workers=1, max_queued=4, retention=timedelta(minutes=5), max_jobs_per_owner=1
        )
        release = threading.Event()
        try:
            store.submit(hang_until(release), owner="a")
            with pytest.raises(UserLimitError):
                store.submit(lambda: {}, owner="a")
            # Another owner, and ownerless work, still get in.
            store.submit(lambda: {}, owner="b")
            store.submit(lambda: {})
        finally:
            release.set()
            store.shutdown()

    def test_a_finished_job_frees_the_owners_slot(self):
        limited = JobStore(
            max_workers=1, max_queued=4, retention=timedelta(minutes=5), max_jobs_per_owner=1
        )
        try:
            wait_for(limited.submit(lambda: {}, owner="a"), limited)
            assert limited.submit(lambda: {}, owner="a").owner == "a"
        finally:
            limited.shutdown()

    def test_an_owner_is_held_to_their_hourly_allowance(self):
        store = JobStore(
            max_workers=1,
            max_queued=4,
            retention=timedelta(minutes=5),
            max_runs_per_owner_per_hour=2,
        )
        try:
            for _ in range(2):
                wait_for(store.submit(lambda: {}, owner="a"), store)
            with pytest.raises(UserLimitError, match="past hour"):
                store.submit(lambda: {}, owner="a")
            assert store.submit(lambda: {}, owner="b").owner == "b"
        finally:
            store.shutdown()

    def test_a_user_limit_is_a_kind_of_full_queue(self):
        # So the API answers it with 429 like any other refusal.
        assert issubclass(UserLimitError, QueueFullError)


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


def wait_until(predicate, timeout=5.0):
    """Block until ``predicate()`` is true, or fail the test."""
    pause = threading.Event()
    for _ in range(int(timeout * 100)):
        if predicate():
            return
        pause.wait(0.01)
    raise AssertionError(f"condition not met within {timeout}s")


def hang_until(release, result=None):
    """Work that blocks until ``release`` is set, like a hung SWMM run."""

    def work():
        release.wait(10)
        return result if result is not None else {}

    return work


class TestSnapshots:
    """Regression: ``get`` handed out the live job, which the worker kept
    mutating, so a reader could see SUCCEEDED with the result still unset."""

    def test_get_returns_a_snapshot_not_the_live_job(self, store):
        release = threading.Event()
        try:
            job = store.submit(hang_until(release, {"ok": True}))
            before = store.get(job.id)
            release.set()
            wait_for(job, store)

            assert not before.is_finished
            assert before.result is None
        finally:
            release.set()

    def test_submit_returns_a_snapshot(self, store):
        job = store.submit(lambda: {"ok": True})
        wait_for(job, store)
        assert job.status is JobStatus.QUEUED

    @pytest.mark.parametrize("fails", [False, True])
    def test_a_finished_job_has_exactly_one_of_result_and_error(self, store, fails):
        def work():
            if fails:
                raise RuntimeError("boom")
            return {"ok": True}

        job = wait_for(store.submit(work), store)
        assert (job.result is None) != (job.error is None)


class TestAStuckJobDoesNotStallTheQueue:
    """Regression: timing out a hung job used to free nothing but its slot.

    The hung thread still held the only worker, so every job behind it
    stayed QUEUED forever, was never reaped (only RUNNING jobs were), and
    once the cap filled every new submission got 429.
    """

    def test_work_queued_behind_a_timed_out_job_still_runs(self):
        store = JobStore(
            max_workers=1,
            max_queued=4,
            retention=timedelta(minutes=5),
            max_runtime=timedelta(milliseconds=200),
        )
        release = threading.Event()
        try:
            stuck = store.submit(hang_until(release))
            wait_until(lambda: store.get(stuck.id).status is not JobStatus.QUEUED)
            behind = store.submit(lambda: {"ok": True})

            finished = wait_for(behind, store)
            assert finished.status is JobStatus.SUCCEEDED
            assert finished.result == {"ok": True}
            assert store.get(stuck.id).status is JobStatus.FAILED
        finally:
            release.set()
            store.shutdown()

    def test_new_work_runs_after_a_job_times_out(self):
        store = JobStore(
            max_workers=1,
            max_queued=4,
            retention=timedelta(minutes=5),
            max_runtime=timedelta(milliseconds=200),
        )
        release = threading.Event()
        try:
            wait_for(store.submit(hang_until(release)), store)
            assert wait_for(store.submit(lambda: {"ok": True}), store).result == {"ok": True}
        finally:
            release.set()
            store.shutdown()

    def test_a_job_that_waits_in_the_queue_too_long_is_failed(self):
        store = JobStore(
            max_workers=1,
            max_queued=4,
            retention=timedelta(minutes=5),
            max_runtime=timedelta(minutes=5),
            max_queue_wait=timedelta(milliseconds=100),
        )
        release = threading.Event()
        ran = threading.Event()

        def record_run():
            ran.set()
            return {}

        try:
            store.submit(hang_until(release))
            waiting = store.submit(record_run)

            finished = wait_for(waiting, store)
            assert finished.status is JobStatus.FAILED
            assert "waited too long" in finished.error
            assert finished.result is None

            # Once the worker frees up, the expired job must not run anyway.
            release.set()
            assert not ran.wait(0.3)
        finally:
            release.set()
            store.shutdown()

    def test_a_job_within_its_time_limits_is_not_reaped(self):
        store = JobStore(
            max_workers=1,
            max_queued=4,
            retention=timedelta(minutes=5),
            max_runtime=timedelta(minutes=5),
            max_queue_wait=timedelta(minutes=5),
        )
        release = threading.Event()
        try:
            running = store.submit(hang_until(release, {"ok": 1}))
            queued = store.submit(lambda: {"ok": 2})
            wait_until(lambda: store.get(running.id).status is JobStatus.RUNNING)
            threading.Event().wait(0.1)

            assert store.get(running.id).status is JobStatus.RUNNING
            assert store.get(queued.id).status is JobStatus.QUEUED
            release.set()
            assert wait_for(running, store).status is JobStatus.SUCCEEDED
            assert wait_for(queued, store).status is JobStatus.SUCCEEDED
        finally:
            release.set()
            store.shutdown()


class TestShutdown:
    def test_submitting_after_shutdown_is_refused_cleanly(self):
        store = JobStore(max_workers=1, max_queued=4, retention=timedelta(minutes=5))
        store.shutdown()
        with pytest.raises(JobStoreClosedError):
            store.submit(lambda: {})

    def test_a_refused_submission_leaves_no_orphan_job(self):
        store = JobStore(max_workers=1, max_queued=1, retention=timedelta(minutes=5))
        store.shutdown()
        with pytest.raises(JobStoreClosedError):
            store.submit(lambda: {})
        # Had the refused job been recorded, it would hold the only slot.
        assert store.outstanding() == 0

    def test_shutdown_fails_queued_jobs_instead_of_leaving_them_queued(self):
        store = JobStore(max_workers=1, max_queued=4, retention=timedelta(minutes=5))
        release = threading.Event()
        ran = threading.Event()

        def record_run():
            ran.set()
            return {}

        try:
            store.submit(hang_until(release))
            queued = store.submit(record_run)
            store.shutdown()

            after = store.get(queued.id)
            assert after.status is JobStatus.FAILED
            assert "shut down" in after.error
            release.set()
            assert not ran.wait(0.3)
        finally:
            release.set()


def test_a_failure_message_keeps_its_meaning_but_not_server_paths():
    from app.jobs import public_message

    windows = OSError(r"cannot open C:\Users\svc\AppData\Local\Temp\run-1\model.inp")
    posix = RuntimeError("SWMM failed reading /tmp/drain-x1/model.rpt at line 4")
    assert public_message(windows) == "cannot open model.inp"
    assert public_message(posix) == "SWMM failed reading model.rpt at line 4"
    assert public_message(RuntimeError()) == "RuntimeError"
    assert public_message(RuntimeError("did not finish in time")) == "did not finish in time"
