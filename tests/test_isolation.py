"""Tests for running simulations in a child process that can be killed.

These start real processes. The functions they run are defined at module
level because a spawned child imports them by name.
"""

from __future__ import annotations

import os
import tempfile
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


class _PatientReceiver:
    """The parent's end of the pipe, which starts waiting for the result
    only once ``started()`` says the child is running."""

    def __init__(self, receiver, started):
        self._receiver = receiver
        self._started = started

    def poll(self, timeout):
        wait_until(self._started, timeout=60.0)
        return self._receiver.poll(timeout)

    def __getattr__(self, name):
        return getattr(self._receiver, name)


@pytest.fixture
def timeout_counted_from(monkeypatch):
    """Count ``run_isolated``'s timeout from when the child is running.

    It is counted from when the process is started, and a spawned child
    takes a second or two to import the app before it runs anything (longer
    on Windows, or a busy machine). With a short timeout the child was
    sometimes killed before its first line ran, and the tests below then
    had nothing to show it had ever been alive. Waiting for the child's own
    sign of life first leaves them timing the kill, not the spawn.
    """

    def arrange(started):
        real_pipe = isolation._CONTEXT.Pipe

        def pipe(duplex=True):
            receiver, sender = real_pipe(duplex=duplex)
            return _PatientReceiver(receiver, started), sender

        monkeypatch.setattr(isolation._CONTEXT, "Pipe", pipe)

    return arrange


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


def test_a_child_that_overruns_is_killed(tmp_path, timeout_counted_from):
    """Regression: an overrunning run was only marked failed. Its thread
    could not be stopped, so it kept a core busy until SWMM finished."""
    beats = tmp_path / "beats"
    running = []

    def has_started():
        if beats.exists() and not running:
            running.append(time.monotonic())
        return bool(running)

    timeout_counted_from(has_started)
    with pytest.raises(SimulationTimeoutError):
        run_isolated(heartbeat_forever, str(beats), timeout=1)
    # Killed at its limit, give or take the reaping.
    assert 1 <= time.monotonic() - running[0] < 12

    # The child is gone: nothing is still writing.
    size = beats.stat().st_size
    assert size > 0
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


def report_temp_directory(_):
    return tempfile.gettempdir()


def write_then_hang(path):
    """Start a run's working directory the way SWMM does, say where it is,
    and never leave it."""
    with tempfile.TemporaryDirectory(prefix="drain-sim-") as workdir:
        (Path(workdir) / "model.out").write_bytes(b"0" * 1024)
        Path(path).write_text(workdir)
        while True:
            time.sleep(0.02)


class TestRunDirectories:
    """Regression: a killed child never left its TemporaryDirectory block,
    so each run that overran left a copy of the network and its partial
    output in the temp directory for good."""

    def test_a_run_gets_a_directory_that_is_gone_when_it_returns(self):
        used = Path(run_isolated(report_temp_directory, None, timeout=60))
        assert used.name.startswith("drain-run-")
        assert used.parent == Path(tempfile.gettempdir())
        assert not used.exists()

    def test_a_killed_runs_files_are_removed(self, tmp_path, timeout_counted_from):
        told = tmp_path / "workdir"

        def has_written():
            # Once the child has said where it is working, and the file it
            # put there is there to be removed.
            return told.exists() and (Path(told.read_text()) / "model.out").is_file()

        timeout_counted_from(has_written)
        with pytest.raises(SimulationTimeoutError):
            run_isolated(write_then_hang, str(told), timeout=1)
        workdir = Path(told.read_text())
        assert workdir.parent.name.startswith("drain-run-")
        assert not workdir.exists() and not workdir.parent.exists()

    def test_a_failed_runs_directory_is_removed_too(self):
        before = set(Path(tempfile.gettempdir()).glob("drain-run-*"))
        with pytest.raises(RuntimeError):
            run_isolated(explode, "SWMM exploded", timeout=60)
        assert set(Path(tempfile.gettempdir()).glob("drain-run-*")) <= before


class TestStartupSweep:
    """A server killed outright removes nothing on its way out, so the next
    one clears what it left."""

    def aged(self, path, seconds):
        path.mkdir()
        (path / "model.inp").write_text("left behind")
        then = time.time() - seconds
        os.utime(path, (then, then))
        return path

    def test_old_run_directories_are_removed(self, tmp_path):
        run = self.aged(tmp_path / "drain-run-abc", 3 * 3600)
        legacy = self.aged(tmp_path / "drain-sim-abc", 3 * 3600)
        assert isolation.sweep_stale_directories(1800, root=tmp_path) == 2
        assert not run.exists() and not legacy.exists()

    def test_anything_else_is_left_alone(self, tmp_path):
        other = self.aged(tmp_path / "someone-elses", 3 * 3600)
        inner = self.aged(tmp_path / "pytest-drain-run-abc", 3 * 3600)
        named_like_one = tmp_path / "drain-run-notes.txt"
        named_like_one.write_text("a file, not a run")
        then = time.time() - 3 * 3600
        os.utime(named_like_one, (then, then))

        assert isolation.sweep_stale_directories(1800, root=tmp_path) == 0
        assert other.exists() and inner.exists() and named_like_one.exists()

    def test_a_directory_still_within_the_run_limit_is_left_alone(self, tmp_path):
        recent = self.aged(tmp_path / "drain-run-abc", 2 * 3600)
        assert isolation.sweep_stale_directories(3 * 3600, root=tmp_path) == 0
        assert recent.exists()

    def test_a_short_run_limit_does_not_make_the_sweep_eager(self, tmp_path):
        recent = self.aged(tmp_path / "drain-run-abc", 600)
        assert isolation.sweep_stale_directories(0, root=tmp_path) == 0
        assert recent.exists()

    def test_a_missing_temp_directory_is_not_an_error(self, tmp_path):
        assert isolation.sweep_stale_directories(1800, root=tmp_path / "gone") == 0

    def test_the_server_sweeps_when_it_starts(self, monkeypatch):
        swept = []
        monkeypatch.setattr(
            isolation, "sweep_stale_directories", lambda older_than: swept.append(older_than)
        )
        with TestClient(make_app(max_runtime_seconds=77)):
            pass
        assert swept == [77]
