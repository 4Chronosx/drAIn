"""Tests for reading settings from the environment."""

from __future__ import annotations

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

    SOUND = Settings(require_auth=True, trusted_proxy_hops=1)

    def test_sound_settings_have_none(self):
        assert deployment_problems(self.SOUND, ON_RENDER) == []

    def test_sign_in_turned_off_is_one(self):
        config = Settings(require_auth=False, trusted_proxy_hops=1)
        (problem,) = deployment_problems(config, ON_RENDER)
        assert "REQUIRE_AUTH" in problem

    def test_no_trusted_proxy_is_one(self):
        config = Settings(require_auth=True, trusted_proxy_hops=0)
        (problem,) = deployment_problems(config, ON_RENDER)
        assert "TRUSTED_PROXY_HOPS" in problem

    def test_the_defaults_are_not_fit_to_deploy(self):
        # They are a developer's: no proxy in front.
        assert len(deployment_problems(Settings(), ON_RENDER)) == 1
        assert len(deployment_problems(Settings(require_auth=False), ON_RENDER)) == 2

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
