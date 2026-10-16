"""Tests for the HTTP layer."""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import polling as app_polling
from app.auth import AccountRefusedError, AuthUnavailableError, Caller, SupabaseAuthenticator
from app.config import VERCEL_PREVIEW_ORIGIN_REGEX, settings
from app.main import _build_authenticator, create_app
from app.schemas import MAX_NODE_OVERRIDES, SimulationRequest
from app.simulation import baseline_result, is_baseline
from drain.flooding import build_flooding_summary as flooding_summary_for_test
from drain.network import link_suffixes, node_ids

#: Two known users; any other token is invalid.
USERS = {"token-a": Caller("user-a"), "token-b": Caller("user-b")}


class FakeAuthenticator:
    def authenticate(self, token: str) -> Caller | None:
        return USERS.get(token)


class BrokenAuthenticator:
    def authenticate(self, token: str) -> Caller | None:
        raise AuthUnavailableError("Supabase is down")


#: Generous per-user and per-address limits, so tests that queue several
#: runs and poll quickly aren't held back by them. The tests of the limits
#: set their own. Simulations run in-process so tests can stub them;
#: tests/test_isolation.py covers the child process.
TEST_SETTINGS = replace(
    settings,
    require_auth=True,
    max_jobs_per_user=100,
    max_runs_per_user_per_hour=1000,
    max_jobs_per_ip=100,
    queue_slots_reserved=0,
    submit_rate_per_minute=0,
    poll_rate_per_minute=0,
    isolate_simulations=False,
)

AS_A = {"Authorization": "Bearer token-a"}
AS_B = {"Authorization": "Bearer token-b"}


def make_app(authenticator=None, environ=None, **overrides):
    return create_app(
        replace(TEST_SETTINGS, **overrides),
        authenticator=authenticator or FakeAuthenticator(),
        # Empty unless a test says where it is deployed.
        environ=environ or {},
    )


@pytest.fixture(scope="module")
def client():
    with TestClient(make_app(), headers=AS_A) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def preview_client():
    """A client for a server that lets Vercel previews in, as one does with
    ALLOWED_ORIGIN_REGEX set to the preview pattern."""
    app = make_app(origin_regex=VERCEL_PREVIEW_ORIGIN_REGEX)
    with TestClient(app, headers=AS_A) as test_client:
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


def test_health_does_not_wait_for_a_worker_thread(client):
    # A plain function would queue behind requests blocked on Supabase.
    route = next(route for route in client.app.routes if route.path == "/health")
    assert inspect.iscoroutinefunction(route.endpoint)


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
    assert "/run-simulation" not in client.app.openapi()["paths"]


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

    def test_an_auth_outage_is_logged_as_one_line(self, caplog):
        # Every request during an outage lands here; a traceback for each
        # buried everything else in the log.
        with TestClient(make_app(BrokenAuthenticator())) as outage:
            # Start-up reconfigures logging, so listen only once it has.
            logging.getLogger("app.main").addHandler(caplog.handler)
            try:
                outage.post("/simulations", json={}, headers=AS_A)
            finally:
                logging.getLogger("app.main").removeHandler(caplog.handler)
        records = [record for record in caplog.records if "sign-in" in record.getMessage()]
        assert [record.levelno for record in records] == [logging.WARNING]
        assert "Supabase is down" in records[0].getMessage()
        assert records[0].exc_info is None

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

        monkeypatch.setattr("app.simulation.run_simulation", explode)
        finished = run(client, {"rainfall": {"total_precip": 10, "duration_hr": 1}})

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

        monkeypatch.setattr("app.simulation.build_flooding_summary", spy)
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
def test_our_vercel_previews_are_refused_by_default(client, origin):
    response = client.get("/health", headers={"Origin": origin})
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "origin",
    [
        "https://drain-aws7ldd79-kiloumanjaros-projects.vercel.app",
        "https://drain-git-develop-kiloumanjaros-projects.vercel.app",
    ],
)
def test_our_vercel_previews_may_call_the_api_when_allowed(preview_client, origin):
    response = preview_client.get("/health", headers={"Origin": origin})
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
def test_other_vercel_projects_may_not(preview_client, origin):
    response = preview_client.get("/health", headers={"Origin": origin})
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


