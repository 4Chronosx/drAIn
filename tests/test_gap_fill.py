"""Gap-fill tests for error paths the existing suites do not reach.

Covers the ExpiringLru cache directly, the If-None-Match matching rules on
rendered poll responses, the unknown-job memory's expiry, and the body-size
middleware's malformed and boundary Content-Length handling.
"""

from __future__ import annotations

import gzip
import json

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.cache import MISSING, ExpiringLru
from app.jobs import JobStatus, SimulationJob
from app.middleware import BodySizeLimitMiddleware, client_ip
from app.polling import UNKNOWN_JOB_SECONDS, RenderedState, RenderedStates, render


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TestExpiringLru:
    def test_a_missing_key_is_missing_but_a_cached_none_is_none(self):
        cache = ExpiringLru(4)
        cache.put("a", None, expires=10.0)
        assert cache.get("a", now=0.0) is None
        assert cache.get("b", now=0.0) is MISSING

    def test_an_entry_expires_exactly_at_its_deadline(self):
        cache = ExpiringLru(4)
        cache.put("a", "value", expires=10.0)
        assert cache.get("a", now=9.999) == "value"
        # expires <= now counts as dead, so the deadline itself is too late.
        assert cache.get("a", now=10.0) is MISSING

    def test_an_expired_entry_is_dropped_not_just_hidden(self):
        cache = ExpiringLru(4)
        cache.put("a", "value", expires=1.0)
        cache.get("a", now=2.0)
        assert len(cache) == 0

    def test_the_least_recently_used_entry_is_evicted_when_full(self):
        cache = ExpiringLru(2)
        cache.put("a", 1, expires=100.0)
        cache.put("b", 2, expires=100.0)
        cache.put("c", 3, expires=100.0)
        assert cache.get("a", now=0.0) is MISSING
        assert cache.get("b", now=0.0) == 2
        assert cache.get("c", now=0.0) == 3

    def test_reading_an_entry_protects_it_from_eviction(self):
        cache = ExpiringLru(2)
        cache.put("a", 1, expires=100.0)
        cache.put("b", 2, expires=100.0)
        cache.get("a", now=0.0)  # "a" is now the most recently used
        cache.put("c", 3, expires=100.0)
        assert cache.get("a", now=0.0) == 1
        assert cache.get("b", now=0.0) is MISSING

    def test_overwriting_a_key_does_not_grow_the_cache(self):
        cache = ExpiringLru(2)
        cache.put("a", 1, expires=100.0)
        cache.put("a", 2, expires=100.0)
        assert len(cache) == 1
        assert cache.get("a", now=0.0) == 2


def finished_job(result=None, error=None):
    status = JobStatus.FAILED if error else JobStatus.SUCCEEDED
    return SimulationJob(id="job-1", owner="user-1", status=status, result=result, error=error)


class TestRenderedState:
    def test_the_gzip_variant_gets_its_own_etag(self):
        rendered = render(finished_job(result={"nodes": 1}))
        plain = rendered.etag_for(False)
        zipped = rendered.etag_for(True)
        assert plain.startswith('"') and plain.endswith('"')
        assert zipped == f'{plain[:-1]}-gzip"'

    def test_the_body_round_trips_through_gzip(self):
        rendered = render(finished_job(result={"nodes": 1}))
        body = json.loads(rendered.body())
        assert body["status"] == "succeeded"
        assert body["result"] == {"nodes": 1}
        assert body["error"] is None
        assert gzip.decompress(rendered.gzipped) == rendered.body()

    def test_a_failed_job_renders_its_error_and_no_result(self):
        rendered = render(finished_job(error="The model blew up."))
        body = json.loads(rendered.body())
        assert body["status"] == "failed"
        assert body["result"] is None
        assert body["error"] == "The model blew up."

    def test_matches_accepts_either_encodings_tag(self):
        rendered = render(finished_job(result={"nodes": 1}))
        assert rendered.matches(rendered.etag_for(False))
        assert rendered.matches(rendered.etag_for(True))

    def test_matches_handles_lists_weak_tags_and_the_wildcard(self):
        rendered = render(finished_job(result={"nodes": 1}))
        etag = rendered.etag_for(False)
        # Proxies may add weak prefixes or join several tags with commas.
        assert rendered.matches(f"W/{etag}")
        assert rendered.matches(f'"something-else", {etag}')
        assert rendered.matches("*")

    def test_matches_refuses_missing_or_foreign_tags(self):
        rendered = render(finished_job(result={"nodes": 1}))
        assert not rendered.matches(None)
        assert not rendered.matches("")
        assert not rendered.matches('"not-this-body"')

    def test_different_results_get_different_etags(self):
        one = render(finished_job(result={"nodes": 1}))
        two = render(finished_job(result={"nodes": 2}))
        assert one.etag != two.etag


