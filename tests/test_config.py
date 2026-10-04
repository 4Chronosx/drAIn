"""Tests for reading settings from the environment."""

from __future__ import annotations

import pytest

from app.config import Settings, _env_flag


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