class TestRequestSize:
    """FastAPI reads the whole body before the sign-in check, so without a
    cap anyone could make the server hold and parse as much as they sent."""

    def test_a_declared_oversized_body_is_refused_unread(self):
        with TestClient(make_app(max_request_bytes=1000), headers=AS_A) as small:
            response = small.post(
                "/simulations",
                content=b"{" + b" " * 2000 + b"}",
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code == 413

    def test_a_streamed_body_is_cut_off_at_the_limit(self):
        def chunks():
            yield b'{"nodes": {'
            for _ in range(100):
                yield b" " * 100

        with TestClient(make_app(max_request_bytes=1000), headers=AS_A) as small:
            response = small.post(
                "/simulations", content=chunks(), headers={"Content-Type": "application/json"}
            )
        assert response.status_code == 413

    def test_the_largest_real_request_fits_the_default(self):
        # Every node and link, every field, long floats: what the limit is
        # sized from.
        long = 1.2345678901234567
        node = dict.fromkeys(("inv_elev", "init_depth", "surcharge_depth"), long)
        link = dict.fromkeys(
            ("init_flow", "upstrm_offset_depth", "downstrm_offset_depth", "avg_conduit_loss"),
            long,
        )
        payload = {
            "nodes": {n: {**node, "ponding_area": 1234.5678901234567} for n in node_ids()},
            "links": dict.fromkeys(link_suffixes(), link),
            "rainfall": {"total_precip": 1234.5678901234567, "duration_hr": 23.456789012345678},
        }
        body = json.dumps(payload, indent=2).encode()
        assert len(body) < settings.max_request_bytes
        request = SimulationRequest.model_validate(payload)
        assert len(request.nodes) == len(node_ids())

    def test_more_overrides_than_the_network_could_have_are_refused(self, client):
        payload = {"nodes": {f"I-{n}": {} for n in range(MAX_NODE_OVERRIDES + 1)}}
        assert client.post("/simulations", json=payload).status_code == 422


class TestRateLimits:
    """Per client address, checked before any body is read or sign-in asked."""

    def test_starting_runs_is_limited_per_address(self, monkeypatch):
        monkeypatch.setattr("app.main._simulate", lambda request: {})
        with TestClient(make_app(submit_rate_per_minute=2), headers=AS_A) as limited:
            for _ in range(2):
                assert limited.post("/simulations", json={}).status_code == 202
            refused = limited.post("/simulations", json={})
        assert refused.status_code == 429
        assert int(refused.headers["retry-after"]) >= 1

    def test_polling_is_limited_separately(self):
        with TestClient(make_app(poll_rate_per_minute=3), headers=AS_A) as limited:
            codes = [limited.get("/simulations/nope").status_code for _ in range(4)]
            assert codes == [404, 404, 404, 429]
            assert limited.post("/simulations", json={}).status_code == 202

    def test_made_up_tokens_count_too(self):
        """They are what the limit is for."""
        with TestClient(make_app(poll_rate_per_minute=1)) as limited:
            forged = {"Authorization": "Bearer forged"}
            assert limited.get("/simulations/x", headers=forged).status_code == 401
            assert limited.get("/simulations/x", headers=forged).status_code == 429

    def test_behind_a_proxy_each_forwarded_address_has_its_own_limit(self):
        app = make_app(poll_rate_per_minute=1, trusted_proxy_hops=1)
        with TestClient(app, headers=AS_A) as limited:
            first = {"X-Forwarded-For": "203.0.113.1"}
            second = {"X-Forwarded-For": "203.0.113.2"}
            assert limited.get("/simulations/x", headers=first).status_code == 404
            assert limited.get("/simulations/x", headers=first).status_code == 429
            assert limited.get("/simulations/x", headers=second).status_code == 404

    def test_a_caller_cannot_choose_their_forwarded_address(self):
        # The proxy appends the address it saw; whatever the caller wrote
        # before it is ignored.
        app = make_app(poll_rate_per_minute=1, trusted_proxy_hops=1)
        with TestClient(app, headers=AS_A) as limited:
            real = "198.51.100.7"
            spoofed = {"X-Forwarded-For": f"203.0.113.1, {real}"}
            respoofed = {"X-Forwarded-For": f"203.0.113.9, {real}"}
            assert limited.get("/simulations/x", headers=spoofed).status_code == 404
            assert limited.get("/simulations/x", headers=respoofed).status_code == 429

    def test_without_trusted_proxies_the_header_is_ignored(self):
        with TestClient(make_app(poll_rate_per_minute=1), headers=AS_A) as limited:
            first = limited.get("/simulations/x", headers={"X-Forwarded-For": "192.0.2.1"})
            second = limited.get("/simulations/x", headers={"X-Forwarded-For": "192.0.2.2"})
        assert (first.status_code, second.status_code) == (404, 429)

    def test_health_is_not_limited(self):
        with TestClient(make_app(poll_rate_per_minute=1, submit_rate_per_minute=1)) as limited:
            assert all(limited.get("/health").status_code == 200 for _ in range(5))


class TestPerAddressQueueShare:
    def test_one_address_cannot_fill_the_queue_with_several_accounts(self, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr("app.main._simulate", lambda request: (release.wait(10), {})[1])
        app = make_app(max_jobs_per_user=1, max_jobs_per_ip=1)
        try:
            with TestClient(app) as shared:
                assert shared.post("/simulations", json={}, headers=AS_A).status_code == 202
                refused = shared.post("/simulations", json={}, headers=AS_B)
                assert refused.status_code == 429
                assert "your network" in refused.json()["detail"]
        finally:
            release.set()


class TestAccountChecks:
    def test_an_account_the_server_refuses_is_a_403(self):
        class Refusing:
            def authenticate(self, token):
                raise AccountRefusedError("Confirm your email address before running simulations.")

        with TestClient(make_app(Refusing()), headers=AS_A) as refused:
            response = refused.post("/simulations", json={})
        assert response.status_code == 403
        assert "Confirm your email" in response.json()["detail"]


class TestApiDocs:
    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    def test_docs_are_off_by_default(self, client, path):
        assert client.get(path).status_code == 404

    def test_docs_can_be_turned_on(self):
        with TestClient(make_app(enable_docs=True)) as documented:
            assert documented.get("/docs").status_code == 200
            assert "/simulations" in documented.get("/openapi.json").json()["paths"]


class TestResponseHeaders:
    def test_every_response_says_not_to_sniff_or_store(self, client):
        response = client.post("/simulations", json={})
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"

    def test_a_refusal_from_middleware_gets_them_too(self):
        with TestClient(make_app(max_request_bytes=10), headers=AS_A) as small:
            response = small.post("/simulations", json={"nodes": {}, "links": {}})
        assert response.status_code == 413
        assert response.headers["x-content-type-options"] == "nosniff"


class TestFinishedResultsAreCached:
    """A finished result is 0.7 MB, polled every few seconds. It used to be
    rebuilt, serialised and gzipped again for every poll."""

    def finished(self, client):
        poll_url = client.post("/simulations", json={}).json()["poll_url"]
        poll_until_finished(client, poll_url)
        return poll_url

    def test_a_finished_result_has_an_etag_and_answers_304(self, client):
        poll_url = self.finished(client)
        first = client.get(poll_url)
        etag = first.headers["etag"]
        assert first.headers["cache-control"] == "private, no-cache"

        again = client.get(poll_url, headers={"If-None-Match": etag})
        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["etag"] == etag

    def test_the_etag_differs_by_encoding(self, client):
        poll_url = self.finished(client)
        zipped = client.get(poll_url, headers={"Accept-Encoding": "gzip"})
        plain = client.get(poll_url, headers={"Accept-Encoding": "identity"})
        assert zipped.headers["etag"] != plain.headers["etag"]
        assert zipped.json() == plain.json()

    def test_a_cached_result_is_still_only_its_owners(self, client):
        poll_url = self.finished(client)
        etag = client.get(poll_url).headers["etag"]
        assert client.get(poll_url, headers=AS_B).status_code == 404
        assert client.get(poll_url, headers={**AS_B, "If-None-Match": etag}).status_code == 404

    def test_an_unfinished_job_is_not_cached(self, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr("app.main._simulate", lambda request: (release.wait(10), {})[1])
        try:
            with TestClient(make_app(), headers=AS_A) as slow:
                poll_url = slow.post("/simulations", json={}).json()["poll_url"]
                response = slow.get(poll_url)
                assert "etag" not in response.headers
                assert response.headers["cache-control"] == "no-store"
        finally:
            release.set()

    def test_the_result_is_serialised_once(self, client, monkeypatch):
        poll_url = self.finished(client)
        client.get(poll_url)
        calls = []
        real = app_polling.render
        monkeypatch.setattr("app.main.render", lambda job: calls.append(job) or real(job))
        for _ in range(3):
            assert client.get(poll_url).status_code == 200
        assert calls == []

    def test_unmodified_runs_share_one_result(self, client):
        first = self.finished(client)
        second = self.finished(client)
        a, b = (client.get(url).json() for url in (first, second))
        assert a["result"] == b["result"]
        assert a["job_id"] != b["job_id"]
        assert is_baseline(baseline_result())


@pytest.mark.parametrize(
    "origin",
    [
        # A stranger's project named "drain-x-kiloumanjaros-projects" gets
        # this hostname. The old pattern let it in.
        "https://drain-x-kiloumanjaros-projects.vercel.app",
        "https://evil-kiloumanjaros-projects.vercel.app",
        "https://drain-kiloumanjaros-projects.vercel.app",
        "https://drain-abc-kiloumanjaros-projects.vercel.app",
        "https://drain-aws7ldd79x-kiloumanjaros-projects.vercel.app",
        "https://notdrain-aws7ldd79-kiloumanjaros-projects.vercel.app",
        "https://drain-git--kiloumanjaros-projects.vercel.app",
        "http://drain-aws7ldd79-kiloumanjaros-projects.vercel.app",
    ],
)
def test_lookalike_hostnames_in_our_team_slug_may_not(preview_client, origin):
    response = preview_client.get("/health", headers={"Origin": origin})
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "origin",
    [
        "https://pjdsc-drain-git-feature-x-kiloumanjaros-projects.vercel.app",
        "https://ai-drain-0123abcde-kiloumanjaros-projects.vercel.app",
        "https://project-drain.vercel.app",
    ],
)
def test_real_preview_and_production_hostnames_may_when_allowed(preview_client, origin):
    response = preview_client.get("/health", headers={"Origin": origin})
    assert response.headers.get("access-control-allow-origin") == origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://pjdsc-drain.vercel.app",
        "https://project-drain.vercel.app",
        "https://ai-drain.vercel.app",
    ],
)
def test_production_hostnames_may_by_default(client, origin):
    response = client.get("/health", headers={"Origin": origin})
    assert response.headers.get("access-control-allow-origin") == origin


