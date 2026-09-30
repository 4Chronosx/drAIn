"""Who is asking for a simulation.

A run is minutes of CPU on a small instance, so the API only runs them for
people signed in to the app. The browser sends its Supabase access token as
``Authorization: Bearer <token>``; this module asks Supabase Auth whose it
is. CORS never did this job: it is a browser courtesy, and ``curl`` ignores
it.

Asking Supabase (``GET /auth/v1/user``) rather than checking the token's
signature here works for every Supabase signing setup and notices a revoked
session. Answers are cached for a minute, because a client polls every few
seconds while it waits for a run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Caller:
    """A signed-in user."""

    user_id: str


class AuthUnavailableError(RuntimeError):
    """Raised when the sign-in check itself could not be made."""


class Authenticator(Protocol):
    def authenticate(self, token: str) -> Caller | None:
        """The token's owner, or ``None`` if the token is not valid.

        Raises :class:`AuthUnavailableError` when validity can't be decided.
        """


#: Takes (url, headers) and returns (HTTP status, body). Injected in tests.
Fetch = Callable[[str, dict[str, str]], tuple[int, bytes]]


def _urllib_fetch(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise AuthUnavailableError(f"Could not reach Supabase Auth: {error}") from error


class SupabaseAuthenticator:
    """Checks access tokens with Supabase Auth, caching answers briefly."""

    def __init__(
        self,
        supabase_url: str,
        api_key: str,
        cache_seconds: float = 60.0,
        fetch: Fetch = _urllib_fetch,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._user_url = f"{supabase_url.rstrip('/')}/auth/v1/user"
        self._api_key = api_key
        self._cache_seconds = cache_seconds
        self._fetch = fetch
        self._clock = clock
        # Keyed by a hash so raw tokens are not kept in memory longer than
        # the request that carried them.
        self._cache: dict[str, tuple[float, Caller | None]] = {}
        self._lock = threading.Lock()

    def authenticate(self, token: str) -> Caller | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]

        status, body = self._fetch(
            self._user_url,
            {"apikey": self._api_key, "Authorization": f"Bearer {token}"},
        )
        if status == 200:
            try:
                caller: Caller | None = Caller(user_id=str(json.loads(body)["id"]))
            except (ValueError, KeyError, TypeError) as error:
                raise AuthUnavailableError("Supabase Auth sent an unreadable user.") from error
        elif status in (401, 403):
            caller = None
        else:
            raise AuthUnavailableError(f"Supabase Auth answered HTTP {status}.")

        with self._lock:
            # Drop expired entries so the cache can't grow without bound.
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            self._cache[key] = (now + self._cache_seconds, caller)
        return caller


class OpenAuthenticator:
    """Lets everyone in as one shared caller. Local development only.

    Used when ``REQUIRE_AUTH=false``, so the API can be run without a
    Supabase project. Never set that on a deployed server.
    """

    CALLER = Caller(user_id="local-dev")

    def authenticate(self, token: str) -> Caller | None:
        return self.CALLER
