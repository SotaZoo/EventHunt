from __future__ import annotations

import sqlite3

from starlette.testclient import TestClient

from api.app import create_app
from api.tests.fixtures.web_helpers import migrated_db


def test_healthz_returns_ok_for_migrated_database(tmp_path) -> None:
    app = create_app(db_path=migrated_db(tmp_path), session_secret="test-secret")
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["db"] == "ok"
    assert response.json()["migrations"] == "current"


def test_healthz_returns_unavailable_for_unmigrated_database(tmp_path) -> None:
    app = create_app(db_path=tmp_path / "missing.db", session_secret="test-secret")
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["db"] == "error"


def test_healthz_does_not_create_missing_sqlite_file_or_parent(tmp_path) -> None:
    db = tmp_path / "missing-parent" / "events.db"
    app = create_app(db_path=db, session_secret="test-secret")
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["db"] == "error"
    assert not db.exists()
    assert not db.parent.exists()


def test_healthz_requires_private_alpha_tables(tmp_path) -> None:
    db = tmp_path / "partial.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    finally:
        conn.close()
    app = create_app(db_path=db, session_secret="test-secret")
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_healthz_requires_latest_sqlite_migration(tmp_path) -> None:
    db = migrated_db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "DELETE FROM schema_migrations WHERE filename = ?",
            ("0006_scout_bot_mvp.sql",),
        )
        conn.commit()
    finally:
        conn.close()
    app = create_app(db_path=db, session_secret="test-secret")
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["migrations"] == "gaps"