#: Render sets this in the environment of every service it runs.
ON_RENDER = {"RENDER": "true"}

#: What a deployment sets, over the tests' own settings (which turn the
#: rate limits off).
DEPLOYED = {
    "trusted_proxy_hops": 1,
    "supabase_url": "https://p.supabase.co",
    "supabase_anon_key": "anon-key",
    "supabase_service_role_key": None,
    "require_confirmed_email": True,
    "submit_rate_per_minute": 6,
    "poll_rate_per_minute": 120,
}


class TestBuildingTheAuthenticator:
    @pytest.mark.parametrize("refuse", [False, True])
    def test_refusing_hs256_tokens_follows_the_setting(self, refuse):
        config = replace(TEST_SETTINGS, **DEPLOYED, refuse_hs256_tokens=refuse)
        built = _build_authenticator(config)
        assert isinstance(built, SupabaseAuthenticator)
        assert built._refuse_hs256 is refuse

    def test_turning_it_on_does_not_stop_a_deployment_starting(self):
        app = make_app(environ=ON_RENDER, **DEPLOYED, refuse_hs256_tokens=True)
        with TestClient(app) as deployed:
            assert deployed.get("/health").status_code == 200


class TestDeploymentGuard:
    """Settings that suit a laptop reached the deployed server and it
    started anyway: open to anyone, or with every caller sharing the
    proxy's rate limit. On Render it now refuses to start."""

    def deployed(self, environ=ON_RENDER, **overrides):
        return make_app(environ=environ, **{**DEPLOYED, **overrides})

    def test_a_deployment_without_sign_in_does_not_start(self):
        app = self.deployed(require_auth=False)
        with pytest.raises(RuntimeError, match="REQUIRE_AUTH"), TestClient(app):
            pass

    def test_a_deployment_that_cannot_tell_callers_apart_does_not_start(self):
        app = self.deployed(trusted_proxy_hops=0)
        with pytest.raises(RuntimeError, match="TRUSTED_PROXY_HOPS"), TestClient(app):
            pass

    @pytest.mark.parametrize("missing", ["supabase_url", "supabase_anon_key"])
    def test_a_deployment_that_cannot_sign_anyone_in_does_not_start(self, missing):
        """Regression: it started, passed its health check, and answered
        every simulation with 503."""
        app = self.deployed(**{missing: None})
        with pytest.raises(RuntimeError, match=missing.upper()), TestClient(app):
            pass

    def test_a_deployment_open_to_unconfirmed_accounts_does_not_start(self):
        app = self.deployed(require_confirmed_email=False)
        with pytest.raises(RuntimeError, match="REQUIRE_CONFIRMED_EMAIL"), TestClient(app):
            pass

    @pytest.mark.parametrize(
        ("setting", "variable"),
        [
            ("submit_rate_per_minute", "SUBMIT_RATE_LIMIT_PER_MINUTE"),
            ("poll_rate_per_minute", "POLL_RATE_LIMIT_PER_MINUTE"),
            ("max_jobs_per_user", "MAX_JOBS_PER_USER"),
            ("max_runs_per_user_per_hour", "MAX_RUNS_PER_USER_PER_HOUR"),
            ("max_jobs_per_ip", "MAX_JOBS_PER_IP"),
        ],
    )
    def test_a_deployment_with_a_limit_at_zero_does_not_start(self, setting, variable):
        app = self.deployed(**{setting: 0})
        with pytest.raises(RuntimeError, match=variable), TestClient(app):
            pass

    def test_a_sound_deployment_starts(self):
        with TestClient(self.deployed()) as deployed:
            assert deployed.get("/health").status_code == 200

    def test_a_deployment_that_keeps_runs_in_memory_starts_with_a_warning(self, caplog):
        # Built from the settings alone, as the server is. No service-role
        # key: it works, but forgets its runs when it restarts.
        config = replace(TEST_SETTINGS, **DEPLOYED)
        with caplog.at_level(logging.WARNING, logger="app.main"):
            app = create_app(config, environ=ON_RENDER)
        warned = [record for record in caplog.records if record.levelno == logging.WARNING]
        assert any("SUPABASE_SERVICE_ROLE_KEY" in record.getMessage() for record in warned)
        with TestClient(app) as deployed:
            assert deployed.get("/health").status_code == 200

    def test_the_same_settings_start_anywhere_else(self):
        app = make_app(environ={}, require_auth=False, trusted_proxy_hops=0)
        with TestClient(app) as local:
            assert local.get("/health").status_code == 200

    def test_the_override_starts_it_anyway(self):
        environ = {**ON_RENDER, "ALLOW_INSECURE_DEPLOY": "true"}
        app = self.deployed(environ, trusted_proxy_hops=0, require_confirmed_email=False)
        with TestClient(app) as deployed:
            assert deployed.get("/health").status_code == 200

    @pytest.mark.parametrize("value", ["false", "0", ""])
    def test_an_override_that_is_off_does_not(self, value):
        environ = {**ON_RENDER, "ALLOW_INSECURE_DEPLOY": value}
        app = self.deployed(environ, trusted_proxy_hops=0)
        with pytest.raises(RuntimeError, match="Refusing to start"), TestClient(app):
            pass

    def test_each_problem_is_logged(self, monkeypatch):
        logged = []
        monkeypatch.setattr(
            "app.main.logger.log", lambda level, message, *args: logged.append((level, args[0]))
        )
        app = self.deployed(require_auth=False, trusted_proxy_hops=0)
        with pytest.raises(RuntimeError), TestClient(app):
            pass
        assert [level for level, _ in logged] == [logging.ERROR, logging.ERROR]

        logged.clear()
        environ = {**ON_RENDER, "ALLOW_INSECURE_DEPLOY": "yes"}
        app = self.deployed(environ, require_auth=False, trusted_proxy_hops=0)
        with TestClient(app):
            pass
        assert [level for level, _ in logged] == [logging.WARNING, logging.WARNING]
        assert "REQUIRE_AUTH" in logged[0][1] and "TRUSTED_PROXY_HOPS" in logged[1][1]
