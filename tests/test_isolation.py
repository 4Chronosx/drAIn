"""Tests for running simulations in a child process that can be killed.

These start real processes. The functions they run are defined at module
level because a spawned child imports them by name.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import isolation
from app.config import Settings
from app.isolation import SimulationTimeoutError, run_isolated
from tests.test_api import AS_A, make_app, poll_until_finished


def double(value):
    return {"doubled": value * 2, "pid": os.getpid()}


def explode(message):
    raise ValueError(message)


def die(code):
    os._exit(code)


def heartbeat_forever(path):
    """Append to a file every 20 ms until killed."""
    while True:
        with Path(path).open("a") as beats:
            beats.write(".")
        time.sleep(0.02)


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout}s")


def test_the_result_comes_back_from_another_process():
    result = run_isolated(double, 21, timeout=60)
    assert result["doubled"] == 42
    assert result["pid"] != os.getpid()


def test_a_failure_comes_back_as_its_message():
    with pytest.raises(RuntimeError, match="SWMM exploded"):
        run_isolated(explode, "SWMM exploded", timeout=60)


def test_a_child_that_dies_is_reported_not_waited_on():
    with pytest.raises(RuntimeError, match="stopped unexpectedly"):
        run_isolated(die, 3, timeout=60)


def test_a_child_that_overruns_is_killed(tmp_path):
    """Regression: an overrunning run was only marked failed. Its thread
    could not be stopped, so it kept a core busy until SWMM finished."""
    beats = tmp_path / "beats"
    started = time.monotonic()
    with pytest.raises(SimulationTimeoutError):
        run_isolated(heartbeat_forever, str(beats), timeout=3)
    assert time.monotonic() - started < 15

    # The child is gone: nothing is still writing.
    size = beats.stat().st_size
    time.sleep(0.3)
    assert beats.stat().st_size == size
    assert not isolation._live


@pytest.fixture
def isolated_app():
    """The app as deployed: real simulations in a child process."""
    return make_app(isolate_simulations=True)


def test_a_real_simulation_runs_in_a_child_process(isolated_app):
    with TestClient(isolated_app, headers=AS_A) as client:
        payload = {"rainfall": {"total_precip": 20, "duration_hr": 1}}
        poll_url = client.post("/simulations", json=payload).json()["poll_url"]
        finished = poll_until_finished(client, poll_url, timeout=120)
    assert finished["status"] == "succeeded", finished["error"]
    assert finished["result"]["metadata"]["event_hours"] == 1.0
    assert len(finished["result"]["nodes_list"]) == 1413


def test_a_simulation_past_its_time_limit_is_killed():
    app = make_app(isolate_simulations=True, max_runtime_seconds=1)
    with TestClient(app, headers=AS_A) as client:
        payload = {"rainfall": {"total_precip": 400, "duration_hr": 24}}
        poll_url = client.post("/simulations", json=payload).json()["poll_url"]
        finished = poll_until_finished(client, poll_url, timeout=30)
        assert finished["status"] == "failed"
        assert "did not finish" in finished["error"]
        # Killed at the same limit, not left running after the job failed.
        wait_until(lambda: not isolation._live)


def test_the_unmodified_network_needs_no_child_process(monkeypatch, isolated_app):
    def refuse(*args, **kwargs):
        raise AssertionError("the baseline should not start a process")

    monkeypatch.setattr(isolation, "run_isolated", refuse)
    with TestClient(isolated_app, headers=AS_A) as client:
        poll_url = client.post("/simulations", json={}).json()["poll_url"]
        assert poll_until_finished(client, poll_url)["status"] == "succeeded"


def test_deployments_isolate_by_default():
    assert Settings().isolate_simulations is True
