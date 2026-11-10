"""Smoke tests for the three dev modes (Pre-M0).

Confirms that env-var switching alone is enough to run the test suite without
pi-auth / Discord / live scrapers.
"""

from __future__ import annotations

import pytest

import api.configs.settings as config
from api.integrations.llm import EventFit, score_batch


class TestDefaults:
    def test_llm_default_is_codex_cli(self, isolated_env: None) -> None:
        assert config.llm_scoring_mode() == "codex-cli"
        assert config.claude_oauth_mode() == "codex-cli"

    def test_discord_default_is_live(self, isolated_env: None) -> None:
        assert config.discord_mode() == "live"

    def test_scraper_default_is_live(self, isolated_env: None) -> None:
        assert config.scraper_mode() == "live"

    def test_db_path_default(self, isolated_env: None) -> None:
        assert config.db_path() == "data/events.db"

    def test_friend_alpha_auth_config_defaults_off(self, isolated_env: None) -> None:
        assert config.public_base_url() == ""
        assert config.friend_mode_enabled() is False
        assert config.google_oauth_configured() is False


class TestModeSwitching:
    def test_mock_modes_fixture_sets_all_three(
        self, mock_modes: None
    ) -> None:
        assert config.claude_oauth_mode() == "mock"
        assert config.llm_scoring_mode() == "mock"
        assert config.discord_mode() == "stub"
        assert config.scraper_mode() == "fixture"

    def test_llm_scoring_mode_prefers_new_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_OAUTH_MODE", "cli")
        monkeypatch.setenv("LLM_SCORING_MODE", "codex-cli")
        assert config.llm_scoring_mode() == "codex-cli"

    def test_invalid_claude_mode_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_SCORING_MODE", "openai")
        with pytest.raises(ValueError, match="LLM_SCORING_MODE"):
            config.llm_scoring_mode()

    def test_invalid_discord_mode_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISCORD_MODE", "telegram")
        with pytest.raises(ValueError, match="DISCORD_MODE"):
            config.discord_mode()

    def test_invalid_scraper_mode_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCRAPER_MODE", "wishful_thinking")
        with pytest.raises(ValueError, match="SCRAPER_MODE"):
            config.scraper_mode()

    def test_friend_alpha_auth_config_reads_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.com/")
        monkeypatch.setenv("NYC_AI_EVENTS_FRIEND_MODE", "1")
        monkeypatch.setenv(
            "NYC_AI_EVENTS_GOOGLE_CLIENT_ID",
            "1234567890-alpha.apps.googleusercontent.com",
        )
        monkeypatch.setenv("NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET", "GOCSPX-alpha-secret")

        assert config.public_base_url() == "https://events.example.com"
        assert config.friend_mode_enabled() is True
        assert config.google_oauth_client_id() == "1234567890-alpha.apps.googleusercontent.com"
        assert config.google_oauth_client_secret() == "GOCSPX-alpha-secret"
        assert config.google_oauth_configured() is True


class TestMockScoreBatch:
    async def test_returns_canned_fits(self, mock_modes: None) -> None:
        fits = await score_batch([1, 2, 3], taste_profile="ignored in mock")
        assert [f.event_id for f in fits] == [1, 2, 3]
        assert all(isinstance(f, EventFit) for f in fits)

    async def test_preserves_caller_order(self, mock_modes: None) -> None:
        fits = await score_batch([3, 1, 2], taste_profile="ignored")
        assert [f.event_id for f in fits] == [3, 1, 2]

    async def test_missing_event_id_raises(self, mock_modes: None) -> None:
        with pytest.raises(KeyError, match="999"):
            await score_batch([1, 999], taste_profile="ignored")

    async def test_pi_auth_mode_rejects_bare_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_OAUTH_MODE", "pi-auth")
        with pytest.raises(ValueError, match="event dicts"):
            await score_batch([1], taste_profile="ignored")

    async def test_fixture_data_is_valid_event_fit(self, mock_modes: None) -> None:
        fits = await score_batch([1], taste_profile="ignored")
        assert 0.0 <= fits[0].fit_score <= 1.0
        assert fits[0].urgency in ("low", "medium", "high")
        assert len(fits[0].reasoning) <= 200
