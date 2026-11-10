"""Shared test fixtures for the api package."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip every env var the app reads so a test starts from a known state."""
    for var in (
        "LLM_SCORING_MODE",
        "CLAUDE_OAUTH_MODE",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_OAUTH_TOKEN",
        "CODEX_CLI_PATH",
        "CODEX_MODEL",
        "CODEX_REASONING_EFFORT",
        "PI_AUTH_ANTHROPIC_ACCOUNT",
        "PI_AUTH_CLI_PATH",
        "PI_AUTH_PRINCIPAL",
        "DATABASE_PATH",
        "DB_PATH",
        "DISCORD_MODE",
        "SCRAPER_MODE",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def mock_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the three dev modes to their hermetic test values."""
    monkeypatch.setenv("LLM_SCORING_MODE", "mock")
    monkeypatch.setenv("CLAUDE_OAUTH_MODE", "mock")
    monkeypatch.setenv("DISCORD_MODE", "stub")
    monkeypatch.setenv("SCRAPER_MODE", "fixture")


@pytest.fixture(autouse=True)
def _clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip the dev shell's proxy env vars so httpx doesn't try to route mocked
    test requests through a SOCKS proxy (and fail to import socksio)."""
    for var in (
        "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
        "ALL_PROXY", "all_proxy", "FTP_PROXY", "ftp_proxy",
        "GRPC_PROXY", "grpc_proxy",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep developer and production auth settings out of hermetic tests."""
    for var in (
        "NYC_AI_EVENTS_AGENT_API_TOKEN",
        "NYC_AI_EVENTS_FEEDBACK_EMAIL",
        "NYC_AI_EVENTS_FRIEND_MODE",
        "NYC_AI_EVENTS_GOOGLE_CLIENT_ID",
        "NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET",
        "NYC_AI_EVENTS_PUBLIC_BASE_URL",
        "NYC_AI_EVENTS_SESSION_SECRET",
        "NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY",
        "NYC_AI_EVENTS_SUPABASE_URL",
        "EVENT_HUNT_BREVO_API_KEY", "EVENT_HUNT_POLICY_EFFECTIVE_DATE",
        "EVENT_HUNT_OPEN_GOOGLE_SIGNUP",
    ):
        monkeypatch.delenv(var, raising=False)
