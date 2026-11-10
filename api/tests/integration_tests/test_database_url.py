from __future__ import annotations

import pytest

import api.configs.settings as config


def test_database_url_defaults_to_sqlite_data_path(isolated_env: None) -> None:
    assert config.database_url() == "sqlite:///data/events.db"
    settings = config.database_settings()
    assert settings.dialect == "sqlite"
    assert settings.redacted_url == "sqlite:///data/events.db"


def test_database_path_env_becomes_sqlite_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", "tmp/dev.db")

    assert config.database_url() == "sqlite:///tmp/dev.db"


def test_database_url_wins_over_database_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", "tmp/dev.db")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@localhost:5432/app")

    settings = config.database_settings()

    assert settings.dialect == "postgres"
    assert settings.url == "postgresql+psycopg://user:secret@localhost:5432/app"
    assert settings.redacted_url == "postgresql+psycopg://user:***@localhost:5432/app"


def test_split_cloud_sql_env_builds_socket_postgres_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGUSER", "nyc_ai_events")
    monkeypatch.setenv("PGPASSWORD", "secret value")
    monkeypatch.setenv("PGDATABASE", "nyc_ai_events")
    monkeypatch.setenv("CLOUD_SQL_INSTANCE_CONNECTION_NAME", "proj:us-central1:db")

    settings = config.database_settings()

    assert settings.dialect == "postgres"
    assert settings.url == (
        "postgresql+psycopg://nyc_ai_events:secret%20value@/nyc_ai_events"
        "?host=/cloudsql/proj%3Aus-central1%3Adb"
    )
    assert "secret value" not in settings.url
    assert "secret value" not in settings.redacted_url
    assert "secret%20value" not in settings.redacted_url


def test_split_cloud_sql_env_escapes_url_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGUSER", "nyc/ai")
    monkeypatch.setenv("PGPASSWORD", "secret/with/slash")
    monkeypatch.setenv("PGDATABASE", "events/db")
    monkeypatch.setenv("CLOUD_SQL_INSTANCE_CONNECTION_NAME", "proj:us-central1:db")

    settings = config.database_settings()

    assert "nyc/ai" not in settings.url
    assert "secret/with/slash" not in settings.url
    assert "events/db" not in settings.url
    assert settings.url.startswith("postgresql+psycopg://nyc%2Fai:")


def test_incomplete_split_postgres_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGUSER", "nyc_ai_events")

    with pytest.raises(ValueError, match="incomplete Postgres database env"):
        config.database_url()


def test_invalid_database_url_scheme_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql://user:pw@localhost/db")

    with pytest.raises(ValueError, match="sqlite:// or postgresql://"):
        config.database_url()


def test_taste_profile_text_prefers_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    profile = tmp_path / "taste.md"
    profile.write_text("file profile")
    monkeypatch.setenv("TASTE_PROFILE_MD", "inline profile")

    assert config.taste_profile_text(profile) == "inline profile"


def test_taste_profile_text_can_read_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    profile = tmp_path / "taste.md"
    profile.write_text("file profile")
    monkeypatch.setenv("TASTE_PROFILE_PATH", str(profile))

    assert config.taste_profile_text() == "file profile"
