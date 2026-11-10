from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from api.commands.main import app
from api.tasks import backups as backup_module
from api.tasks.backups import backup_sqlite_database
from api.tests.fixtures.web_helpers import add_scored_event, migrated_db

runner = CliRunner()


def test_backup_sqlite_database_creates_restorable_backup(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    add_scored_event(db, title="Backup Me")

    result = backup_sqlite_database(
        source=db,
        backup_dir=tmp_path / "backups",
        now=datetime(2026, 4, 29, 12, 30, tzinfo=UTC),
    )

    assert result.path.name == "events-20260429-123000.db"
    conn = sqlite3.connect(result.path)
    try:
        title = conn.execute("SELECT title FROM events WHERE title = ?", ("Backup Me",)).fetchone()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    assert title == ("Backup Me",)
    assert integrity == "ok"


def test_backup_sqlite_database_handles_same_second_collision(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    backup_dir = tmp_path / "backups"
    now = datetime(2026, 4, 29, 12, 30, tzinfo=UTC)

    first = backup_sqlite_database(source=db, backup_dir=backup_dir, now=now)
    second = backup_sqlite_database(source=db, backup_dir=backup_dir, now=now)

    assert first.path.name == "events-20260429-123000.db"
    assert second.path.name == "events-20260429-123000-1.db"
    assert first.path.exists()
    assert second.path.exists()


def test_backup_sqlite_database_handles_source_paths_with_spaces(tmp_path: Path) -> None:
    spaced_dir = tmp_path / "db with spaces"
    spaced_dir.mkdir()
    db = migrated_db(spaced_dir)
    add_scored_event(db, title="Path With Spaces")

    result = backup_sqlite_database(
        source=db,
        backup_dir=tmp_path / "backup with spaces",
        now=datetime(2026, 4, 29, 12, 30, tzinfo=UTC),
    )

    conn = sqlite3.connect(result.path)
    try:
        title = conn.execute("SELECT title FROM events WHERE title = ?", ("Path With Spaces",)).fetchone()
    finally:
        conn.close()
    assert title == ("Path With Spaces",)


def test_backup_sqlite_database_does_not_publish_failed_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = migrated_db(tmp_path)
    backup_dir = tmp_path / "backups"

    def fail_verify(path: Path) -> None:
        raise RuntimeError(f"boom: {path}")

    monkeypatch.setattr(backup_module, "_verify_backup", fail_verify)

    with pytest.raises(RuntimeError, match="boom"):
        backup_sqlite_database(
            source=db,
            backup_dir=backup_dir,
            now=datetime(2026, 4, 29, 12, 30, tzinfo=UTC),
        )

    assert not list(backup_dir.glob("events-*.db"))
    assert not list(backup_dir.glob("*.tmp"))
    assert not list(backup_dir.glob(".*.tmp"))


def test_backup_sqlite_database_prunes_old_backups(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    old = backup_dir / "events-20260301-000000.db"
    old.write_text("old")
    old_collision = backup_dir / "events-20260301-000000-1.db"
    old_collision.write_text("old")
    keep = backup_dir / "events-20260415-000000.db"
    keep.write_text("keep")

    result = backup_sqlite_database(
        source=db,
        backup_dir=backup_dir,
        retention_days=30,
        now=datetime(2026, 4, 29, 12, 30, tzinfo=UTC),
    )

    assert old in result.pruned
    assert not old.exists()
    assert old_collision in result.pruned
    assert not old_collision.exists()
    assert keep.exists()


def test_backup_command_uses_database_path_and_reports_backup(tmp_path: Path, monkeypatch) -> None:
    db = migrated_db(tmp_path)
    add_scored_event(db, title="CLI Backup")
    monkeypatch.setenv("DATABASE_PATH", str(db))

    result = runner.invoke(app, ["backup", "--out-dir", str(tmp_path / "backups")])

    assert result.exit_code == 0, result.stdout
    assert "Backup written:" in result.stdout
    backups = list((tmp_path / "backups").glob("events-*.db"))
    assert len(backups) == 1
