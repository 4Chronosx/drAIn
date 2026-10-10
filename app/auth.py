"""Who is asking for a simulation.

A run is minutes of CPU on a small instance, so the API only runs them for
people signed in to the app. The browser sends its Supabase access token as
``Authorization: Bearer <token>``; this module decides whose it is. CORS
never did this job: it is a browser courtesy, and ``curl`` ignores it.

A token goes through up to three checks, cheapest first:

1. **Its shape, locally.** A three-part JWT with an allowed algorithm, a
   future ``exp``, this project's issuer, ``aud`` and ``role`` both
   ``authenticated``. Anything else is refused without a network call.
   Every unseen token used to cost a five-second call to Supabase, so a
   stream of made-up tokens tied up the server doing Supabase's work.
2. **Its signature, locally,** for tokens signed with an asymmetric key
   (ES256, RS256), against the project's published keys (JWKS). A forged
   token is refused here, without a network call. So is every HS256 token
   once ``refuse_hs256`` is set and the project is known to publish keys:
   it signs with those by then, and a token claiming the shared secret is
   one somebody wrote.
3. **Supabase Auth** (``GET /auth/v1/user``) for every token still
   standing: a genuine signature says who signed in, but only Supabase
   knows whether that session has since been signed out. Tokens signed
   with the shared secret (HS256), unless check 2 refused them, and any
   token while the published keys can't be fetched, rely on this check
   alone.

**Moving a project to asymmetric keys.** Supabase lists the new key before
it signs with it, and the sessions signed with the shared secret stay valid
until they expire. HS256 tokens used to be refused as soon as a key was
listed, which would have signed everyone out of this service part-way
through. So they are refused only once ``refuse_hs256`` says the move is
over; until then a warning, logged once, says the keys have been seen.

Answers are kept briefly -- a client polls every few seconds while it waits
for a run -- in two bounded caches, so that refused tokens can't crowd out
accepted ones: a minute for an accepted token, 30 seconds for a refused one.

**Signing out is not instant.** A token stays accepted for up to a minute
after its session is revoked: the cached answer outlives the session by at
most that long.

With ``require_confirmed_email`` (the default), an account must have a
confirmed email address, which also turns away anonymous sign-ins: an
account is free to make, so otherwise a few throwaway ones could fill the
run queue. That is on the Supabase user record, read in check 3.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import jwt

from app.cache import MISSING, ExpiringLru

logger = logging.getLogger(__name__)

#: Algorithms Supabase signs access tokens with. "none", and anything else,
#: is refused before it gets further.
ALLOWED_ALGORITHMS = frozenset({"HS256", "ES256", "RS256"})
#: Those whose signature can be checked with the project's public keys.
ASYMMETRIC_ALGORITHMS = frozenset({"ES256", "RS256"})

#: The audience and role of a signed-in user's token. The project's anon
#: and service-role keys are JWTs too, but with other roles.
AUDIENCE = "authenticated"
ROLE = "authenticated"

#: Allowance for the server's clock and Supabase's disagreeing.
CLOCK_SKEW_SECONDS = 30

#: Upper bound on tokens remembered at once. Past it the least recently
#: used are forgotten first.
MAX_CACHED = 10_000

#: At most this many calls to Supabase Auth at once. More wait briefly,
#: then get a 503, rather than pile up threads each waiting five seconds.
MAX_CONCURRENT_CHECKS = 8

#: The published keys are refetched this often, and a token naming a key
#: they don't have (or any key at all, while none are held) may prompt a
#: refetch no more often than the second.
JWKS_REFRESH_SECONDS = 600.0
JWKS_RETRY_SECONDS = 60.0


@dataclass(frozen=True)
class Caller:
    """A signed-in user."""

    user_id: str


class AuthUnavailableError(RuntimeError):
    """Raised when the sign-in check itself could not be made."""


class AccountRefusedError(RuntimeError):
    """Raised for a genuine sign-in whose account may not run simulations.

    The message says why, and is safe to show the caller.
    """


class Authenticator(Protocol):
    def authenticate(self, token: str) -> Caller | None:
        """The token's owner, or ``None`` if the token is not valid.

        Raises :class:`AuthUnavailableError` when validity can't be decided,
        and :class:`AccountRefusedError` for an account that is not allowed.
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


