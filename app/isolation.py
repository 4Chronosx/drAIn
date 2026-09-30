"""Running a simulation in a child process that can be killed.

A SWMM run used to execute on a thread of the server process. A thread
cannot be stopped, so one that overran was only marked failed: it kept a
core busy until it finished on its own, slowing every run after it. In a
child process a run that overruns is killed outright.

Processes are started with ``spawn`` on every platform, so the child
imports what it needs afresh rather than inheriting a copy of the server's
threads and locks mid-use (what ``fork`` would do on Linux).
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any

logger = logging.getLogger(__name__)

_CONTEXT = multiprocessing.get_context("spawn")

#: Seconds to wait for a killed child to be reaped.
_REAP_SECONDS = 5.0

_live: set[BaseProcess] = set()
_live_lock = threading.Lock()


class SimulationTimeoutError(RuntimeError):
    """Raised when a child process overran and was killed."""


def _child(sender: Connection, function: Callable[[Any], Any], argument: Any, level: int) -> None:
    """The child's entry point: run, and send back (succeeded, value)."""
    from app.logging_config import configure_logging

    configure_logging(logging.getLevelName(level))
    try:
        outcome: tuple[bool, Any] = (True, function(argument))
    except BaseException as error:
        # The exception itself may not survive pickling; its text does.
        logging.getLogger(__name__).exception("Simulation failed in child process")
        outcome = (False, str(error) or error.__class__.__name__)
    try:
        sender.send(outcome)
    finally:
        sender.close()


def run_isolated(function: Callable[[Any], Any], argument: Any, timeout: float) -> Any:
    """``function(argument)`` in a child process, killed after ``timeout`` s.

    ``function`` must be importable by name (a module-level function), and
    its argument and result picklable. Raises :class:`SimulationTimeoutError`
    on a timeout, and :class:`RuntimeError` with the child's message if it
    failed or died.
    """
    receiver, sender = _CONTEXT.Pipe(duplex=False)
    process = _CONTEXT.Process(
        target=_child,
        args=(sender, function, argument, logging.getLogger().getEffectiveLevel()),
        name="simulation",
        daemon=True,
    )
    # Registered before it starts, so a shutdown while a slow spawn is under
    # way still finds it.
    with _live_lock:
        _live.add(process)
    try:
        process.start()
        sender.close()
        # Readable once a result arrives, or once the child dies.
        if not receiver.poll(timeout):
            raise SimulationTimeoutError(
                f"The simulation did not finish within {timeout:.0f} s and was stopped."
            )
        try:
            succeeded, value = receiver.recv()
        except EOFError:
            process.join(_REAP_SECONDS)
            raise RuntimeError(
                f"The simulation process stopped unexpectedly (exit code {process.exitcode})."
            ) from None
    finally:
        sender.close()
        receiver.close()
        if process.pid is not None:
            if process.is_alive():
                process.kill()
            process.join(_REAP_SECONDS)
        with _live_lock:
            _live.discard(process)
    if not succeeded:
        raise RuntimeError(value)
    return value


def kill_all() -> None:
    """Kill every child still running, for a server that is stopping."""
    with _live_lock:
        running = [process for process in _live if process.pid is not None and process.is_alive()]
    for process in running:
        logger.warning("Killing simulation process %s at shutdown", process.pid)
        process.kill()
