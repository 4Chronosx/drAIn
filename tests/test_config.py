"""Tests for reading settings from the environment."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import Settings, _env_flag, deployment_problems, insecure_deploy_allowed


class TestFlags:
    """Regression: anything that was not a known "off" counted as on, so a
    typo such as ``ENABLE_DOCS=flase`` served the API docs."""

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "On", "  true  "])
    def test_the_known_spellings_of_on(self, monkeypatch, raw):
        monkeypatch.setenv("SOME_FLAG", raw)
        assert _env_flag("SOME_FLAG", False) is True

    @pytest.mark.parametrize("raw", ["0", "false", "False", "no", "OFF", " off\n"])
    def test_the_known_spellings_of_off(self, monkeypatch, raw):
        monkeypatch.setenv("SOME_FLAG", raw)
        assert _env_flag("SOME_FLAG", True) is False

    @pytest.mark.parametrize("default", [True, False])
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_unset_or_empty_is_the_default(self, monkeypatch, raw, default):
        if raw is None:
            monkeypatch.delenv("SOME_FLAG", raising=False)
        else:
            monkeypatch.setenv("SOME_FLAG", raw)
        assert _env_flag("SOME_FLAG", default) is default

    @pytest.mark.parametrize("raw", ["flase", "ture", "2", "enabled", "y"])
    def test_anything_else_is_an_error_naming_the_variable(self, monkeypatch, raw):
        monkeypatch.setenv("SOME_FLAG", raw)
        with pytest.raises(ValueError, match="SOME_FLAG"):
            _env_flag("SOME_FLAG", False)

    def test_a_mistyped_flag_stops_the_settings_being_read(self, monkeypatch):
        monkeypatch.setenv("ENABLE_DOCS", "flase")
        with pytest.raises(ValueError, match="ENABLE_DOCS"):
            Settings.from_env()


class TestRefusingSharedSecretTokens:
    """``REFUSE_HS256_TOKENS`` is turned on last, when a project has moved
    to asymmetric signing keys and its older sessions have expired."""

    def test_it_is_off_unless_set(self, monkeypatch):
        monkeypatch.delenv("REFUSE_HS256_TOKENS", raising=False)
        assert Settings.from_env().refuse_hs256_tokens is False
        assert Settings().refuse_hs256_tokens is False

    @pytest.mark.parametrize(("raw", "expected"), [("true", True), ("1", True), ("false", False)])
    def test_it_is_read_from_the_environment(self, monkeypatch, raw, expected):
        monkeypatch.setenv("REFUSE_HS256_TOKENS", raw)
        assert Settings.from_env().refuse_hs256_tokens is expected

    def test_a_mistyped_value_is_an_error(self, monkeypatch):
        monkeypatch.setenv("REFUSE_HS256_TOKENS", "ture")
        with pytest.raises(ValueError, match="REFUSE_HS256_TOKENS"):
            Settings.from_env()


class TestStoredRunsCoverTheHour:
    """The hourly allowance is counted from the stored runs, so a restart
    doesn't reset it. Keeping fewer than an hour's worth deleted the rows
    that count needs, and the allowance was never reached."""

    def test_keeping_fewer_runs_than_the_hour_allows_is_an_error(self, monkeypatch):
        monkeypatch.setenv("MAX_STORED_RUNS_PER_USER", "5")
        monkeypatch.setenv("MAX_RUNS_PER_USER_PER_HOUR", "10")
        with pytest.raises(ValueError, match="MAX_STORED_RUNS_PER_USER") as refusal:
            Settings.from_env()
        assert "MAX_RUNS_PER_USER_PER_HOUR" in str(refusal.value)

    def test_the_default_allowance_counts_against_a_lowered_keep(self, monkeypatch):
        monkeypatch.delenv("MAX_RUNS_PER_USER_PER_HOUR", raising=False)
        monkeypatch.setenv("MAX_STORED_RUNS_PER_USER", "9")
        with pytest.raises(ValueError, match="MAX_STORED_RUNS_PER_USER"):
            Settings.from_env()

    @pytest.mark.parametrize(("stored", "hourly"), [("10", "10"), ("20", "10"), ("0", "10")])
    def test_as_many_or_all_of_them_is_fine(self, monkeypatch, stored, hourly):
        # 0 keeps every run until it ages out.
        monkeypatch.setenv("MAX_STORED_RUNS_PER_USER", stored)
        monkeypatch.setenv("MAX_RUNS_PER_USER_PER_HOUR", hourly)
        assert Settings.from_env().max_stored_runs_per_user == int(stored)

    def test_the_defaults_are_fine(self, monkeypatch):
        monkeypatch.delenv("MAX_STORED_RUNS_PER_USER", raising=False)
        monkeypatch.delenv("MAX_RUNS_PER_USER_PER_HOUR", raising=False)
        assert Settings.from_env().max_stored_runs_per_user == 20


class TestSecrets:
    def test_the_service_role_key_is_not_in_the_repr(self):
        config = Settings(
            supabase_url="https://p.supabase.co", supabase_service_role_key="service-secret"
        )
        assert "service-secret" not in repr(config)
        assert "p.supabase.co" in repr(config)

    def test_the_service_role_key_is_still_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-secret")
        assert Settings.from_env().supabase_service_role_key == "service-secret"


#: Render sets this in the environment of every service it runs.
ON_RENDER = {"RENDER": "true"}


class TestDeploymentProblems:
    """What the server checks before it starts on Render, which says where
    it is by setting ``RENDER`` in the environment."""

    SOUND = Settings(
        require_auth=True,
        trusted_proxy_hops=1,
        supabase_url="https://p.supabase.co",
        supabase_anon_key="anon-key",
    )

    def test_sound_settings_have_none(self):
        assert deployment_problems(self.SOUND, ON_RENDER) == []

    def test_sign_in_turned_off_is_one(self):
        config = replace(self.SOUND, require_auth=False)
        (problem,) = deployment_problems(config, ON_RENDER)
        assert "REQUIRE_AUTH" in problem

    def test_no_trusted_proxy_is_one(self):
        config = replace(self.SOUND, trusted_proxy_hops=0)
        (problem,) = deployment_problems(config, ON_RENDER)
        assert "TRUSTED_PROXY_HOPS" in problem

    def test_the_defaults_are_not_fit_to_deploy(self):
        # They are a developer's: no proxy in front, no Supabase project.
        assert len(deployment_problems(Settings(), ON_RENDER)) == 2
        # With sign-in off there is no project to miss; being off is the problem.
        assert len(deployment_problems(Settings(require_auth=False), ON_RENDER)) == 2

    @pytest.mark.parametrize(
        ("missing", "named", "not_named"),
        [
            ({"supabase_url": None}, "SUPABASE_URL is", "SUPABASE_ANON_KEY"),
            ({"supabase_anon_key": None}, "SUPABASE_ANON_KEY is", "SUPABASE_URL"),
            (
                {"supabase_url": None, "supabase_anon_key": None},
                "SUPABASE_URL and SUPABASE_ANON_KEY are",
                None,
            ),
        ],
    )
    def test_no_way_to_sign_anyone_in_is_one(self, missing, named, not_named):
        """Regression: the server started, passed its health check, and
        answered every simulation with 503."""
        (problem,) = deployment_problems(replace(self.SOUND, **missing), ON_RENDER)
        assert named in problem
        assert not_named is None or not_named not in problem

    def test_sign_in_turned_off_does_not_also_miss_its_settings(self):
        config = replace(self.SOUND, require_auth=False, supabase_url=None)
        (problem,) = deployment_problems(config, ON_RENDER)
        assert "REQUIRE_AUTH" in problem

    def test_unconfirmed_accounts_let_in_is_one(self):
        config = replace(self.SOUND, require_confirmed_email=False)
        (problem,) = deployment_problems(config, ON_RENDER)
        assert "REQUIRE_CONFIRMED_EMAIL" in problem

    @pytest.mark.parametrize(
        ("setting", "variable"),
        [
            ("submit_rate_per_minute", "SUBMIT_RATE_LIMIT_PER_MINUTE"),
            ("poll_rate_per_minute", "POLL_RATE_LIMIT_PER_MINUTE"),
        ],
    )
    @pytest.mark.parametrize("value", [0, -1])
    def test_a_rate_limit_turned_off_is_one(self, setting, variable, value):
        (problem,) = deployment_problems(replace(self.SOUND, **{setting: value}), ON_RENDER)
        assert variable in problem and "off" in problem

    @pytest.mark.parametrize(
        ("setting", "variable"),
        [
            ("max_jobs_per_user", "MAX_JOBS_PER_USER"),
            ("max_runs_per_user_per_hour", "MAX_RUNS_PER_USER_PER_HOUR"),
            ("max_jobs_per_ip", "MAX_JOBS_PER_IP"),
        ],
    )
    @pytest.mark.parametrize("value", [0, -1])
    def test_a_cap_nobody_is_under_is_one(self, setting, variable, value):
        # Zero is not "no cap" for these: it refuses every run.
        (problem,) = deployment_problems(replace(self.SOUND, **{setting: value}), ON_RENDER)
        assert variable in problem and "refused" in problem

    def test_limits_of_one_are_limits(self):
        config = replace(
            self.SOUND,
            submit_rate_per_minute=1,
            poll_rate_per_minute=1,
            max_jobs_per_user=1,
            max_runs_per_user_per_hour=1,
            max_jobs_per_ip=1,
        )
        assert deployment_problems(config, ON_RENDER) == []

    @pytest.mark.parametrize(
        "choice", [{"queue_slots_reserved": 0}, {"max_stored_runs_per_user": 0}]
    )
    def test_a_zero_that_is_a_documented_choice_is_not_one(self, choice):
        assert deployment_problems(replace(self.SOUND, **choice), ON_RENDER) == []

    def test_no_service_role_key_is_not_one(self):
        # The server works without it, keeping runs in memory only, and
        # warns when it is built (tests/test_api.py).
        assert self.SOUND.supabase_service_role_key is None
        assert deployment_problems(self.SOUND, ON_RENDER) == []

    @pytest.mark.parametrize("environ", [{}, {"RENDER": ""}, {"RENDER": "  "}])
    def test_nothing_is_checked_off_render(self, environ):
        assert deployment_problems(Settings(require_auth=False), environ) == []

    def test_the_real_environment_is_not_consulted(self, monkeypatch):
        monkeypatch.setenv("RENDER", "true")
        assert deployment_problems(Settings(), {}) == []

    @pytest.mark.parametrize(
        ("environ", "allowed"),
        [
            ({}, False),
            ({"ALLOW_INSECURE_DEPLOY": ""}, False),
            ({"ALLOW_INSECURE_DEPLOY": "false"}, False),
            ({"ALLOW_INSECURE_DEPLOY": "true"}, True),
            ({"ALLOW_INSECURE_DEPLOY": "1"}, True),
        ],
    )
    def test_the_override_is_a_flag_like_any_other(self, environ, allowed):
        assert insecure_deploy_allowed(environ) is allowed

    def test_a_mistyped_override_is_an_error_not_a_yes(self):
        with pytest.raises(ValueError, match="ALLOW_INSECURE_DEPLOY"):
            insecure_deploy_allowed({"ALLOW_INSECURE_DEPLOY": "ture"})
