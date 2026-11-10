from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import api.configs.settings as config
from api.commands.main import app
from api.tasks.cycle import CycleResult

runner = CliRunner()


def test_database_path_defaults_to_data_events_db(isolated_env: None) -> None:
    assert config.database_path() == Path("data/events.db")


def test_database_path_prefers_database_path_env(monkeypatch) -> None:
    monkeypatch.setenv("DB_PATH", "legacy.db")
    monkeypatch.setenv("DATABASE_PATH", "canonical.db")

    assert config.database_path() == Path("canonical.db")


def test_database_path_supports_legacy_db_path(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.setenv("DB_PATH", "legacy.db")

    assert config.database_path() == Path("legacy.db")


def test_cycle_uses_canonical_database_path(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "cycle.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/abc")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "taste-profile.md").write_text("likes infra")

    seen: dict[str, Path] = {}

    def fake_run_cycle(cfg):
        seen["db_path"] = cfg.db_path
        return CycleResult()

    monkeypatch.setattr("api.tasks.cycle.run_cycle", fake_run_cycle)

    result = runner.invoke(app, ["cycle", "--sources", "luma"])

    assert result.exit_code == 0, result.stdout
    assert seen["db_path"] == db


def test_migrate_uses_legacy_db_path_fallback(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "legacy-migrate.db"
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.setenv("DB_PATH", str(db))

    result = runner.invoke(app, ["migrate"])

    assert result.exit_code == 0, result.stdout
    assert db.exists()
