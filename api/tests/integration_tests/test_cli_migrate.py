from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from api.commands.main import app

runner = CliRunner()


def test_migrate_runs_pending(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    # Bare DB — migrate should create schema_migrations + apply 0001.
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.stdout
    assert "0001_pivot_to_channel.sql" in result.stdout
    # Second run = no-op
    result2 = runner.invoke(app, ["migrate"])
    assert result2.exit_code == 0
    assert "no pending migrations" in result2.stdout.lower() or "0 applied" in result2.stdout.lower()
