from __future__ import annotations

from pathlib import Path

import pytest
import typer

from api.commands.deploy import redact_for_log, resolve_env
from api.configs.required_env import check_env_file


def test_resolve_env_staging() -> None:
    cfg = resolve_env("staging")

    assert cfg.name == "staging"
    assert cfg.checkout == Path.home() / ".local/share/nyc-ai-events/app-staging"
    assert cfg.env_file == Path.home() / ".local/share/nyc-ai-events/.env.staging"
    assert cfg.db_path == Path.home() / ".local/share/nyc-ai-events/app-staging/data/staging.db"
    assert cfg.services == ("com.yang.nyc-ai-events-staging-web",)
    assert cfg.smoke_services == cfg.services
    assert cfg.web_port == 8848
    assert cfg.requires_origin_ancestor_check is False
    assert cfg.sync_extras == ("dev",)
    assert cfg.scheduled_services == ()


def test_resolve_env_production() -> None:
    cfg = resolve_env("production")

    assert cfg.name == "production"
    assert cfg.checkout == Path.home() / ".local/share/nyc-ai-events/app"
    assert cfg.env_file == Path.home() / ".local/share/nyc-ai-events/app/.env.friends-alpha"
    assert cfg.db_path == Path.home() / ".local/share/nyc-ai-events/app/data/events.db"
    assert cfg.services == ("com.yang.nyc-ai-events-web",)
    assert cfg.smoke_services == (
        "com.yang.nyc-ai-events",
        "com.yang.nyc-ai-events-web",
        "com.yang.nyc-ai-events.scout-bot",
        "com.yang.nyc-ai-events.capacity-poller",
    )
    assert cfg.web_port == 8847
    assert cfg.requires_origin_ancestor_check is True
    assert cfg.sync_extras == ()
    assert cfg.scheduled_services == (
        "com.yang.nyc-ai-events",
        "com.yang.nyc-ai-events.capacity-poller",
    )


def test_resolve_env_rejects_unknown_env() -> None:
    with pytest.raises(typer.BadParameter):
        resolve_env("dev")


def test_redact_for_log_covers_secret_key_patterns() -> None:
    redacted = redact_for_log(
        {
            "NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET": "abc",
            "NYC_AI_EVENTS_GOOGLE_CLIENT_ID": "client-id",
            "OAUTH_TOKEN": "token",
            "SESSION_COOKIE": "cookie",
            "DISCORD_WEBHOOK_URL": "https://discord.test/webhook",
            "DATABASE_ROW": "{full row}",
            "PORT": "8848",
        }
    )

    assert redacted == {
        "NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET": "***REDACTED***",
        "NYC_AI_EVENTS_GOOGLE_CLIENT_ID": "***REDACTED***",
        "OAUTH_TOKEN": "***REDACTED***",
        "SESSION_COOKIE": "***REDACTED***",
        "DISCORD_WEBHOOK_URL": "***REDACTED***",
        "DATABASE_ROW": "***REDACTED***",
        "PORT": "8848",
    }


def test_check_env_file_reports_missing_required_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.staging"
    env_file.write_text("DATABASE_PATH=/tmp/events.db\n")

    assert check_env_file(env_file, "staging") == ["NYC_AI_EVENTS_SESSION_SECRET"]


def test_check_env_file_accepts_complete_staging_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.staging"
    env_file.write_text(
        "DATABASE_PATH=/tmp/events.db\n"
        "NYC_AI_EVENTS_SESSION_SECRET=private-session-secret-private-session\n"
    )

    assert check_env_file(env_file, "staging") == []
