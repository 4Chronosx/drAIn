"""Tests for the HTTP layer."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app, create_app
from drain.flooding import build_flooding_summary as flooding_summary_for_test


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health_reports_the_model_is_available(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["vulnerability_model_loaded"] is True


def test_unmodified_request_serves_the_baseline(client):
    """An empty request needs no simulation, so this stays fast."""
    response = client.post("/run-simulation", json={"nodes": {}, "links": {}, "rainfall": {}})
    assert response.status_code == 200

    body = response.json()
    assert body["metadata"]["total_nodes"] == 1413
    assert body["metadata"]["flooded_nodes"] == 450
    assert len(body["nodes_list"]) == 1413


def test_baseline_exposes_the_same_nodes_as_a_list_and_a_dict(client):
    body = client.post("/run-simulation", json={}).json()
    assert {row["Node"] for row in body["nodes_list"]} == set(body["nodes_dict"])


def test_node_rows_carry_the_documented_fields(client):
    body = client.post("/run-simulation", json={}).json()
    assert set(body["nodes_dict"]["I-4"]) == {
        # Raw flooding figures from the simulation.
        "Hours_Flooded",
        "Maximum_Rate_CMS",
        "Time_of_Max_days",
        "Time_of_Max_hr_min",
        "Total_Flood_Volume_10e6_ltr",
        "Time_After_Raining_min",
        # Hazard: how badly the node floods.
        "Vulnerability_Category",
        "Vulnerability_Score",
        # Exposure: who is around it.
        "Barangay",
        "Population_Density",
        "Exposure_Score",
        # The two combined.
        "Risk_Score",
        # The superseded k-means output.
        "Legacy_Cluster_Category",
        "Legacy_Cluster_Score",
    }


def test_metadata_does_not_leak_server_paths(client):
    metadata = client.post("/run-simulation", json={}).json()["metadata"]
    for key in ("rpt_file", "out_file", "model_file"):
        assert "/" not in metadata[key] and "\\" not in metadata[key]


@pytest.mark.parametrize(
    "payload",
    [
        {"rainfall": {"total_precip": 10, "duration_hr": 48}},  # beyond the 24 h window
        {"rainfall": {"total_precip": 10, "duration_hr": 0}},  # zero-length storm
        {"rainfall": {"total_precip": -5, "duration_hr": 2}},  # negative depth
        {"rainfall": {"total_precip": 10}},  # missing duration
    ],
)
def test_rejects_impossible_storms(client, payload):
    assert client.post("/run-simulation", json=payload).status_code == 422


def test_a_failing_simulation_returns_an_error_status(client, monkeypatch):
    """Regression: failures used to be swallowed and returned as 200 {}.

    The frontend checks response.ok, so a 200 with no nodes_list surfaced as
    an unrelated TypeError further down the page.
    """

    def explode(*args, **kwargs):
        raise RuntimeError("SWMM exploded")

    monkeypatch.setattr("app.main.run_simulation", explode)
    response = client.post("/run-simulation", json={})
    assert response.status_code == 500
    assert "nodes_list" not in response.json()


def test_a_failing_sync_simulation_says_why(client, monkeypatch):
    """Regression: the synchronous endpoint dropped the job's error and
    always answered a bare 'Simulation failed.'"""

    def explode(*args, **kwargs):
        raise RuntimeError("SWMM exploded")

    monkeypatch.setattr("app.main.run_simulation", explode)
    response = client.post("/run-simulation", json={})
    assert response.status_code == 500
    assert "SWMM exploded" in response.json()["detail"]


@pytest.mark.parametrize("path", ["/simulations", "/run-simulation"])
def test_a_request_during_shutdown_is_a_503_not_a_crash(path):
    app = create_app(settings)
    with TestClient(app):
        pass  # leaving the block runs shutdown, which closes the job store
    response = TestClient(app).post(path, json={})
    assert response.status_code == 503
    assert int(response.headers["retry-after"]) > 0


def poll_until_finished(client, poll_url, timeout=60.0):
    """Poll a queued simulation the way a client would."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(poll_url)
        assert response.status_code == 200
        body = response.json()
        if body["status"] in {"succeeded", "failed"}:
            return body
        time.sleep(0.05)
    raise AssertionError(f"{poll_url} did not finish within {timeout}s")


