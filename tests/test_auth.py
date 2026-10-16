"""Tests for checking callers' Supabase access tokens."""

from __future__ import annotations

import base64
import json
import logging
import threading
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.auth import (
    AccountRefusedError,
    AuthUnavailableError,
    Caller,
    SupabaseAuthenticator,
    read_unverified,
)

PROJECT = "https://project.supabase.co"
ISSUER = f"{PROJECT}/auth/v1"
USER_URL = f"{PROJECT}/auth/v1/user"
JWKS_URL = f"{PROJECT}/auth/v1/.well-known/jwks.json"

CONFIRMED_USER = {"id": "user-1", "email_confirmed_at": "2026-09-01T00:00:00Z"}


def claims(**overrides):
    """A signed-in user's claims, as Supabase writes them."""
    base = {
        "iss": ISSUER,
        "sub": "user-1",
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 3600,
        "is_anonymous": False,
    }
    return {**base, **overrides}


def _b64(value) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()


def unsigned(header=None, **claim_overrides) -> str:
    """A token shaped like Supabase's, with a signature nobody checks here:
    HS256 tokens are checked by Supabase, which the fake stands in for."""
    header = header or {"alg": "HS256", "typ": "JWT"}
    return f"{_b64(header)}.{_b64(claims(**claim_overrides))}.c2lnbmF0dXJl"


class SigningKey:
    """A key pair (ES256, or RS256 if asked), published the way Supabase
    publishes its keys."""

    def __init__(self, kid="key-1", algorithm="ES256"):
        self.kid = kid
        self.algorithm = algorithm
        if algorithm == "RS256":
            self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        else:
            self.private = ec.generate_private_key(ec.SECP256R1())

    def jwk(self):
        to_jwk = (
            jwt.algorithms.RSAAlgorithm if self.algorithm == "RS256" else jwt.algorithms.ECAlgorithm
        ).to_jwk
        public = json.loads(to_jwk(self.private.public_key()))
        return {**public, "kid": self.kid, "alg": self.algorithm, "use": "sig"}

    def sign(self, **claim_overrides) -> str:
        return jwt.encode(
            claims(**claim_overrides),
            self.private,
            algorithm=self.algorithm,
            headers={"kid": self.kid},
        )


class FakeSupabase:
    """Answers GET /auth/v1/user and the JWKS the way Supabase does, and
    counts calls to each."""

    def __init__(self, status=200, body=None, keys=None, jwks_status=200):
        self.status = status
        self.body = body if body is not None else json.dumps(CONFIRMED_USER).encode()
        self.keys = keys or []
        self.jwks_status = jwks_status
        self.calls = []
        self.jwks_calls = 0

    def __call__(self, url, headers):
        if url == JWKS_URL:
            self.jwks_calls += 1
            body = json.dumps({"keys": [key.jwk() for key in self.keys]}).encode()
            return self.jwks_status, body
        self.calls.append((url, headers))
        return self.status, self.body


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def authenticator(fake, clock=None, **options):
    return SupabaseAuthenticator(
        PROJECT + "/",
        "anon-key",
        cache_seconds=60,
        fetch=fake,
        clock=clock or Clock(),
        **options,
    )


class TestAskingSupabase:
    """HS256 tokens can only be checked by Supabase."""

    def test_a_valid_token_names_its_user(self):
        fake = FakeSupabase()
        token = unsigned()
        assert authenticator(fake).authenticate(token) == Caller("user-1")

        url, headers = fake.calls[0]
        assert url == USER_URL
        assert headers == {"apikey": "anon-key", "Authorization": f"Bearer {token}"}

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_rejected_token_is_nobody(self, status):
        fake = FakeSupabase(status=status, body=b"{}")
        assert authenticator(fake).authenticate(unsigned()) is None

    @pytest.mark.parametrize("status", [500, 502, 429])
    def test_an_auth_outage_is_not_mistaken_for_a_bad_token(self, status):
        with pytest.raises(AuthUnavailableError):
            authenticator(FakeSupabase(status=status, body=b"")).authenticate(unsigned())

    def test_an_unreadable_answer_is_an_outage(self):
        with pytest.raises(AuthUnavailableError):
            authenticator(FakeSupabase(body=b"not json")).authenticate(unsigned())


