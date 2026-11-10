from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import text

from api.configs.settings import DatabaseSettings
from api.repositories import migrate as migrate_module
from api.repositories.database import create_database
from api.repositories.migrate import (
    MigrationError,
    applied_migrations,
    applied_migrations_sqlalchemy,
    apply_pending,
    apply_pending_sqlalchemy,
    ensure_table,
    migration_dir_for_dialect,
)


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def test_ensure_table_creates_schema_migrations(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    ensure_table(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchall()
    assert len(rows) == 1


def test_applied_migrations_starts_empty(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    ensure_table(conn)
    assert applied_migrations(conn) == []


def test_apply_pending_runs_sql_and_records(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    ensure_table(conn)
    mig_dir = tmp_path / "migs"
    mig_dir.mkdir()
    (mig_dir / "0001_test.sql").write_text(
        "CREATE TABLE foo (id INTEGER PRIMARY KEY); INSERT INTO foo VALUES (1);"
    )
    applied = apply_pending(conn, mig_dir)
    assert applied == ["0001_test.sql"]
    assert applied_migrations(conn) == ["0001_test.sql"]
    assert conn.execute("SELECT id FROM foo").fetchone()["id"] == 1


def test_apply_pending_idempotent(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    ensure_table(conn)
    mig_dir = tmp_path / "migs"
    mig_dir.mkdir()
    (mig_dir / "0001_x.sql").write_text("CREATE TABLE x (id INTEGER);")
    apply_pending(conn, mig_dir)
    # Second run: nothing to apply
    assert apply_pending(conn, mig_dir) == []


def test_apply_pending_orders_by_filename(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    ensure_table(conn)
    mig_dir = tmp_path / "migs"
    mig_dir.mkdir()
    (mig_dir / "0002_b.sql").write_text("CREATE TABLE b (id INTEGER);")
    (mig_dir / "0001_a.sql").write_text("CREATE TABLE a (id INTEGER);")
    applied = apply_pending(conn, mig_dir)
    assert applied == ["0001_a.sql", "0002_b.sql"]


def test_apply_pending_failure_rolls_back(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    ensure_table(conn)
    mig_dir = tmp_path / "migs"
    mig_dir.mkdir()
    (mig_dir / "0001_bad.sql").write_text("CREATE TABLE good (id INTEGER PRIMARY KEY); SYNTAX ERROR;")
    import pytest
    with pytest.raises(MigrationError):
        apply_pending(conn, mig_dir)
    # 'good' must NOT exist (atomic rollback)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='good'"
    ).fetchall()
    assert rows == []
    assert applied_migrations(conn) == []


def test_migration_dir_for_dialect() -> None:
    root = Path("/repo")

    assert migration_dir_for_dialect("sqlite", root) == root / "db" / "migrate" / "sqlite"
    assert migration_dir_for_dialect("postgres", root) == root / "db" / "migrate" / "postgres"


def test_migration_dir_prefers_packaged_runtime_assets(tmp_path: Path, monkeypatch) -> None:
    api_root = tmp_path / "site-packages" / "api"
    packaged = api_root / "_runtime" / "db" / "migrate" / "sqlite"
    packaged.mkdir(parents=True)
    module_file = api_root / "repositories" / "migrate.py"
    module_file.parent.mkdir(parents=True)
    module_file.write_text("")

    monkeypatch.setattr(migrate_module, "__file__", str(module_file))

    assert migration_dir_for_dialect("sqlite") == packaged


def test_apply_pending_sqlalchemy_runs_sql_and_records(tmp_path: Path) -> None:
    db = create_database(
        DatabaseSettings(
            url=f"sqlite:///{tmp_path / 'sa.db'}",
            dialect="sqlite",
            redacted_url="sqlite:///sa.db",
        )
    )
    mig_dir = tmp_path / "migs"
    mig_dir.mkdir()
    (mig_dir / "0001_test.sql").write_text(
        "CREATE TABLE foo (id INTEGER PRIMARY KEY); INSERT INTO foo VALUES (1);"
    )

    with db.begin() as conn:
        applied = apply_pending_sqlalchemy(conn, mig_dir)
        assert applied_migrations_sqlalchemy(conn) == ["0001_test.sql"]

    assert applied == ["0001_test.sql"]
    with db.connect() as conn:
        assert conn.execute(text("SELECT id FROM foo")).scalar_one() == 1