def _segment(part: str) -> Any:
    """One base64url-encoded JSON part of a JWT."""
    padded = part + "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))


def read_unverified(
    token: str, issuer: str | None, now: float
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The token's header and claims, if it could be a live Supabase session.

    Checks everything that can be checked without a key. Says nothing about
    whether the token is genuine: anyone can write these fields.
    """
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        return None
    try:
        header, claims = _segment(parts[0]), _segment(parts[1])
    except (ValueError, UnicodeError, binascii.Error):
        return None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        return None
    if header.get("alg") not in ALLOWED_ALGORITHMS:
        return None

    expires = claims.get("exp")
    if isinstance(expires, bool) or not isinstance(expires, int | float):
        return None
    if expires + CLOCK_SKEW_SECONDS <= now:
        return None
    if issuer is not None and claims.get("iss") != issuer:
        return None
    audience = claims.get("aud")
    if audience != AUDIENCE and not (isinstance(audience, list) and AUDIENCE in audience):
        return None
    if claims.get("role") != ROLE:
        return None
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        return None
    return header, claims


@dataclass(frozen=True)
class _Refusal:
    """A cached "genuine, but not allowed", with the reason."""

    reason: str


#: What checking a signature can conclude.
_VALID, _INVALID, _UNKNOWN = "valid", "invalid", "unknown"


class _PublishedKeys:
    """The project's public signing keys (JWKS), fetched rarely."""

    def __init__(
        self,
        url: str,
        api_key: str,
        fetch: Fetch,
        clock: Callable[[], float],
        on_published: Callable[[], None] | None = None,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._fetch = fetch
        self._clock = clock
        # Called once, the first time a fetch lists a key.
        self._on_published = on_published
        self._keys: dict[str, jwt.PyJWK] | None = None
        self._fetched_at = float("-inf")
        self._attempted_at = float("-inf")
        self._lock = threading.Lock()

    def find(self, kid: str) -> tuple[bool, jwt.PyJWK | None]:
        """(whether the keys are known, the key with this id if they have it)."""
        keys = self._current(kid)
        if keys is None:
            return False, None
        return True, keys.get(kid)

    def published(self) -> bool:
        """Whether the project is known to publish asymmetric keys."""
        return self._current() is not None

    def _current(self, kid: str | None = None) -> dict[str, jwt.PyJWK] | None:
        """The keys held, refetched first if they are due.

        The fetch is a network call of up to five seconds, so it is made
        without the lock: whoever finds the keys due stamps the attempt and
        fetches, and everyone arriving meanwhile uses the keys already held
        instead of queueing behind it.
        """
        with self._lock:
            now = self._clock()
            stale = now - self._fetched_at > JWKS_REFRESH_SECONDS
            # With no keys held, any key a token names is one to look for.
            # Just after a project starts signing with its first key, its
            # tokens otherwise stayed unknown, each one a call to Supabase,
            # until the ten minutes were up. The retry gate bounds this
            # too, so made-up key ids can't each cost a fetch.
            missing = kid is not None and (self._keys is None or kid not in self._keys)
            due = (stale or missing) and now - self._attempted_at > JWKS_RETRY_SECONDS
            if not due:
                return self._keys
            self._attempted_at = now

        fetched, keys = self._fetch_keys()
        with self._lock:
            if fetched:
                self._keys = keys
                self._fetched_at = now
            held = self._keys
            announce = self._on_published if held is not None else None
            if announce is not None:
                self._on_published = None
        if announce is not None:
            announce()
        return held

    def _fetch_keys(self) -> tuple[bool, dict[str, jwt.PyJWK] | None]:
        """(whether the fetch worked, the keys it listed). A failure leaves
        the keys already held in place."""
        try:
            status, body = self._fetch(self._url, {"apikey": self._api_key})
        except AuthUnavailableError:
            logger.warning("Could not fetch the project's signing keys")
            return False, None
        if status != 200:
            logger.warning("Fetching the project's signing keys answered HTTP %d", status)
            return False, None
        try:
            listed = json.loads(body).get("keys", [])
        except (ValueError, AttributeError):
            logger.warning("The project's signing keys were unreadable")
            return False, None
        keys: dict[str, jwt.PyJWK] = {}
        for entry in listed if isinstance(listed, list) else []:
            try:
                key = jwt.PyJWK(entry)
            except (jwt.PyJWTError, TypeError, ValueError):
                continue
            if key.key_id and key.algorithm_name in ASYMMETRIC_ALGORITHMS:
                keys[key.key_id] = key
        # A project still on the shared secret publishes none; its tokens
        # go to Supabase.
        return True, keys or None


def _warn_hs256_still_accepted() -> None:
    logger.warning(
        "The project publishes asymmetric signing keys, and tokens signed with the "
        "shared secret (HS256) are still accepted. Set REFUSE_HS256_TOKENS=true once "
        "the sessions signed with it have expired."
    )


class SupabaseAuthenticator:
    """Checks access tokens for one Supabase project, caching answers briefly."""

    def __init__(
        self,
        supabase_url: str,
        api_key: str,
        cache_seconds: float = 60.0,
        fetch: Fetch = _urllib_fetch,
        clock: Callable[[], float] = time.monotonic,
        *,
        require_confirmed_email: bool = True,
        refuse_hs256: bool = False,
        rejection_seconds: float = 30.0,
        max_cached: int = MAX_CACHED,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        base = supabase_url.rstrip("/")
        self._user_url = f"{base}/auth/v1/user"
        self._issuer = f"{base}/auth/v1"
        self._api_key = api_key
        self._cache_seconds = cache_seconds
        self._rejection_seconds = rejection_seconds
        self._require_confirmed_email = require_confirmed_email
        self._refuse_hs256 = refuse_hs256
        self._fetch = fetch
        self._clock = clock
        self._wall_clock = wall_clock
        self._keys = _PublishedKeys(
            f"{base}/auth/v1/.well-known/jwks.json",
            api_key,
            fetch,
            clock,
            # Said once, when the keys first appear, to whoever has yet to
            # turn the setting on.
            on_published=None if refuse_hs256 else _warn_hs256_still_accepted,
        )
        # Keyed by a hash so raw tokens are not kept in memory longer than
        # the request that carried them. Bounded, and expired on read: it
        # used to be rebuilt in full on every miss, which made a stream of
        # unseen tokens quadratic.
        self._tokens = ExpiringLru(max_cached)
        # Refused tokens are remembered apart from accepted ones. Made-up
        # tokens cost nothing to produce, and in one cache a flood of them
        # pushed out the people actually signed in.
        self._refused = ExpiringLru(max_cached)
        self._checks = threading.BoundedSemaphore(MAX_CONCURRENT_CHECKS)

    def authenticate(self, token: str) -> Caller | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = self._clock()
        for cache in (self._tokens, self._refused):
            cached = cache.get(key, now)
            if cached is not MISSING:
                return self._answer(cached)

        parsed = read_unverified(token, self._issuer, self._wall_clock())
        if parsed is None:
            return self._answer(self._remember(key, None, now))
        header, claims = parsed
        lifetime = float(claims["exp"]) - self._wall_clock()

        # The keys are looked at before the setting, so that a project still
        # signing with the shared secret is told when its keys appear.
        if (
            header["alg"] not in ASYMMETRIC_ALGORITHMS
            and self._keys.published()
            and self._refuse_hs256
        ):
            # The project has finished moving to an asymmetric key, so no
            # HS256 token it issued is still live. Anyone can write one, and
            # each would be sent to Supabase to be turned down.
            #
            # Never while no keys are held, whatever the setting: a project
            # that publishes none signs every token with the shared secret,
            # and a failed fetch would otherwise sign everyone out.
            return self._answer(self._remember(key, None, now))

        if header["alg"] in ASYMMETRIC_ALGORITHMS:
            verdict = self._check_signature(token, header)
            if verdict == _INVALID:
                return self._answer(self._remember(key, None, now))
            if verdict == _VALID:
                # Genuine, but only Supabase knows whether its session is
                # still live (and whether the email is confirmed).
                outcome = self._ask_supabase(token)
                if isinstance(outcome, Caller) and outcome.user_id != str(claims["sub"]):
                    raise AuthUnavailableError(
                        "Supabase Auth named a different user than the token."
                    )
                return self._answer(self._remember(key, outcome, now, lifetime))

        # HS256 that was not refused above, or the published keys could not
        # be fetched: only Supabase can say.
        return self._answer(self._remember(key, self._ask_supabase(token), now, lifetime))

    @staticmethod
    def _answer(outcome: Any) -> Caller | None:
        if isinstance(outcome, _Refusal):
            raise AccountRefusedError(outcome.reason)
        return outcome

    def _remember(self, key: str, outcome: Any, now: float, lifetime: float | None = None) -> Any:
        """Cache an outcome for as long as it may be trusted, and return it."""
        accepted = isinstance(outcome, Caller)
        seconds = self._cache_seconds if accepted else self._rejection_seconds
        if lifetime is not None:
            # Never past the token's own expiry.
            seconds = max(0.0, min(seconds, lifetime))
        (self._tokens if accepted else self._refused).put(key, outcome, now + seconds)
        return outcome

    def _check_signature(self, token: str, header: dict[str, Any]) -> str:
        kid = header.get("kid")
        if not isinstance(kid, str):
            return _INVALID
        known, key = self._keys.find(kid)
        if not known:
            return _UNKNOWN
        if key is None or key.algorithm_name != header["alg"]:
            return _INVALID
        try:
            jwt.decode(
                token,
                key=key.key,
                algorithms=[header["alg"]],
                audience=AUDIENCE,
                issuer=self._issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "sub"]},
            )
        except jwt.PyJWTError:
            return _INVALID
        return _VALID

    def _ask_supabase(self, token: str) -> Any:
        """Supabase Auth's answer: the caller, a refusal, or ``None``."""
        if not self._checks.acquire(timeout=2):
            raise AuthUnavailableError("Too many sign-in checks are already waiting on Supabase.")
        try:
            status, body = self._fetch(
                self._user_url,
                {"apikey": self._api_key, "Authorization": f"Bearer {token}"},
            )
        finally:
            self._checks.release()

        if status in (401, 403):
            return None
        if status != 200:
            raise AuthUnavailableError(f"Supabase Auth answered HTTP {status}.")
        try:
            user = json.loads(body)
            caller = Caller(user_id=str(user["id"]))
        except (ValueError, KeyError, TypeError) as error:
            raise AuthUnavailableError("Supabase Auth sent an unreadable user.") from error
        if self._require_confirmed_email and (
            user.get("is_anonymous") or not user.get("email_confirmed_at")
        ):
            logger.info("Refused user %s: no confirmed email address", caller.user_id)
            return _Refusal("Confirm your email address before running simulations.")
        return caller


class OpenAuthenticator:
    """Lets everyone in as one shared caller. Local development only.

    Used when ``REQUIRE_AUTH=false``, so the API can be run without a
    Supabase project. Never set that on a deployed server.
    """

    CALLER = Caller(user_id="local-dev")

    def authenticate(self, token: str) -> Caller | None:
        return self.CALLER