class TestTheShapeIsCheckedFirst:
    """Regression: every unseen token cost a five-second call to Supabase,
    so a stream of made-up tokens kept the server busy asking."""

    @pytest.mark.parametrize(
        "token",
        [
            "token",
            "a.b",
            "a.b.c.d",
            "not-base64!.not-base64!.x",
            unsigned(header={"alg": "none"}),
            unsigned(header={"alg": "HS512"}),
            unsigned(exp=int(time.time()) - 3600),
            unsigned(exp="tomorrow"),
            unsigned(iss="https://elsewhere.supabase.co/auth/v1"),
            unsigned(aud="someone-else"),
            unsigned(role="anon"),
            unsigned(role="service_role"),
            unsigned(sub=""),
        ],
    )
    def test_a_token_that_cannot_be_a_session_never_reaches_supabase(self, token):
        fake = FakeSupabase()
        assert authenticator(fake).authenticate(token) is None
        assert fake.calls == [] and fake.jwks_calls == 0

    def test_a_list_audience_including_ours_is_accepted(self):
        assert read_unverified(unsigned(aud=["authenticated"]), ISSUER, time.time()) is not None


class TestCaching:
    def test_answers_are_cached_while_a_client_polls(self):
        fake = FakeSupabase()
        clock = Clock()
        auth = authenticator(fake, clock)
        token = unsigned()

        auth.authenticate(token)
        clock.now += 30
        auth.authenticate(token)
        assert len(fake.calls) == 1

        clock.now += 31
        auth.authenticate(token)
        assert len(fake.calls) == 2

    def test_a_rejection_is_cached_briefly(self):
        fake = FakeSupabase(status=401, body=b"{}")
        clock = Clock()
        auth = authenticator(fake, clock)
        token = unsigned()
        auth.authenticate(token)
        auth.authenticate(token)
        assert len(fake.calls) == 1

        clock.now += 31
        auth.authenticate(token)
        assert len(fake.calls) == 2

    def test_different_tokens_are_checked_separately(self):
        fake = FakeSupabase()
        auth = authenticator(fake)
        auth.authenticate(unsigned(sub="user-1", jti="one"))
        auth.authenticate(unsigned(sub="user-1", jti="two"))
        assert len(fake.calls) == 2

    def test_an_answer_is_not_kept_past_the_tokens_expiry(self):
        fake = FakeSupabase()
        clock = Clock()
        auth = authenticator(fake, clock)
        token = unsigned(exp=int(time.time()) + 10)
        auth.authenticate(token)
        clock.now += 11
        auth.authenticate(token)
        assert len(fake.calls) == 2

    def test_the_cache_is_bounded(self):
        fake = FakeSupabase()
        auth = authenticator(fake, max_cached=2)
        first, second, third = (unsigned(jti=str(n)) for n in range(3))
        for token in (first, second, third):
            auth.authenticate(token)
        assert len(auth._tokens) == 2

        # The least recently used went, so it is asked about again.
        auth.authenticate(first)
        assert len(fake.calls) == 4


class TestConfirmedEmail:
    """Accounts are free, so throwaway ones could otherwise fill the queue."""

    @pytest.mark.parametrize(
        "user",
        [
            {"id": "user-1", "email_confirmed_at": None},
            {"id": "user-1"},
            {"id": "user-1", "is_anonymous": True, "email_confirmed_at": None},
        ],
    )
    def test_an_account_without_a_confirmed_email_is_refused(self, user):
        auth = authenticator(FakeSupabase(body=json.dumps(user).encode()))
        with pytest.raises(AccountRefusedError, match="Confirm your email"):
            auth.authenticate(unsigned())

    def test_a_refusal_is_cached_too(self):
        user = {"id": "user-1", "email_confirmed_at": None}
        fake = FakeSupabase(body=json.dumps(user).encode())
        auth = authenticator(fake)
        token = unsigned()
        for _ in range(2):
            with pytest.raises(AccountRefusedError):
                auth.authenticate(token)
        assert len(fake.calls) == 1

    def test_the_check_can_be_turned_off(self):
        user = {"id": "user-1", "email_confirmed_at": None}
        auth = authenticator(
            FakeSupabase(body=json.dumps(user).encode()), require_confirmed_email=False
        )
        assert auth.authenticate(unsigned()) == Caller("user-1")


