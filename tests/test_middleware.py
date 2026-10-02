"""Unit tests for the guards in app.middleware."""

from __future__ import annotations

import pytest

from app.middleware import TokenBucket, client_ip


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def scope(peer="192.0.2.10", forwarded=None):
    headers = [(b"x-forwarded-for", value.encode()) for value in forwarded or []]
    return {"type": "http", "client": (peer, 1234), "headers": headers}


class TestTokenBucket:
    def test_a_burst_up_to_the_minutes_allowance_is_fine(self):
        bucket = TokenBucket(3, Clock())
        assert [bucket.take("a") for _ in range(3)] == [0, 0, 0]
        assert bucket.take("a") > 0

    def test_the_allowance_refills_evenly(self):
        clock = Clock()
        bucket = TokenBucket(60, clock)
        for _ in range(60):
            bucket.take("a")
        assert bucket.take("a") == pytest.approx(1.0)
        clock.now += 1.0
        assert bucket.take("a") == 0

    def test_keys_are_independent(self):
        bucket = TokenBucket(1, Clock())
        assert bucket.take("a") == 0
        assert bucket.take("b") == 0
        assert bucket.take("a") > 0

    def test_memory_is_bounded(self):
        bucket = TokenBucket(1, Clock(), max_keys=2)
        for key in "abc":
            bucket.take(key)
        assert len(bucket._buckets) == 2


class TestClientIp:
    def test_without_trusted_proxies_the_connection_decides(self):
        assert client_ip(scope(forwarded=["203.0.113.5"]), 0) == "192.0.2.10"

    def test_the_entry_the_trusted_proxy_appended_is_used(self):
        assert client_ip(scope(forwarded=["203.0.113.5, 198.51.100.7"]), 1) == "198.51.100.7"

    def test_two_proxies_means_two_from_the_right(self):
        forwarded = ["spoofed, 203.0.113.5, 10.0.0.2"]
        assert client_ip(scope(forwarded=forwarded), 2) == "203.0.113.5"

    def test_repeated_headers_are_read_as_one_list(self):
        assert client_ip(scope(forwarded=["203.0.113.5", "198.51.100.7"]), 1) == "198.51.100.7"

    def test_too_few_entries_falls_back_to_the_connection(self):
        assert client_ip(scope(forwarded=[]), 1) == "192.0.2.10"
