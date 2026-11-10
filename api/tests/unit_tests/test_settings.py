import pytest

import api.configs.settings as runtime_settings
from api.configs.settings import Settings, load_settings


@pytest.mark.parametrize('value,enabled', [('', False), ('0', False), ('true', False), ('1', True)])
def test_google_signup_requires_explicit_enablement(monkeypatch, value, enabled):
    monkeypatch.setenv('EVENT_HUNT_OPEN_GOOGLE_SIGNUP', value)
    assert runtime_settings.open_google_signup() is enabled


def test_open_google_signup_rejects_missing_supabase_configuration(monkeypatch, tmp_path):
    from api.app import create_app

    monkeypatch.setenv('EVENT_HUNT_OPEN_GOOGLE_SIGNUP', '1')
    with pytest.raises(RuntimeError, match='requires Supabase Auth'):
        create_app(db_path=tmp_path/'events.db', session_secret='test-secret')


def test_load_settings_accepts_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///tmp/test.db")
    settings = load_settings()
    assert isinstance(settings, Settings)
    assert (
        settings.database_url == "sqlite:////tmp/test.db"
        or settings.database_url == "sqlite:///tmp/test.db"
    )


def test_supabase_urls_are_derived_from_one_https_project_url(monkeypatch) -> None:
    monkeypatch.setenv("NYC_AI_EVENTS_SUPABASE_URL", "https://project-ref.supabase.co/")

    assert runtime_settings.supabase_url() == "https://project-ref.supabase.co"
    assert runtime_settings.supabase_issuer_url() == "https://project-ref.supabase.co/auth/v1"
    assert (
        runtime_settings.supabase_jwks_url()
        == "https://project-ref.supabase.co/auth/v1/.well-known/jwks.json"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://project-ref.supabase.co",
        "https://project-ref.supabase.co/path",
        "https://project-ref.supabase.co?secret=no",
        "https://replace-with-project.supabase.co",
    ],
)
def test_supabase_project_url_fails_closed(value: str) -> None:
    assert runtime_settings.supabase_url_config_error(value) is not None


def test_mcp_auth_mode_selects_exactly_one_provider(monkeypatch) -> None:
    monkeypatch.delenv("NYC_AI_EVENTS_AGENT_API_TOKEN", raising=False)
    monkeypatch.delenv("NYC_AI_EVENTS_SUPABASE_URL", raising=False)
    monkeypatch.delenv("NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY", raising=False)
    assert runtime_settings.mcp_auth_mode() == "disabled"

    monkeypatch.setenv("NYC_AI_EVENTS_AGENT_API_TOKEN", "a" * 32)
    assert runtime_settings.mcp_auth_mode() == "static"

    monkeypatch.delenv("NYC_AI_EVENTS_AGENT_API_TOKEN")
    monkeypatch.setenv("NYC_AI_EVENTS_SUPABASE_URL", "https://project-ref.supabase.co")
    monkeypatch.setenv("NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_gate0")
    assert runtime_settings.mcp_auth_mode() == "supabase"


def test_mcp_auth_mode_rejects_partial_or_ambiguous_supabase_config(monkeypatch) -> None:
    monkeypatch.setenv("NYC_AI_EVENTS_SUPABASE_URL", "https://project-ref.supabase.co")
    monkeypatch.delenv("NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY", raising=False)
    monkeypatch.delenv("NYC_AI_EVENTS_AGENT_API_TOKEN", raising=False)

    with pytest.raises(ValueError, match="SUPABASE_PUBLISHABLE_KEY"):
        runtime_settings.mcp_auth_mode()

    monkeypatch.setenv("NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_gate0")
    monkeypatch.setenv("NYC_AI_EVENTS_AGENT_API_TOKEN", "a" * 32)

    with pytest.raises(ValueError, match="exactly one"):
        runtime_settings.mcp_auth_mode()
