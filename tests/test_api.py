"""Tests for the HTTP layer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


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
        "Hours_Flooded",
        "Maximum_Rate_CMS",
        "Time_of_Max_days",
        "Time_of_Max_hr_min",
        "Total_Flood_Volume_10e6_ltr",
        "Time_After_Raining_min",
        "Vulnerability_Category",
        "Vulnerability_Score",
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
