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

    def test_a_connection_without_a_peer_is_unknown(self):
        assert client_ip({"type": "http", "client": None, "headers": []}, 0) == "unknown"


class TestAddressesAsLimitKeys:
    """An IPv6 customer holds a whole /64 and can send from any address in
    it. Limited per address, one caller had as many allowances as they
    cared to use."""

    def test_two_addresses_in_one_ipv6_network_share_a_key(self):
        first = client_ip(scope(forwarded=["2001:db8:1:2::1"]), 1)
        second = client_ip(scope(forwarded=["2001:db8:1:2:ffff:abcd:0:9"]), 1)
        assert first == second == "2001:db8:1:2::/64"

    def test_different_ipv6_networks_do_not(self):
        first = client_ip(scope(forwarded=["2001:db8:1:2::1"]), 1)
        second = client_ip(scope(forwarded=["2001:db8:1:3::1"]), 1)
        assert first != second

    def test_an_ipv4_address_written_as_ipv6_is_that_ipv4_address(self):
        assert client_ip(scope(forwarded=["::ffff:203.0.113.5"]), 1) == "203.0.113.5"

    def test_an_ipv6_peer_is_keyed_the_same_way(self):
        assert client_ip(scope(peer="2001:db8:1:2::1"), 0) == "2001:db8:1:2::/64"
        assert client_ip(scope(peer="::ffff:192.0.2.10"), 0) == "192.0.2.10"

    @pytest.mark.parametrize(
        "entry", ["not-an-address", "203.0.113.5:4321", "unknown", "999.1.1.1"]
    )
    def test_a_forwarded_entry_that_is_not_an_address_falls_back_to_the_peer(self, entry):
        assert client_ip(scope(forwarded=[f"203.0.113.5, {entry}"]), 1) == "192.0.2.10"

    def test_a_peer_that_is_not_an_address_is_kept_as_it_is(self):
        assert client_ip(scope(peer="testclient"), 0) == "testclient"

    def test_the_limiter_counts_a_network_as_one_caller(self):
        bucket = TokenBucket(1, Clock())
        assert bucket.take(client_ip(scope(forwarded=["2001:db8:1:2::1"]), 1)) == 0
        assert bucket.take(client_ip(scope(forwarded=["2001:db8:1:2::2"]), 1)) > 0
        assert bucket.take(client_ip(scope(forwarded=["2001:db8:1:3::1"]), 1)) == 0
