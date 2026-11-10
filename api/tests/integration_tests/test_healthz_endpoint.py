from __future__ import annotations

import sqlite3
from pathlib import Path

from starlette.testclient import TestClient

from api.app import create_app
from api.repositories.migrate import migration_dir_for_dialect
from api.tests.fixtures.web_helpers import migrated_db


def test_healthz_returns_json_for_current_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_OAUTH_MODE", "mock")
    monkeypatch.setenv("DISCORD_MODE", "stub")
    monkeypatch.setenv("SCRAPER_MODE", "fixture")
    app = create_app(db_path=migrated_db(tmp_path), session_secret="test-secret")
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "status": "ok",
        "db": "ok",
        "migrations": "current",
        "mode": {
            "claude": "mock",
            "discord": "stub",
            "scraper": "fixture",
        },
    }


def test_healthz_returns_degraded_when_db_is_unreachable(tmp_path: Path) -> None:
    app = create_app(db_path=tmp_path / "missing.db", session_secret="test-secret")
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"] == "error"
    assert body["error"] == "FileNotFoundError"


def test_healthz_does_not_create_missing_sqlite_file_or_parent(tmp_path: Path) -> None:
    db = tmp_path / "missing-parent" / "events.db"
    app = create_app(db_path=db, session_secret="test-secret")
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["db"] == "error"
    assert not db.exists()
    assert not db.parent.exists()


def test_healthz_reports_missing_migrations(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    latest = sorted(path.name for path in migration_dir_for_dialect("sqlite").glob("*.sql"))[-1]
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM schema_migrations WHERE filename = ?", (latest,))
        conn.commit()
    finally:
        conn.close()
    app = create_app(db_path=db, session_secret="test-secret")
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"] == "ok"
    assert body["migrations"] == "gaps"
    assert body["missing"] == [latest]
    assert body["unknown_applied"] == []


def test_healthz_reports_unknown_applied_migrations(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO schema_migrations (filename) VALUES (?)",
            ("9999_unknown.sql",),
        )
        conn.commit()
    finally:
        conn.close()
    app = create_app(db_path=db, session_secret="test-secret")
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"] == "ok"
    assert body["migrations"] == "gaps"
    assert body["missing"] == []
    assert body["unknown_applied"] == ["9999_unknown.sql"]

