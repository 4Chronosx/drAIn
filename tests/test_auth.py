"""Tests for checking callers with Supabase Auth."""

from __future__ import annotations

import json

import pytest

from app.auth import AuthUnavailableError, Caller, SupabaseAuthenticator


class FakeSupabase:
    """Answers GET /auth/v1/user the way Supabase does, and counts calls."""

    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else json.dumps({"id": "user-1"}).encode()
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        return self.status, self.body


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def authenticator(fake, clock=None):
    return SupabaseAuthenticator(
        "https://project.supabase.co/",
        "anon-key",
        cache_seconds=60,
        fetch=fake,
        clock=clock or Clock(),
    )


def test_a_valid_token_names_its_user():
    fake = FakeSupabase()
    assert authenticator(fake).authenticate("token") == Caller("user-1")

    url, headers = fake.calls[0]
    assert url == "https://project.supabase.co/auth/v1/user"
    assert headers == {"apikey": "anon-key", "Authorization": "Bearer token"}


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_is_nobody(status):
    assert authenticator(FakeSupabase(status=status, body=b"{}")).authenticate("t") is None


@pytest.mark.parametrize("status", [500, 502, 429])
def test_an_auth_outage_is_not_mistaken_for_a_bad_token(status):
    with pytest.raises(AuthUnavailableError):
        authenticator(FakeSupabase(status=status, body=b"")).authenticate("t")


def test_an_unreadable_answer_is_an_outage():
    with pytest.raises(AuthUnavailableError):
        authenticator(FakeSupabase(body=b"not json")).authenticate("t")


def test_answers_are_cached_while_a_client_polls():
    fake = FakeSupabase()
    clock = Clock()
    auth = authenticator(fake, clock)

    auth.authenticate("token")
    clock.now += 30
    auth.authenticate("token")
    assert len(fake.calls) == 1

    clock.now += 31
    auth.authenticate("token")
    assert len(fake.calls) == 2


def test_a_rejection_is_cached_too():
    fake = FakeSupabase(status=401, body=b"{}")
    auth = authenticator(fake)
    auth.authenticate("bad")
    auth.authenticate("bad")
    assert len(fake.calls) == 1


def test_different_tokens_are_checked_separately():
    fake = FakeSupabase()
    auth = authenticator(fake)
    auth.authenticate("one")
    auth.authenticate("two")
    assert len(fake.calls) == 2
