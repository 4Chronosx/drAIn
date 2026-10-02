"""ASGI middleware that guards the API before a route runs.

Each piece here acts before FastAPI reads a body or checks a sign-in, which
is the point: those are the costly steps a caller should not be able to
trigger at will.
"""

from __future__ import annotations

import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: Client addresses a rate limiter remembers. Past it the least recently
#: seen are forgotten, which only ever hands them a fresh allowance.
MAX_TRACKED_CLIENTS = 10_000


def client_ip(scope: Scope, proxy_hops: int) -> str:
    """The caller's address, as far as it can be trusted.

    Each proxy appends the address it heard from to X-Forwarded-For, so the
    entry ``proxy_hops`` from the right is the one the outermost trusted
    proxy saw. Anything left of it was written by the caller and could say
    anything. (Uvicorn's own ``--forwarded-allow-ips='*'`` takes the leftmost
    entry, which the caller chooses.)
    """
    if proxy_hops > 0:
        forwarded = ",".join(Headers(scope=scope).getlist("x-forwarded-for"))
        hops = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
        if len(hops) >= proxy_hops:
            return hops[-proxy_hops]
    client = scope.get("client")
    return client[0] if client else "unknown"


class BodySizeLimitMiddleware:
    """Refuses request bodies over ``max_bytes`` with 413.

    FastAPI reads and parses the whole body before the route's sign-in check
    runs, so without this anyone could make the server hold and parse as
    much JSON as they cared to send. A declared Content-Length over the
    limit is refused before reading anything; a body sent without one
    (chunked) is counted as it arrives and cut off at the limit.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                too_large = int(declared) > self.max_bytes
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)(
                    scope, receive, send
                )
                return
            if too_large:
                await self._refuse(scope, receive, send)
                return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # An HTTPException, so FastAPI's body reader passes it
                    # on as a 413 instead of turning it into a 400.
                    raise HTTPException(status_code=413, detail=self._detail())
            return message

        await self.app(scope, counting_receive, send)

    def _detail(self) -> str:
        return f"The request body is larger than {self.max_bytes} bytes."

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse({"detail": self._detail()}, status_code=413)(scope, receive, send)


class TokenBucket:
    """Per-key allowances: ``per_minute`` requests, refilled evenly.

    A key starts with a full minute's worth, so a short burst is fine and a
    steady flood is not. Used only from the event loop, so it needs no lock.
    """

    def __init__(
        self,
        per_minute: int,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        self._capacity = float(per_minute)
        self._rate = per_minute / 60.0
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def take(self, key: str) -> float:
        """Spend one request. Returns 0 if allowed, else seconds to wait."""
        now = self._clock()
        tokens, updated = self._buckets.get(key, (self._capacity, now))
        tokens = min(self._capacity, tokens + (now - updated) * self._rate)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        self._buckets[key] = (tokens, now)
        self._buckets.move_to_end(key)
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)
        return 0.0 if allowed else (1.0 - tokens) / self._rate


class RateLimitMiddleware:
    """Per-address limits on starting runs and on polling them.

    Checked before the body is read or the sign-in is asked about, so a
    flood costs a dictionary lookup each. The per-account limits in the job
    store still apply behind it; this catches what they cannot see: one
    address cycling through accounts, or tokens that are not accounts at all.
    """

    def __init__(
        self,
        app: ASGIApp,
        submit_per_minute: int,
        poll_per_minute: int,
        proxy_hops: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.proxy_hops = proxy_hops
        self._limits = {
            "POST": TokenBucket(submit_per_minute, clock) if submit_per_minute > 0 else None,
            "GET": TokenBucket(poll_per_minute, clock) if poll_per_minute > 0 else None,
        }
        self._warned_about_proxy = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/simulations"):
            await self.app(scope, receive, send)
            return
        bucket = self._limits.get(scope["method"])
        if bucket is None:
            await self.app(scope, receive, send)
            return

        if (
            self.proxy_hops == 0
            and not self._warned_about_proxy
            and "x-forwarded-for" in Headers(scope=scope)
        ):
            self._warned_about_proxy = True
            logger.warning(
                "Requests arrive through a proxy, but TRUSTED_PROXY_HOPS is 0: every "
                "caller is limited as the proxy's address. Set it to the number of proxies."
            )

        wait = bucket.take(client_ip(scope, self.proxy_hops))
        if wait > 0:
            retry = max(1, math.ceil(wait))
            response = JSONResponse(
                {"detail": "Too many requests from this address. Slow down."},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


#: Added to every response. The API only serves JSON, so these cost nothing.
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
}


class SecurityHeadersMiddleware:
    """Adds :data:`SECURITY_HEADERS`, and ``Cache-Control: no-store`` to any
    response that did not choose its own caching. Responses carry signed-in
    users' results, which no shared cache should keep."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                headers.setdefault("cache-control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)
