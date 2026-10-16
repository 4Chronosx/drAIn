"""Running a simulation in a child process that can be killed.

A SWMM run used to execute on a thread of the server process. A thread
cannot be stopped, so one that overran was only marked failed: it kept a
core busy until it finished on its own, slowing every run after it. In a
child process a run that overruns is killed outright.

Processes are started with ``spawn`` on every platform, so the child
imports what it needs afresh rather than inheriting a copy of the server's
threads and locks mid-use (what ``fork`` would do on Linux).

A killed child cleans nothing up, so the copy of the network and the SWMM
output it was writing stayed in the temp directory for good. Each run now
gets a directory made, and removed, by this process; the child is pointed
at it for all its temporary files.
"""

from __future__ import annotations

import logging
import multiprocessing
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONTEXT = multiprocessing.get_context("spawn")

#: Seconds to wait for a killed child to be reaped.
_REAP_SECONDS = 5.0

#: Each run's own directory starts with this.
RUN_DIRECTORY_PREFIX = "drain-run-"

#: Directories a start-up sweep may remove: a run's own, and the ones
#: drain.swmm_runner made directly in the temp directory before runs had
#: their own.
STALE_DIRECTORY_PREFIXES = (RUN_DIRECTORY_PREFIX, "drain-sim-")

#: A sweep leaves anything touched more recently than this alone, whatever
#: the run limit is set to.
MIN_STALE_SECONDS = 3600.0

_live: set[BaseProcess] = set()
_live_lock = threading.Lock()


class SimulationTimeoutError(RuntimeError):
    """Raised when a child process overran and was killed."""


def _child(
    sender: Connection,
    function: Callable[[Any], Any],
    argument: Any,
    level: int,
    workdir: str,
) -> None:
    """The child's entry point: run, and send back (succeeded, value)."""
    from app.logging_config import configure_logging

    configure_logging(logging.getLevelName(level))
    # Everything the run writes to a temporary file lands in the directory
    # the parent removes, including when this process is killed.
    tempfile.tempdir = workdir
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
    workdir = tempfile.mkdtemp(prefix=RUN_DIRECTORY_PREFIX)
    receiver, sender = _CONTEXT.Pipe(duplex=False)
    process = _CONTEXT.Process(
        target=_child,
        args=(sender, function, argument, logging.getLogger().getEffectiveLevel(), workdir),
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
        # After the child is gone, so nothing in it is still open.
        shutil.rmtree(workdir, ignore_errors=True)
    if not succeeded:
        raise RuntimeError(value)
    return value


def sweep_stale_directories(older_than: float, root: str | Path | None = None) -> int:
    """Remove run directories a previous process left in the temp directory.

    A server that is killed outright (out of memory, a redeploy) removes
    nothing on its way out. Only directories named like a run's, directly
    under ``root``, and untouched for ``older_than`` seconds -- never less
    than :data:`MIN_STALE_SECONDS` -- are removed, so a run another process
    has under way is left alone. Returns how many went.
    """
    base = Path(root if root is not None else tempfile.gettempdir())
    cutoff = time.time() - max(older_than, MIN_STALE_SECONDS)
    removed = 0
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(STALE_DIRECTORY_PREFIXES):
            continue
        try:
            if entry.is_symlink() or not entry.is_dir() or entry.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += not entry.exists()
    if removed:
        logger.warning("Removed %d simulation directories left by an earlier process", removed)
    return removed


def kill_all() -> None:
    """Kill every child still running, for a server that is stopping."""
    with _live_lock:
        running = [process for process in _live if process.pid is not None and process.is_alive()]
    for process in running:
        logger.warning("Killing simulation process %s at shutdown", process.pid)
        process.kill()
