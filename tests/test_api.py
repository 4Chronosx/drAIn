"""Tests for the HTTP layer."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.auth import AuthUnavailableError, Caller
from app.config import settings
from app.main import create_app
from drain.flooding import build_flooding_summary as flooding_summary_for_test

#: Two known users; any other token is invalid.
USERS = {"token-a": Caller("user-a"), "token-b": Caller("user-b")}


class FakeAuthenticator:
    def authenticate(self, token: str) -> Caller | None:
        return USERS.get(token)


class BrokenAuthenticator:
    def authenticate(self, token: str) -> Caller | None:
        raise AuthUnavailableError("Supabase is down")


#: Generous per-user limits, so tests that queue several runs aren't held
#: back by them. The tests of the limits set their own.
TEST_SETTINGS = replace(
    settings,
    require_auth=True,
    max_jobs_per_user=100,
    max_runs_per_user_per_hour=1000,
)

AS_A = {"Authorization": "Bearer token-a"}
AS_B = {"Authorization": "Bearer token-b"}


def make_app(authenticator=None, **overrides):
    return create_app(
        replace(TEST_SETTINGS, **overrides),
        authenticator=authenticator or FakeAuthenticator(),
    )


@pytest.fixture(scope="module")
def client():
    with TestClient(make_app(), headers=AS_A) as test_client:
        yield test_client


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


def run(client, payload):
    """Queue a simulation and wait for it; return the finished job."""
    response = client.post("/simulations", json=payload)
    assert response.status_code == 202, response.text
    return poll_until_finished(client, response.json()["poll_url"])


def baseline(client):
    finished = run(client, {})
    assert finished["status"] == "succeeded"
    return finished["result"]


def test_health_says_ok(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_health_needs_no_sign_in():
    with TestClient(make_app()) as anonymous:
        assert anonymous.get("/health").status_code == 200


def test_unmodified_request_serves_the_baseline(client):
    """An empty request needs no simulation, so this stays fast."""
    body = baseline(client)
    assert body["metadata"]["total_nodes"] == 1413
    assert body["metadata"]["flooded_nodes"] == 450
    assert len(body["nodes_list"]) == 1413


def test_each_node_is_sent_once(client):
    # nodes_dict repeated every row keyed by id and doubled the payload.
    body = baseline(client)
    assert "nodes_dict" not in body
    assert "structure_info" not in body["metadata"]
    ids = [row["Node"] for row in body["nodes_list"]]
    assert len(ids) == len(set(ids)) == 1413


def test_node_rows_carry_the_documented_fields(client):
    body = baseline(client)
    row = next(row for row in body["nodes_list"] if row["Node"] == "I-4")
    assert set(row) - {"Node"} == {
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
        "Exposure_Basis",
        "Exposure_Distance_m",
        # The two combined.
        "Risk_Score",
    }


def test_metadata_does_not_leak_server_paths(client):
    metadata = baseline(client)["metadata"]
    for key in ("rpt_file", "out_file"):
        assert "/" not in metadata[key] and "\\" not in metadata[key]


def test_a_request_during_shutdown_is_a_503_not_a_crash():
    app = make_app()
    with TestClient(app):
        pass  # leaving the block runs shutdown, which closes the job store
    response = TestClient(app).post("/simulations", json={}, headers=AS_A)
    assert response.status_code == 503
    assert int(response.headers["retry-after"]) > 0


def test_the_deprecated_synchronous_endpoint_is_gone(client):
    """It held a request open for the whole run and had no sign-in."""
    assert client.post("/run-simulation", json={}).status_code in {404, 405}
    assert "/run-simulation" not in client.get("/openapi.json").json()["paths"]


class TestSignIn:
    """Runs cost minutes of CPU, so only signed-in users may start them."""

    def test_no_token_is_a_401(self):
        with TestClient(make_app()) as anonymous:
            response = anonymous.post("/simulations", json={})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    def test_an_unknown_token_is_a_401(self):
        with TestClient(make_app()) as stranger:
            response = stranger.post(
                "/simulations", json={}, headers={"Authorization": "Bearer forged"}
            )
        assert response.status_code == 401

    def test_polling_needs_a_sign_in_too(self, client):
        poll_url = client.post("/simulations", json={}).json()["poll_url"]
        assert client.get(poll_url, headers={"Authorization": ""}).status_code == 401

    def test_someone_elses_job_is_not_found(self, client):
        poll_url = client.post("/simulations", json={}).json()["poll_url"]
        assert client.get(poll_url, headers=AS_B).status_code == 404

    def test_a_server_without_auth_settings_refuses_rather_than_opens(self):
        app = create_app(replace(TEST_SETTINGS, supabase_url=None, supabase_anon_key=None))
        with TestClient(app) as misconfigured:
            response = misconfigured.post("/simulations", json={}, headers=AS_A)
        assert response.status_code == 503

    def test_an_auth_outage_is_a_503(self):
        with TestClient(make_app(BrokenAuthenticator())) as outage:
            response = outage.post("/simulations", json={}, headers=AS_A)
        assert response.status_code == 503

    def test_local_development_can_turn_sign_in_off(self):
        app = create_app(replace(TEST_SETTINGS, require_auth=False))
        with TestClient(app) as local:
            poll_url = local.post("/simulations", json={}).json()["poll_url"]
            assert poll_until_finished(local, poll_url)["status"] == "succeeded"


class TestPerUserLimits:
    def test_one_run_at_a_time_per_person(self, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr("app.main._simulate", lambda request: (release.wait(10), {})[1])
        app = make_app(max_jobs_per_user=1)
        try:
            with TestClient(app) as limited:
                assert limited.post("/simulations", json={}, headers=AS_A).status_code == 202

                second = limited.post("/simulations", json={}, headers=AS_A)
                assert second.status_code == 429
                assert "already have a simulation" in second.json()["detail"]
                assert int(second.headers["retry-after"]) >= 60

                # Someone else is not held back by it.
                assert limited.post("/simulations", json={}, headers=AS_B).status_code == 202
        finally:
            release.set()

    def test_an_hourly_allowance_per_person(self, monkeypatch):
        monkeypatch.setattr("app.main._simulate", lambda request: {})
        app = make_app(max_runs_per_user_per_hour=2)
        with TestClient(app, headers=AS_A) as limited:
            for _ in range(2):
                poll_url = limited.post("/simulations", json={}).json()["poll_url"]
                poll_until_finished(limited, poll_url)

            refused = limited.post("/simulations", json={})
            assert refused.status_code == 429
            assert "past hour" in refused.json()["detail"]
            assert limited.post("/simulations", json={}, headers=AS_B).status_code == 202


class TestOverridesAreChecked:
    """SWMM takes whatever it is given, so the API checks first."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"rainfall": {"total_precip": 10, "duration_hr": 48}},  # beyond the 24 h window
            {"rainfall": {"total_precip": 10, "duration_hr": 0}},  # zero-length storm
            {"rainfall": {"total_precip": -5, "duration_hr": 2}},  # negative depth
            {"rainfall": {"total_precip": 10}},  # missing duration
            {"rainfall": {"total_precip": 1e9, "duration_hr": 2}},  # absurd depth
            {"nodes": {"I-4": {"inv_elev": -3}}},  # below the network's datum
            {"nodes": {"I-4": {"ponding_area": 1e12}}},  # absurd area
            {"nodes": {"I-4": {"surcharge_depth": -1}}},
            {"nodes": {"I-4": {"inv_elev": "NaN"}}},  # not a finite number
            {"nodes": {"I-4": {"invert": 5}}},  # misspelt field
            {"links": {"C-88": {"init_flow": -1}}},
            {"links": {"C-88": {"avg_conduit_loss": 1e6}}},
            {"links": {"": {"init_flow": 1}}},  # an empty id would match every link
            {"extra": {}},
        ],
    )
    def test_an_impossible_request_is_rejected_before_it_is_queued(self, client, payload):
        # Validation failures should not cost a queue slot.
        assert client.post("/simulations", json=payload).status_code == 422

    def test_an_unknown_node_is_rejected_by_name(self, client):
        response = client.post("/simulations", json={"nodes": {"I-99999": {"inv_elev": 5}}})
        assert response.status_code == 422
        assert "I-99999" in response.json()["detail"]

    def test_an_unknown_link_is_rejected_by_name(self, client):
        response = client.post("/simulations", json={"links": {"C-XYZ": {"init_flow": 1}}})
        assert response.status_code == 422
        assert "C-XYZ" in response.json()["detail"]

    def test_real_nodes_and_links_are_accepted(self, client, monkeypatch):
        seen = []
        monkeypatch.setattr("app.main._simulate", lambda request: seen.append(request) or {})
        finished = run(
            client,
            {
                "nodes": {"I-4": {"inv_elev": 16, "init_depth": 0}},
                "links": {"C-88": {"init_flow": 2.5}},
            },
        )
        assert finished["status"] == "succeeded"
        assert seen[0].node_overrides() == {"I-4": {"inv_elev": 16.0, "init_depth": 0.0}}


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

    def test_a_finished_job_reports_when_it_ran(self, client):
        finished = run(client, {})

        assert finished["created_at"] is not None
        assert finished["started_at"] is not None
        assert finished["finished_at"] is not None

    def test_polling_an_unknown_job_is_a_404(self, client):
        response = client.get("/simulations/does-not-exist")
        assert response.status_code == 404
        assert "expired" in response.json()["detail"]

    def test_a_failing_simulation_finishes_as_failed(self, client, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("SWMM exploded")

        monkeypatch.setattr("app.main.run_simulation", explode)
        finished = run(client, {})

        assert finished["status"] == "failed"
        assert finished["result"] is None
        assert "SWMM exploded" in finished["error"]

    def test_a_full_queue_is_rejected_with_429(self, monkeypatch):
        # One worker and one slot, held by a simulation that will not return.
        release = threading.Event()
        monkeypatch.setattr("app.main._simulate", lambda request: (release.wait(10), {})[1])
        app = make_app(max_concurrent_simulations=1, max_queued_simulations=1)
        try:
            with TestClient(app) as full_client:
                assert full_client.post("/simulations", json={}, headers=AS_A).status_code == 202

                # A different person, so it is the server's cap that refuses.
                response = full_client.post("/simulations", json={}, headers=AS_B)
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
        app = make_app(max_runtime_seconds=0)
        try:
            with TestClient(app, headers=AS_A) as slow_client:
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
        app = make_app(
            max_concurrent_simulations=1,
            max_queued_simulations=2,
            # Long enough that the second, quick run is not also reaped.
            max_runtime_seconds=1,
        )
        try:
            with TestClient(app, headers=AS_A) as slow_client:
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
        assert baseline(client)["metadata"]["event_hours"] == 24.0

    def test_a_custom_storm_reports_its_own_length(self, client, monkeypatch):
        captured = {}
        real = flooding_summary_for_test

        def spy(rpt_path, out_path, *args, **kwargs):
            captured["event_hours"] = kwargs.get("event_hours")
            return real(rpt_path, out_path, *args, **kwargs)

        monkeypatch.setattr("app.main.build_flooding_summary", spy)
        run(client, {"rainfall": {"total_precip": 0, "duration_hr": 2}})
        assert captured["event_hours"] == 2.0


def test_browsers_may_read_the_polling_headers(client):
    """Regression: CORS did not expose Retry-After or Location, so a browser
    client could not read either, and had to guess how long to wait."""
    response = client.post(
        "/simulations", json={}, headers={"Origin": "https://project-drain.vercel.app"}
    )
    exposed = {
        header.strip().lower()
        for header in response.headers["access-control-expose-headers"].split(",")
    }
    assert {"retry-after", "location"} <= exposed


@pytest.mark.parametrize(
    "origin",
    [
        "https://drain-aws7ldd79-kiloumanjaros-projects.vercel.app",
        "https://drain-git-develop-kiloumanjaros-projects.vercel.app",
    ],
)
def test_our_vercel_previews_may_call_the_api(client, origin):
    response = client.get("/health", headers={"Origin": origin})
    assert response.headers.get("access-control-allow-origin") == origin


@pytest.mark.parametrize(
    "origin",
    [
        # Someone else's project named drain-*, in another Vercel team.
        "https://drain-aws7ldd79-someone-else.vercel.app",
        "https://drain-anything.vercel.app",
        # The slug has to end the hostname, not sit in the middle of it.
        "https://drain-x-kiloumanjaros-projects-evil.vercel.app",
    ],
)
def test_other_vercel_projects_may_not(client, origin):
    response = client.get("/health", headers={"Origin": origin})
    assert "access-control-allow-origin" not in response.headers


def test_finished_results_are_compressed_for_clients_that_accept_gzip(client):
    response = client.post("/simulations", json={})
    poll_url = response.json()["poll_url"]
    poll_until_finished(client, poll_url)

    compressed = client.get(poll_url, headers={"Accept-Encoding": "gzip"})
    assert compressed.headers.get("content-encoding") == "gzip"
    assert compressed.json()["status"] == "succeeded"

    plain = client.get(poll_url, headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
