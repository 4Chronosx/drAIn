"""Tests for the durable record of simulation runs."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.jobs import JobStatus, JobStore, SimulationJob
from app.main import RESTARTED_MESSAGE, create_app
from app.runs import RunRecorder, RunStoreError, SupabaseRunRepository
from app.simulation import BASELINE_MARKER, baseline_result
from drain.model_info import network_sha256
from tests.test_api import AS_A, AS_B, TEST_SETTINGS, FakeAuthenticator

USER_A = "00000000-0000-4000-a000-00000000000a"
USER_B = "00000000-0000-4000-a000-00000000000b"
JOB_ID = "11111111-1111-4111-8111-111111111111"


class FakePostgrest:
    """Records requests and answers from a canned response."""

    def __init__(self, status=201, body=b""):
        self.status = status
        self.body = body
        self.calls = []
        self.timeouts = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        self.timeouts.append(timeout)
        return self.status, self.body


def repository(fake):
    return SupabaseRunRepository("https://p.supabase.co", "service-key", fetch=fake)


def finished_job(**overrides):
    job = SimulationJob(
        id=JOB_ID,
        owner=USER_A,
        status=JobStatus.SUCCEEDED,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        started_at=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
        finished_at=datetime(2026, 9, 1, 0, 3, tzinfo=UTC),
        result={
            "metadata": {"total_nodes": 1},
            "nodes_list": [{"Node": "I-4", "Hours_Flooded": 1.5}],
            "nodes_dict": {"I-4": {"Hours_Flooded": 1.5}},
        },
    )
    return replace(job, **overrides)


class TestSupabaseRunRepository:
    def test_a_save_is_an_upsert_as_the_service_role(self):
        fake = FakePostgrest()
        repository(fake).save(finished_job())

        method, url, headers, row = fake.calls[0]
        assert method == "POST"
        assert url == "https://p.supabase.co/rest/v1/simulation_runs?on_conflict=id"
        assert headers["Authorization"] == "Bearer service-key"
        assert "resolution=merge-duplicates" in headers["Prefer"]
        assert row["id"] == JOB_ID and row["user_id"] == USER_A
        assert row["status"] == "succeeded"
        assert row["model_version"] == network_sha256()

    def test_the_repeated_nodes_dict_is_not_stored(self):
        fake = FakePostgrest()
        repository(fake).save(finished_job())
        assert "nodes_dict" not in fake.calls[0][3]["result"]

    def test_the_request_is_sent_only_when_known(self):
        fake = FakePostgrest()
        repository(fake).save(finished_job())
        assert "request" not in fake.calls[0][3]

        repository(fake).save(finished_job(request={"nodes": {}}))
        assert fake.calls[1][3]["request"] == {"nodes": {}}

    def test_a_loaded_run_is_served_as_it_was_stored(self):
        row = {
            "id": JOB_ID,
            "user_id": USER_A,
            "status": "succeeded",
            "created_at": "2026-09-01T00:00:00+00:00",
            "started_at": "2026-09-01T00:01:00+00:00",
            "finished_at": "2026-09-01T00:03:00+00:00",
            "result": {"nodes_list": [{"Node": "I-4", "Hours_Flooded": 1.5}]},
            "error": None,
        }
        job = repository(FakePostgrest(200, json.dumps([row]).encode())).load(JOB_ID)

        assert job.owner == USER_A
        assert job.status is JobStatus.SUCCEEDED
        assert job.result == {"nodes_list": [{"Node": "I-4", "Hours_Flooded": 1.5}]}

    def test_an_unknown_run_is_none(self):
        assert repository(FakePostgrest(200, b"[]")).load(JOB_ID) is None

    def test_a_malformed_id_never_reaches_the_database(self):
        fake = FakePostgrest(200, b"[]")
        assert repository(fake).load("does-not-exist") is None
        assert fake.calls == []

    def test_unfinished_runs_are_failed_in_one_request(self):
        fake = FakePostgrest(204)
        repository(fake).fail_unfinished("restarted")
        method, url, _, body = fake.calls[0]
        assert method == "PATCH"
        assert url.endswith("?status=in.(queued,running)")
        assert body["status"] == "failed" and body["error"] == "restarted"

    def test_an_error_answer_raises(self):
        with pytest.raises(RunStoreError):
            repository(FakePostgrest(500, b"boom")).save(finished_job())


class MemoryRepository:
    """Keeps rows in a dict, the way the table would."""

    def __init__(self):
        self.rows: dict[str, SimulationJob] = {}
        self.saves: list[SimulationJob] = []
        self.failed_unfinished = []
        self.pruned = []

    def save(self, job):
        self.saves.append(job)
        stored = self.rows.get(job.id)
        request = job.request if job.request is not None else (stored and stored.request)
        self.rows[job.id] = replace(job, request=request)

    def load(self, job_id):
        return self.rows.get(job_id)

    def fail_unfinished(self, reason):
        self.failed_unfinished.append(reason)

    def prune(self, older_than):
        self.pruned.append(older_than)

    def count_recent(self, owner, since, limit):
        recent = [job for job in self.rows.values() if job.owner == owner]
        return min(limit, len([job for job in recent if job.created_at >= since]))


class TestRunRecorder:
    def test_every_change_is_written_in_order(self):
        memory = MemoryRepository()
        recorder = RunRecorder(memory)
        store = JobStore(
            max_workers=1, max_queued=4, retention=timedelta(minutes=5), listener=recorder.record
        )
        try:
            job = store.submit(lambda: {"ok": True}, owner=USER_A, request={"nodes": {}})
            deadline = time.monotonic() + 5
            while not store.get(job.id).is_finished and time.monotonic() < deadline:
                time.sleep(0.01)
            recorder.flush()

            statuses = [saved.status for saved in memory.saves]
            assert statuses == [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCEEDED]
            assert memory.rows[job.id].request == {"nodes": {}}
            assert memory.rows[job.id].result == {"ok": True}
        finally:
            store.shutdown()
            recorder.close()

    def test_a_non_account_owner_is_not_recorded(self):
        memory = MemoryRepository()
        recorder = RunRecorder(memory)
        recorder.record(finished_job(owner="local-dev"))
        recorder.flush()
        recorder.close()
        assert memory.saves == []

    def test_a_failed_write_does_not_stop_later_ones(self):
        class Flaky(MemoryRepository):
            def save(self, job):
                if not self.saves:
                    self.saves.append(job)
                    raise RunStoreError("down")
                super().save(job)

        flaky = Flaky()
        recorder = RunRecorder(flaky)
        recorder.record(finished_job())
        recorder.record(finished_job())
        recorder.flush()
        recorder.close()
        assert len(flaky.saves) == 2


class TestRunRecorderShutdown:
    class Slow(MemoryRepository):
        """Takes a while per write, like Supabase on a bad day."""

        def save(self, job):
            time.sleep(0.02)
            super().save(job)

    class Blocked(MemoryRepository):
        """Doesn't return from a write until released."""

        def __init__(self):
            super().__init__()
            self.release = threading.Event()

        def save(self, job):
            self.release.wait(5)
            super().save(job)

    def test_close_writes_everything_recorded_before_it(self):
        slow = self.Slow()
        recorder = RunRecorder(slow)
        for _ in range(5):
            recorder.record(finished_job())
        recorder.close()
        assert len(slow.saves) == 5

    def test_a_write_that_fails_during_shutdown_is_logged(self, caplog):
        class Down(MemoryRepository):
            def save(self, job):
                raise RunStoreError("down")

        recorder = RunRecorder(Down())
        job = finished_job()
        recorder.record(job)
        with caplog.at_level(logging.ERROR, logger="app.runs"):
            recorder.close()
        assert any(job.id in record.getMessage() for record in caplog.records)

    def test_writes_left_when_close_gives_up_are_logged(self, caplog):
        blocked = self.Blocked()
        recorder = RunRecorder(blocked)
        recorder.record(finished_job())
        recorder.record(finished_job())
        with caplog.at_level(logging.ERROR, logger="app.runs"):
            recorder.close(timeout=0.2)
        blocked.release.set()
        assert any("not saved" in record.getMessage() for record in caplog.records)

    def test_a_change_after_close_is_logged_not_queued(self, caplog):
        memory = MemoryRepository()
        recorder = RunRecorder(memory)
        recorder.close()
        with caplog.at_level(logging.WARNING, logger="app.runs"):
            recorder.record(finished_job())
        assert memory.saves == []
        assert any("after shutdown" in record.getMessage() for record in caplog.records)

    def test_a_full_queue_drops_and_logs_rather_than_blocking(self, caplog):
        blocked = self.Blocked()
        recorder = RunRecorder(blocked, max_pending=1)
        with caplog.at_level(logging.ERROR, logger="app.runs"):
            started = time.monotonic()
            for _ in range(4):
                recorder.record(finished_job())
            assert time.monotonic() - started < 1
        blocked.release.set()
        recorder.close()
        assert any("writes behind" in record.getMessage() for record in caplog.records)