class TestQueuedSimulations:
    """POST /simulations queues work; GET /simulations/{id} collects it."""

    def test_posting_a_simulation_returns_202_not_the_result(self, client):
        response = client.post("/simulations", json={})
        assert response.status_code == 202

        body = response.json()
        assert body["status"] == "queued"
        assert body["poll_url"] == f"/simulations/{body['job_id']}"
        assert "nodes_list" not in body

    def test_the_response_points_at_where_to_poll(self, client):
        response = client.post("/simulations", json={})
        assert response.headers["location"] == response.json()["poll_url"]
        assert int(response.headers["retry-after"]) > 0

    def test_polling_yields_the_same_payload_as_the_sync_endpoint(self, client):
        poll_url = client.post("/simulations", json={}).json()["poll_url"]
        finished = poll_until_finished(client, poll_url)

        assert finished["status"] == "succeeded"
        assert finished["error"] is None
        assert finished["result"] == client.post("/run-simulation", json={}).json()

    def test_a_finished_job_reports_when_it_ran(self, client):
        poll_url = client.post("/simulations", json={}).json()["poll_url"]
        finished = poll_until_finished(client, poll_url)

        assert finished["created_at"] is not None
        assert finished["started_at"] is not None
        assert finished["finished_at"] is not None

    def test_polling_an_unknown_job_is_a_404(self, client):
        response = client.get("/simulations/does-not-exist")
        assert response.status_code == 404
        assert "expired" in response.json()["detail"]

    @pytest.mark.parametrize(
        "payload",
        [
            {"rainfall": {"total_precip": 10, "duration_hr": 48}},
            {"rainfall": {"total_precip": -5, "duration_hr": 2}},
        ],
    )
    def test_an_impossible_storm_is_rejected_before_it_is_queued(self, client, payload):
        # Validation failures should not cost a queue slot.
        assert client.post("/simulations", json=payload).status_code == 422

    def test_a_failing_simulation_finishes_as_failed(self, client, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("SWMM exploded")

        monkeypatch.setattr("app.main.run_simulation", explode)
        poll_url = client.post("/simulations", json={}).json()["poll_url"]
        finished = poll_until_finished(client, poll_url)

        assert finished["status"] == "failed"
        assert finished["result"] is None
        assert "SWMM exploded" in finished["error"]

    def test_a_full_queue_is_rejected_with_429(self, monkeypatch):
        # One worker and one slot, held by a simulation that will not return.
        release = threading.Event()
        monkeypatch.setattr("app.main._simulate", lambda request: (release.wait(10), {})[1])
        app = create_app(replace(settings, max_concurrent_simulations=1, max_queued_simulations=1))
        try:
            with TestClient(app) as full_client:
                assert full_client.post("/simulations", json={}).status_code == 202

                response = full_client.post("/simulations", json={})
                assert response.status_code == 429
                assert int(response.headers["retry-after"]) > 0
        finally:
            release.set()

    def test_a_finished_job_stops_asking_to_be_polled(self, client):
        poll_url = client.post("/simulations", json={}).json()["poll_url"]
        poll_until_finished(client, poll_url)
        assert "retry-after" not in client.get(poll_url).headers

    def test_polling_a_timed_out_job_reports_it_failed(self, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr("app.main._simulate", lambda request: (release.wait(10), {})[1])
        app = create_app(replace(settings, max_runtime_seconds=0))
        try:
            with TestClient(app) as slow_client:
                poll_url = slow_client.post("/simulations", json={}).json()["poll_url"]
                finished = poll_until_finished(slow_client, poll_url, timeout=10)
                assert finished["status"] == "failed"
                assert finished["result"] is None
                assert "did not finish in time" in finished["error"]
        finally:
            release.set()

    def test_a_hung_run_does_not_block_the_jobs_behind_it(self, monkeypatch):
        """Regression: one hung run held the only worker, so every later job
        stayed queued until the cap filled and everything got 429."""
        release = threading.Event()
        calls = []

        def first_hangs(request):
            calls.append(request)
            if len(calls) == 1:
                release.wait(10)
            return {"run": len(calls)}

        monkeypatch.setattr("app.main._simulate", first_hangs)
        app = create_app(
            replace(
                settings,
                max_concurrent_simulations=1,
                max_queued_simulations=2,
                # Long enough that the second, quick run is not also reaped.
                max_runtime_seconds=1,
            )
        )
        try:
            with TestClient(app) as slow_client:
                slow_client.post("/simulations", json={})
                behind = slow_client.post("/simulations", json={}).json()["poll_url"]
                finished = poll_until_finished(slow_client, behind, timeout=10)
                assert finished["status"] == "succeeded"
        finally:
            release.set()


class TestEventDurationReachesTheScorer:
    """Regression: the scorer took the storm length but nobody passed it.

    Hazard scores duration as a share of the event. The parameter existed
    and was unit-tested, but neither the API nor the CLI supplied it, so
    every run was scored against a 24-hour default — a 40-minute flood in a
    one-hour storm read as 3% of a day instead of two thirds of the event,
    which demoted nodes a whole category.
    """

    def test_the_baseline_reports_the_event_length_it_used(self, client):
        metadata = client.post("/run-simulation", json={}).json()["metadata"]
        assert metadata["event_hours"] == 24.0

    def test_a_custom_storm_reports_its_own_length(self, client, monkeypatch):
        captured = {}
        real = flooding_summary_for_test

        def spy(rpt_path, out_path, *args, **kwargs):
            captured["event_hours"] = kwargs.get("event_hours")
            return real(rpt_path, out_path, *args, **kwargs)

        monkeypatch.setattr("app.main.build_flooding_summary", spy)
        client.post(
            "/run-simulation",
            json={"rainfall": {"total_precip": 0, "duration_hr": 2}},
        )
        assert captured["event_hours"] == 2.0


class TestDeprecatedSyncEndpoint:
    def test_it_still_returns_the_result_directly(self, client):
        body = client.post("/run-simulation", json={}).json()
        assert body["metadata"]["total_nodes"] == 1413

    def test_it_is_marked_deprecated_in_the_schema(self, client):
        schema = client.get("/openapi.json").json()
        assert schema["paths"]["/run-simulation"]["post"]["deprecated"] is True
        assert "deprecated" not in schema["paths"]["/simulations"]["post"]
