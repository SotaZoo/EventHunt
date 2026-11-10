"""Structured readiness checks for the private-alpha web app."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url
from starlette.requests import Request
from starlette.responses import JSONResponse

from api.configs.settings import (
    database_path,
    discord_mode,
    llm_scoring_mode,
    scraper_mode,
)
from api.repositories.migrate import migration_dir_for_dialect

LOGGER = logging.getLogger(__name__)

HEALTH_TABLES = (
    "schema_migrations",
    "events",
    "users",
    "user_event_states",
    "alpha_invites",
    "linked_identities",
)


def _mode_payload() -> dict[str, str]:
    return {
        "claude": llm_scoring_mode(),
        "discord": discord_mode(),
        "scraper": scraper_mode(),
    }


def _sqlite_health_path(request: Request) -> Path | None:
    if request.app.state.db_path is not None:
        return Path(request.app.state.db_path)
    database = request.app.state.database
    if database is None or database.settings.dialect != "sqlite":
        return None
    parsed = make_url(database.settings.url)
    return Path(parsed.database or str(database_path()))


def _health_connection(request: Request) -> Any:
    sqlite_path = _sqlite_health_path(request)
    if sqlite_path is None:
        return request.app.state.database.open_compat()
    if not sqlite_path.exists():
        raise FileNotFoundError(sqlite_path)
    uri = f"{sqlite_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _required_migrations(dialect: str) -> set[str]:
    return {path.name for path in migration_dir_for_dialect(dialect).glob("*.sql")}


def _applied_migrations(conn: Any) -> set[str]:
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {row[0] if isinstance(row, tuple) else row["filename"] for row in rows}


def _assert_required_tables(conn: Any) -> None:
    for table in HEALTH_TABLES:
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()


async def health_response(request: Request) -> JSONResponse:
    mode = _mode_payload()
    conn = None
    try:
        conn = _health_connection(request)
        _assert_required_tables(conn)
        dialect = getattr(conn, "dialect", "sqlite")
        required = _required_migrations(dialect)
        applied = _applied_migrations(conn)
    except Exception as exc:
        LOGGER.exception("health check failed")
        return JSONResponse(
            {
                "status": "degraded",
                "db": "error",
                "error": exc.__class__.__name__,
                "mode": mode,
            },
            status_code=503,
        )
    finally:
        if conn is not None:
            conn.close()

    missing = sorted(required - applied)
    unknown_applied = sorted(applied - required)
    if missing or unknown_applied:
        return JSONResponse(
            {
                "status": "degraded",
                "db": "ok",
                "migrations": "gaps",
                "missing": missing,
                "unknown_applied": unknown_applied,
                "mode": mode,
            },
            status_code=503,
        )

    return JSONResponse(
        {
            "status": "ok",
            "db": "ok",
            "migrations": "current",
            "mode": mode,
        }
    )