class FakeAuthByUuid(FakeAuthenticator):
    """The test users, with real-looking account ids."""

    def authenticate(self, token):
        caller = super().authenticate(token)
        if caller is None:
            return None
        return replace(caller, user_id=USER_A if caller.user_id == "user-a" else USER_B)


class TestRunsSurviveRestarts:
    def make_app(self, memory, **overrides):
        return create_app(
            replace(TEST_SETTINGS, **overrides),
            authenticator=FakeAuthByUuid(),
            runs=memory,
        )

    def test_startup_fails_runs_the_last_process_left_unfinished(self):
        memory = MemoryRepository()
        with TestClient(self.make_app(memory)):
            pass
        assert memory.failed_unfinished == [RESTARTED_MESSAGE]
        assert len(memory.pruned) == 1

    def test_a_result_gone_from_memory_is_read_back(self):
        memory = MemoryRepository()
        memory.rows[JOB_ID] = finished_job()
        with TestClient(self.make_app(memory), headers=AS_A) as client:
            response = client.get(f"/simulations/{JOB_ID}")
        assert response.status_code == 200
        assert response.json()["status"] == "succeeded"
        assert response.json()["result"]["nodes_list"][0]["Node"] == "I-4"

    def test_a_stored_run_is_still_only_its_owners(self):
        memory = MemoryRepository()
        memory.rows[JOB_ID] = finished_job()
        with TestClient(self.make_app(memory), headers=AS_B) as client:
            assert client.get(f"/simulations/{JOB_ID}").status_code == 404

    def test_a_new_run_is_recorded_with_what_was_asked(self, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr("app.main._simulate", lambda request: (release.wait(10), {})[1])
        memory = MemoryRepository()
        app = self.make_app(memory)
        try:
            with TestClient(app, headers=AS_A) as client:
                body = {"rainfall": {"total_precip": 50, "duration_hr": 1}}
                job_id = client.post("/simulations", json=body).json()["job_id"]
                deadline = time.monotonic() + 5
                while job_id not in memory.rows and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert memory.rows[job_id].request == {"nodes": {}, "links": {}, **body}
                assert memory.rows[job_id].owner == USER_A
        finally:
            release.set()


class TestTheBaselineIsStoredOnce:
    """Every run of the unmodified network has the same 0.7 MB result, and
    each used to be written to the table in full."""

    def test_a_baseline_run_stores_the_marker(self):
        fake = FakePostgrest()
        repository(fake).save(finished_job(result=baseline_result()))
        assert fake.calls[0][3]["result"] == BASELINE_MARKER

    def test_an_equal_but_separate_result_is_stored_in_full(self):
        fake = FakePostgrest()
        copy = json.loads(json.dumps(baseline_result()))
        repository(fake).save(finished_job(result=copy))
        assert fake.calls[0][3]["result"] != BASELINE_MARKER

    def test_the_marker_is_read_back_as_the_baseline(self):
        row = {
            "id": JOB_ID,
            "user_id": USER_A,
            "status": "succeeded",
            "created_at": "2026-09-01T00:00:00+00:00",
            "started_at": "2026-09-01T00:01:00+00:00",
            "finished_at": "2026-09-01T00:03:00+00:00",
            "result": BASELINE_MARKER,
            "error": None,
        }
        job = repository(FakePostgrest(200, json.dumps([row]).encode())).load(JOB_ID)
        assert job.result is baseline_result()


class TestRecentRunCount:
    def test_it_counts_the_owners_runs_in_the_window(self):
        fake = FakePostgrest(200, json.dumps([{"id": "a"}, {"id": "b"}]).encode())
        since = datetime(2026, 9, 1, tzinfo=UTC)
        assert repository(fake).count_recent(USER_A, since, limit=10) == 2

        method, url, _, _ = fake.calls[0]
        assert method == "GET"
        assert f"user_id=eq.{USER_A}" in url
        assert "created_at=gte.2026-09-01" in url
        assert "limit=10" in url
        # A request waits on this, so it may not take the writes' 30 s.
        assert fake.timeouts[0] <= 5

    def test_a_non_account_owner_is_never_looked_up(self):
        fake = FakePostgrest(200, b"[]")
        assert repository(fake).count_recent("local-dev", datetime.now(UTC), 10) == 0
        assert fake.calls == []


class TestLimitsSurviveRestarts:
    """Regression: the hourly allowance was counted in memory only, so every
    restart handed everyone a fresh one."""

    def make_app(self, memory, **overrides):
        return create_app(
            replace(TEST_SETTINGS, **overrides),
            authenticator=FakeAuthByUuid(),
            runs=memory,
        )

    def test_runs_recorded_before_a_restart_count_against_the_hour(self, monkeypatch):
        monkeypatch.setattr("app.main._simulate", lambda request: {})
        memory = MemoryRepository()
        for n in range(2):
            job_id = f"11111111-1111-4111-8111-00000000000{n}"
            memory.rows[job_id] = finished_job(id=job_id, created_at=datetime.now(UTC))
        with TestClient(self.make_app(memory, max_runs_per_user_per_hour=2)) as client:
            refused = client.post("/simulations", json={}, headers=AS_A)
            assert refused.status_code == 429
            assert "past hour" in refused.json()["detail"]
            assert client.post("/simulations", json={}, headers=AS_B).status_code == 202

    def test_runs_older_than_an_hour_do_not(self, monkeypatch):
        monkeypatch.setattr("app.main._simulate", lambda request: {})
        memory = MemoryRepository()
        memory.rows[JOB_ID] = finished_job(created_at=datetime.now(UTC) - timedelta(hours=2))
        with TestClient(self.make_app(memory, max_runs_per_user_per_hour=1)) as client:
            assert client.post("/simulations", json={}, headers=AS_A).status_code == 202

    def test_if_the_table_cannot_be_counted_memory_decides(self, monkeypatch):
        monkeypatch.setattr("app.main._simulate", lambda request: {})

        class Uncountable(MemoryRepository):
            def count_recent(self, owner, since, limit):
                raise RunStoreError("down")

        with TestClient(self.make_app(Uncountable())) as client:
            assert client.post("/simulations", json={}, headers=AS_A).status_code == 202


class TestUnknownRunsAreRemembered:
    def test_polling_an_unknown_run_asks_the_table_once(self):
        class Counting(MemoryRepository):
            def __init__(self):
                super().__init__()
                self.loads = 0

            def load(self, job_id):
                self.loads += 1
                return super().load(job_id)

        memory = Counting()
        app = create_app(TEST_SETTINGS, authenticator=FakeAuthByUuid(), runs=memory)
        with TestClient(app, headers=AS_A) as client:
            for _ in range(3):
                assert client.get(f"/simulations/{JOB_ID}").status_code == 404
        assert memory.loads == 1

    def test_a_stored_result_is_served_from_cache_after_the_first_read(self):
        class Counting(MemoryRepository):
            loads = 0

            def load(self, job_id):
                Counting.loads += 1
                return super().load(job_id)

        memory = Counting()
        memory.rows[JOB_ID] = finished_job()
        app = create_app(TEST_SETTINGS, authenticator=FakeAuthByUuid(), runs=memory)
        with TestClient(app, headers=AS_A) as client:
            for _ in range(3):
                assert client.get(f"/simulations/{JOB_ID}").status_code == 200
            assert client.get(f"/simulations/{JOB_ID}", headers=AS_B).status_code == 404
        assert Counting.loads == 1
