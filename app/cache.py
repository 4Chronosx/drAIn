"""A small bounded cache whose entries expire."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

#: Returned by :meth:`ExpiringLru.get` for a key with no live entry, so a
#: cached ``None`` can be told apart from no entry.
MISSING: Any = object()


class ExpiringLru:
    """A bounded map whose entries expire. Safe to share between threads.

    Expiry is checked when an entry is read, and the least recently used
    entry goes when the map is full, so no call ever sweeps the whole map.
    """

    def __init__(self, max_entries: int) -> None:
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def get(self, key: str, now: float) -> Any:
        """The live value for ``key``, or :data:`MISSING`."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return MISSING
            expires, value = entry
            if expires <= now:
                del self._entries[key]
                return MISSING
            self._entries.move_to_end(key)
            return value

    def put(self, key: str, value: Any, expires: float) -> None:
        with self._lock:
            self._entries[key] = (expires, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