class TestRenderedStates:
    def rendered(self):
        return RenderedState(owner="user-1", gzipped=gzip.compress(b"{}"), etag='"abc"')

    def test_a_rendered_response_expires_after_its_ttl(self):
        clock = Clock()
        states = RenderedStates(ttl_seconds=60.0, clock=clock)
        states.put("job-1", self.rendered())
        assert states.get("job-1") is not None
        clock.now = 60.0
        assert states.get("job-1") is None

    def test_the_rendered_store_is_bounded(self):
        states = RenderedStates(ttl_seconds=60.0, max_entries=2, clock=Clock())
        for n in range(3):
            states.put(f"job-{n}", self.rendered())
        assert states.get("job-0") is None
        assert states.get("job-2") is not None

    def test_an_unknown_job_is_forgotten_after_its_window(self):
        # Repolls inside the window skip Supabase; afterwards it is asked
        # again, in case the run has appeared in the meantime.
        clock = Clock()
        states = RenderedStates(ttl_seconds=60.0, clock=clock)
        states.mark_unknown("missing")
        assert states.is_unknown("missing")
        clock.now = UNKNOWN_JOB_SECONDS
        assert not states.is_unknown("missing")

    def test_jobs_are_not_unknown_by_default(self):
        states = RenderedStates(ttl_seconds=60.0, clock=Clock())
        assert not states.is_unknown("anything")


def echo_size_app(max_bytes):
    """A tiny app behind the limiter that reads its body like FastAPI would."""

    async def endpoint(request: Request) -> PlainTextResponse:
        body = await request.body()
        return PlainTextResponse(str(len(body)))

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])])
    return TestClient(BodySizeLimitMiddleware(app, max_bytes=max_bytes))


class TestBodySizeLimitEdges:
    def test_a_malformed_content_length_is_a_400_not_a_crash(self):
        client = echo_size_app(100)
        response = client.post("/", content=b"hi", headers={"Content-Length": "banana"})
        assert response.status_code == 400
        assert "Content-Length" in response.json()["detail"]

    def test_a_declared_body_exactly_at_the_limit_is_allowed(self):
        client = echo_size_app(10)
        assert client.post("/", content=b"x" * 10).status_code == 200

    def test_a_declared_body_one_byte_over_is_refused(self):
        client = echo_size_app(10)
        response = client.post("/", content=b"x" * 11)
        assert response.status_code == 413
        assert "11" not in response.json()["detail"]  # the limit is named, not the size

    def test_a_chunked_body_exactly_at_the_limit_is_allowed(self):
        client = echo_size_app(10)

        def chunks():
            yield b"x" * 5
            yield b"x" * 5

        assert client.post("/", content=chunks()).status_code == 200

    def test_a_chunked_body_is_cut_off_just_past_the_limit(self):
        client = echo_size_app(10)

        def chunks():
            yield b"x" * 5
            yield b"x" * 6

        assert client.post("/", content=chunks()).status_code == 413


class TestClientIpFallback:
    def test_a_scope_without_a_client_reads_as_unknown(self):
        # Some test harnesses and unix-socket setups give no client tuple.
        scope = {"type": "http", "headers": []}
        assert client_ip(scope, proxy_hops=0) == "unknown"
        assert client_ip(scope, proxy_hops=1) == "unknown"