class TestSignatureCheckedLocally:
    """Tokens signed with an asymmetric key are checked against the
    project's published keys first, so a forgery never reaches Supabase.
    A genuine one is then confirmed with Supabase, which alone knows
    whether its session was signed out."""

    def test_a_genuine_token_is_confirmed_with_supabase_once_then_cached(self):
        key = SigningKey()
        fake = FakeSupabase(keys=[key])
        clock = Clock()
        auth = authenticator(fake, clock)
        token = key.sign()
        assert auth.authenticate(token) == Caller("user-1")
        assert auth.authenticate(token) == Caller("user-1")
        assert len(fake.calls) == 1

        clock.now += 61
        auth.authenticate(token)
        assert len(fake.calls) == 2

    def test_a_signed_out_token_stops_working_within_a_minute(self):
        key = SigningKey()
        fake = FakeSupabase(keys=[key])
        clock = Clock()
        auth = authenticator(fake, clock)
        token = key.sign()
        assert auth.authenticate(token) == Caller("user-1")

        # The session is revoked: the signature is still good, but Supabase
        # no longer accepts the token.
        fake.status, fake.body = 401, b"{}"
        clock.now += 61
        assert auth.authenticate(token) is None

    def test_supabase_naming_another_user_is_an_outage_not_a_sign_in(self):
        key = SigningKey()
        other = {**CONFIRMED_USER, "id": "someone-else"}
        fake = FakeSupabase(keys=[key], body=json.dumps(other).encode())
        with pytest.raises(AuthUnavailableError):
            authenticator(fake).authenticate(key.sign())

    def test_a_forged_token_is_refused_without_asking_supabase(self):
        published, forger = SigningKey(), SigningKey()
        fake = FakeSupabase(keys=[published])
        auth = authenticator(fake)
        assert auth.authenticate(forger.sign()) is None
        assert fake.calls == []

    def test_an_rs256_key_is_checked_the_same_way(self):
        published = SigningKey(algorithm="RS256")
        forger = SigningKey(algorithm="RS256")
        fake = FakeSupabase(keys=[published])
        auth = authenticator(fake)
        assert auth.authenticate(forger.sign()) is None
        assert fake.calls == []
        assert auth.authenticate(published.sign()) == Caller("user-1")
        assert len(fake.calls) == 1

    def test_a_token_naming_a_key_under_another_algorithm_is_refused(self):
        # The key id is the project's, but that key is an RS256 one.
        fake = FakeSupabase(keys=[SigningKey(algorithm="RS256")])
        assert authenticator(fake).authenticate(SigningKey().sign()) is None
        assert fake.calls == []

    def test_a_token_naming_an_unpublished_key_is_refused(self):
        fake = FakeSupabase(keys=[SigningKey("key-1")])
        auth = authenticator(fake)
        assert auth.authenticate(SigningKey("key-2").sign()) is None
        assert fake.calls == []

    def test_unknown_keys_do_not_each_refetch_the_published_ones(self):
        fake = FakeSupabase(keys=[SigningKey("key-1")])
        auth = authenticator(fake)
        for n in range(5):
            auth.authenticate(SigningKey(f"stranger-{n}").sign())
        assert fake.jwks_calls == 1

    def test_a_tampered_claim_breaks_the_signature(self):
        key = SigningKey()
        header, _, signature = key.sign().split(".")
        tampered = f"{header}.{_b64(claims(sub='someone-else'))}.{signature}"
        fake = FakeSupabase(keys=[key])
        assert authenticator(fake).authenticate(tampered) is None
        assert fake.calls == []

    def test_an_unconfirmed_account_is_refused_even_with_a_genuine_token(self):
        key = SigningKey()
        user = {"id": "user-1", "email_confirmed_at": None}
        fake = FakeSupabase(keys=[key], body=json.dumps(user).encode())
        with pytest.raises(AccountRefusedError):
            authenticator(fake).authenticate(key.sign())

    def test_without_the_published_keys_supabase_decides(self):
        key = SigningKey()
        fake = FakeSupabase(keys=[key], jwks_status=503)
        auth = authenticator(fake, require_confirmed_email=False)
        assert auth.authenticate(key.sign()) == Caller("user-1")
        assert len(fake.calls) == 1

    def test_without_the_published_keys_a_supabase_outage_is_still_an_outage(self):
        key = SigningKey()
        fake = FakeSupabase(keys=[key], jwks_status=503, status=500, body=b"")
        with pytest.raises(AuthUnavailableError):
            authenticator(fake).authenticate(key.sign())


