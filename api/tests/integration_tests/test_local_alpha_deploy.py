from __future__ import annotations

import plistlib
from pathlib import Path

from api.integrations.llm.codex_cli import DEFAULT_MODEL

ROOT = Path(__file__).resolve().parents[3]


def test_friends_alpha_env_is_ignored_and_example_documents_required_values() -> None:
    gitignore = (ROOT / ".gitignore").read_text()
    example = (ROOT / "deploy" / "friends-alpha.env.example").read_text()

    assert ".env.friends-alpha" in gitignore
    for name in (
        "NYC_AI_EVENTS_FRIEND_MODE",
        "NYC_AI_EVENTS_PUBLIC_BASE_URL",
        "NYC_AI_EVENTS_SESSION_SECRET",
        "NYC_AI_EVENTS_GOOGLE_CLIENT_ID",
        "NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET",
        "DATABASE_PATH",
    ):
        assert name in example
    assert "replace-with" in example
    assert "DATABASE_PATH=/path/to/event-hunt/app/data/events.db" in example
    assert "NGROK_DOMAIN" not in example


def test_web_launchd_template_is_parseable_and_local_only() -> None:
    web = plistlib.loads((ROOT / "deploy" / "com.yang.nyc-ai-events-web.plist").read_bytes())

    assert web["Label"] == "com.yang.nyc-ai-events-web"
    web_command = " ".join(web["ProgramArguments"])
    assert ".env.friends-alpha" in web_command
    assert "export NYC_AI_EVENTS_FRIEND_MODE=1" in web_command
    assert "RUNTIME_PATH/.venv/bin/nyc-ai-scout" in web_command
    assert "nyc-ai-scout migrate" in web_command
    assert "nyc-ai-scout serve --host 127.0.0.1 --port 8847" in web_command
    assert web["RunAtLoad"] is True
    assert web["KeepAlive"] is True
    assert not (ROOT / "deploy" / "com.yang.nyc-ai-events-ngrok.plist").exists()


def test_backup_launchd_template_runs_before_cleanup_window() -> None:
    backup = plistlib.loads((ROOT / "deploy" / "com.yang.nyc-ai-events-backup.plist").read_bytes())

    assert backup["Label"] == "com.yang.nyc-ai-events-backup"
    command = " ".join(backup["ProgramArguments"])
    assert "RUNTIME_PATH/.venv/bin/nyc-ai-scout" in command
    assert "nyc-ai-scout backup" in command
    assert "--retention-days 30" in command
    assert backup["StartCalendarInterval"]["Hour"] == 3


def test_all_database_touching_launchd_templates_source_friends_alpha_env() -> None:
    for filename in (
        "com.yang.nyc-ai-events.plist",
        "com.yang.nyc-ai-events-web.plist",
        "com.yang.nyc-ai-events-backup.plist",
        "com.yang.nyc-ai-events-cleanup.plist",
    ):
        plist = plistlib.loads((ROOT / "deploy" / filename).read_bytes())
        command = " ".join(plist["ProgramArguments"])
        assert ".env.friends-alpha" in command
        assert "source RUNTIME_PATH/.env.friends-alpha &&" in command
        assert "export PYTHONSAFEPATH=1" in command
        assert "uv run" not in command
        assert "RUNTIME_PATH/.venv/bin/nyc-ai-scout" in command


def test_all_package_runtime_launchd_templates_enable_safe_python_path() -> None:
    for filename in (
        "com.yang.nyc-ai-events.plist",
        "com.yang.nyc-ai-events-web.plist",
        "com.yang.nyc-ai-events-backup.plist",
        "com.yang.nyc-ai-events-cleanup.plist",
        "com.yang.nyc-ai-events.scout-bot.plist",
        "com.yang.nyc-ai-events.capacity-poller.plist",
    ):
        plist = plistlib.loads((ROOT / "deploy" / filename).read_bytes())
        command = " ".join(plist["ProgramArguments"])
        assert "export PYTHONSAFEPATH=1" in command
        assert "RUNTIME_PATH/.venv/bin/nyc-ai-scout" in command


def test_scoring_runtime_uses_current_cost_sensitive_codex_model() -> None:
    for filename in (
        "com.yang.nyc-ai-events.plist",
        "com.yang.nyc-ai-events.scout-bot.plist",
        "com.yang.nyc-ai-events.capacity-poller.plist",
    ):
        plist = plistlib.loads((ROOT / "deploy" / filename).read_bytes())
        assert plist["EnvironmentVariables"]["CODEX_MODEL"] == DEFAULT_MODEL

    example = (ROOT / "deploy" / "friends-alpha.env.example").read_text()
    assert f"CODEX_MODEL={DEFAULT_MODEL}" in example
