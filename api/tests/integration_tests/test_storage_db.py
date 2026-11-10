from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from api.configs.settings import DatabaseSettings
from api.repositories.database import create_database


def test_sqlite_database_begin_commits(tmp_path: Path) -> None:
    db = create_database(
        DatabaseSettings(
            url=f"sqlite:///{tmp_path / 'events.db'}",
            dialect="sqlite",
            redacted_url="sqlite:///events.db",
        )
    )

    with db.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"))
        conn.execute(text("INSERT INTO t (name) VALUES (:name)"), {"name": "yang"})

    with db.connect() as conn:
        row = conn.execute(text("SELECT name FROM t")).mappings().one()

    assert row["name"] == "yang"


def test_sqlite_database_begin_rolls_back_on_exception(tmp_path: Path) -> None:
    db = create_database(
        DatabaseSettings(
            url=f"sqlite:///{tmp_path / 'events.db'}",
            dialect="sqlite",
            redacted_url="sqlite:///events.db",
        )
    )
    with db.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"))

    with pytest.raises(RuntimeError, match="boom"), db.begin() as conn:
        conn.execute(text("INSERT INTO t (name) VALUES (:name)"), {"name": "rolled-back"})
        raise RuntimeError("boom")

    with db.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM t")).scalar_one()

    assert count == 0


def test_postgres_database_uses_bounded_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_POOL_SIZE", "3")
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "1")
    db = create_database(
        DatabaseSettings(
            url="postgresql+psycopg://user:secret@localhost:5432/app",
            dialect="postgres",
            redacted_url="postgresql+psycopg://user:***@localhost:5432/app",
        )
    )

    assert db.dialect == "postgres"
    assert db.engine.pool.size() == 3
    assert db.engine.pool._max_overflow == 1


def test_compat_connection_translates_qmark_params(tmp_path: Path) -> None:
    db = create_database(
        DatabaseSettings(
            url=f"sqlite:///{tmp_path / 'events.db'}",
            dialect="sqlite",
            redacted_url="sqlite:///events.db",
        )
    )

    with db.compat_connect() as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("INSERT INTO t (name) VALUES (?)", ("yang",))
        row = conn.execute("SELECT id, name FROM t WHERE name = ?", ("yang",)).fetchone()

    assert row is not None
    assert row[1] == "yang"
    assert row["name"] == "yang"