class TestSharedSecretTokens:
    """A project that has moved to asymmetric keys signs with them. A token
    saying HS256 is then one somebody wrote, and each is sent to Supabase
    to be turned down unless ``refuse_hs256`` says the move is over.

    Regression: they were refused as soon as a key was published. Supabase
    lists a new key before it signs with it, and sessions signed with the
    shared secret outlive the change, so everyone signed in was turned
    away."""

    def test_an_hs256_token_is_still_asked_about_once_keys_are_published(self):
        fake = FakeSupabase(keys=[SigningKey()])
        auth = authenticator(fake)
        token = unsigned()
        assert auth.authenticate(token) == Caller("user-1")
        assert len(fake.calls) == 1
        assert fake.jwks_calls == 1

    def test_it_is_refused_without_asking_once_the_setting_is_on(self):
        fake = FakeSupabase(keys=[SigningKey()])
        auth = authenticator(fake, refuse_hs256=True)
        token = unsigned()
        assert auth.authenticate(token) is None
        assert auth.authenticate(token) is None
        assert fake.calls == []
        assert fake.jwks_calls == 1

    @pytest.mark.parametrize("refuse_hs256", [False, True])
    def test_it_is_still_asked_about_while_the_project_publishes_no_keys(self, refuse_hs256):
        # Whatever the setting: HS256 is then the only kind of token there is.
        fake = FakeSupabase(keys=[])
        auth = authenticator(fake, refuse_hs256=refuse_hs256)
        assert auth.authenticate(unsigned()) == Caller("user-1")
        assert len(fake.calls) == 1

    @pytest.mark.parametrize("refuse_hs256", [False, True])
    def test_it_is_still_asked_about_while_the_keys_cannot_be_fetched(self, refuse_hs256):
        # Whatever the setting: an outage must not sign everyone out.
        fake = FakeSupabase(keys=[SigningKey()], jwks_status=503)
        auth = authenticator(fake, refuse_hs256=refuse_hs256)
        assert auth.authenticate(unsigned()) == Caller("user-1")
        assert len(fake.calls) == 1


class TestMovingToAsymmetricKeys:
    """A project on the shared secret publishes no keys, then lists one,
    then signs with it, while the sessions it signed before live on."""

    def test_nobody_is_signed_out_along_the_way(self):
        key = SigningKey()
        fake = FakeSupabase(keys=[])
        clock = Clock()
        auth = authenticator(fake, clock)
        assert auth.authenticate(unsigned(jti="before")) == Caller("user-1")

        # The key is listed, but tokens are still signed with the secret.
        fake.keys = [key]
        clock.now += 601
        assert auth.authenticate(unsigned(jti="listed")) == Caller("user-1")

        # New tokens are signed with the key; older sessions have not expired.
        assert auth.authenticate(key.sign()) == Caller("user-1")
        assert auth.authenticate(unsigned(jti="older")) == Caller("user-1")
        assert len(fake.calls) == 4

    def test_the_first_token_signed_with_a_key_fetches_the_keys_again(self):
        """Regression: with no keys held, a token naming one prompted no
        refetch. For up to ten minutes its signature went unchecked and
        every such token, forged or not, was a call to Supabase."""
        key = SigningKey()
        fake = FakeSupabase(keys=[])
        clock = Clock()
        auth = authenticator(fake, clock)
        auth.authenticate(unsigned())
        assert fake.jwks_calls == 1

        fake.keys = [key]
        clock.now += 61
        assert auth.authenticate(key.sign()) == Caller("user-1")
        assert fake.jwks_calls == 2

        # Checked here from now on: a forgery is not sent to Supabase.
        assert auth.authenticate(SigningKey().sign()) is None
        assert len(fake.calls) == 2

    def test_unknown_keys_do_not_each_refetch_while_none_are_held(self):
        fake = FakeSupabase(keys=[], status=401, body=b"{}")
        clock = Clock()
        auth = authenticator(fake, clock)
        for n in range(5):
            auth.authenticate(SigningKey(f"stranger-{n}").sign())
        assert fake.jwks_calls == 1

        clock.now += 61
        for n in range(5, 10):
            auth.authenticate(SigningKey(f"stranger-{n}").sign())
        assert fake.jwks_calls == 2

    def test_nor_while_the_keys_cannot_be_fetched(self):
        fake = FakeSupabase(keys=[SigningKey()], jwks_status=503, status=401, body=b"{}")
        auth = authenticator(fake)
        for n in range(5):
            auth.authenticate(SigningKey(f"stranger-{n}").sign())
        assert fake.jwks_calls == 1


def hs256_warnings(caplog):
    return [record for record in caplog.records if "REFUSE_HS256_TOKENS" in record.getMessage()]


