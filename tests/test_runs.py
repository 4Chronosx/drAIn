"""Tests for the durable record of simulation runs."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.jobs import JobStatus, JobStore, SimulationJob
from app.main import RESTARTED_MESSAGE, create_app
from app.runs import RunRecorder, RunStoreError, SupabaseRunRepository
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

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
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

    def test_a_loaded_run_serves_nodes_dict_again(self):
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
        assert job.result["nodes_dict"] == {"I-4": {"Hours_Flooded": 1.5}}

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
