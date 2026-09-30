"""Logging setup.

The pipeline previously reported progress with ``print``, which meant no
levels, no timestamps, and -- because some messages carried emoji -- crashes
on Windows consoles using a legacy code page.
"""

from __future__ import annotations

import contextlib
import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Install a stdout handler that can always encode what it is given."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))

    # Non-UTF-8 consoles would otherwise raise UnicodeEncodeError mid-log.
    reconfigure = getattr(handler.stream, "reconfigure", None)
    if reconfigure is not None:
        with contextlib.suppress(ValueError, OSError):
            reconfigure(errors="backslashreplace")

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