class TestWarningThatHs256IsStillAccepted:
    """Once the keys are published, leaving ``refuse_hs256`` off sends every
    forged HS256 token to Supabase. Nothing said so."""

    def test_it_is_logged_once_when_the_keys_first_appear(self, caplog):
        key = SigningKey()
        fake = FakeSupabase(keys=[])
        clock = Clock()
        auth = authenticator(fake, clock)
        with caplog.at_level(logging.WARNING, logger="app.auth"):
            auth.authenticate(unsigned(jti="before"))
            assert hs256_warnings(caplog) == []

            fake.keys = [key]
            for n in range(3):
                # Each of these fetches the keys again.
                clock.now += 601
                auth.authenticate(unsigned(jti=str(n)))
                auth.authenticate(key.sign(jti=str(n)))
        assert fake.jwks_calls == 4
        (warning,) = hs256_warnings(caplog)
        assert warning.levelno == logging.WARNING

    def test_it_is_not_logged_once_the_setting_is_on(self, caplog):
        key = SigningKey()
        auth = authenticator(FakeSupabase(keys=[key]), refuse_hs256=True)
        with caplog.at_level(logging.WARNING, logger="app.auth"):
            auth.authenticate(unsigned())
            auth.authenticate(key.sign())
        assert hs256_warnings(caplog) == []

    def test_it_is_not_logged_while_the_keys_cannot_be_fetched(self, caplog):
        auth = authenticator(FakeSupabase(keys=[SigningKey()], jwks_status=503))
        with caplog.at_level(logging.WARNING, logger="app.auth"):
            auth.authenticate(unsigned())
        assert hs256_warnings(caplog) == []


class TestRefusalsAreCachedApart:
    """Made-up tokens are free to produce. Sharing one bounded cache, a
    flood of them pushed out the people actually signed in, who then each
    cost a call to Supabase again."""

    def test_a_flood_of_junk_does_not_evict_a_signed_in_caller(self):
        fake = FakeSupabase()
        auth = authenticator(fake, max_cached=2)
        token = unsigned()
        assert auth.authenticate(token) == Caller("user-1")

        for n in range(10):
            assert auth.authenticate(f"junk-{n}") is None
        assert auth.authenticate(token) == Caller("user-1")
        assert len(fake.calls) == 1

    def test_refusals_are_bounded_too(self):
        auth = authenticator(FakeSupabase(), max_cached=2)
        for n in range(10):
            auth.authenticate(f"junk-{n}")
        assert len(auth._refused) == 2
        assert len(auth._tokens) == 0

    def test_an_unconfirmed_account_does_not_take_an_accepted_place(self):
        user = {"id": "user-1", "email_confirmed_at": None}
        auth = authenticator(FakeSupabase(body=json.dumps(user).encode()))
        with pytest.raises(AccountRefusedError):
            auth.authenticate(unsigned())
        assert (len(auth._tokens), len(auth._refused)) == (0, 1)


class StallingSupabase(FakeSupabase):
    """Holds a fetch of the published keys open until released."""

    def __init__(self, **options):
        super().__init__(**options)
        self.stall = False
        self.fetching = threading.Event()
        self.release = threading.Event()

    def __call__(self, url, headers):
        if url == JWKS_URL and self.stall:
            self.fetching.set()
            self.release.wait(10)
        return super().__call__(url, headers)


class TestRefreshingThePublishedKeys:
    """The keys were refetched while holding their lock, so for as long as
    Supabase took to answer -- up to five seconds -- every other token with
    a signature to check waited behind it."""

    def test_a_slow_refetch_does_not_hold_up_other_callers(self):
        key = SigningKey()
        fake = StallingSupabase(keys=[key])
        clock = Clock()
        auth = authenticator(fake, clock)
        assert auth.authenticate(key.sign(jti="first")) == Caller("user-1")

        clock.now += 601
        fake.stall = True
        answers = []
        refresher = threading.Thread(
            target=lambda: answers.append(auth.authenticate(key.sign(jti="second")))
        )
        refresher.start()
        try:
            assert fake.fetching.wait(5)
            started = time.monotonic()
            # Checked against the keys already held.
            assert auth.authenticate(key.sign(jti="third")) == Caller("user-1")
            assert auth.authenticate(SigningKey().sign()) is None
            assert time.monotonic() - started < 2
            assert refresher.is_alive()
        finally:
            fake.release.set()
            refresher.join(10)
        assert answers == [Caller("user-1")]
        assert fake.jwks_calls == 2

    def test_a_failed_refetch_keeps_the_keys_already_held(self):
        key = SigningKey()
        fake = FakeSupabase(keys=[key])
        clock = Clock()
        auth = authenticator(fake, clock, refuse_hs256=True)
        auth.authenticate(key.sign())

        fake.jwks_status = 503
        clock.now += 601
        assert auth.authenticate(SigningKey().sign()) is None
        # Still known to be published, so still refused here.
        assert auth.authenticate(unsigned()) is None
        assert fake.jwks_calls == 2
        assert len(fake.calls) == 1
