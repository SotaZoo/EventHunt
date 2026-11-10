"""Numbered SQL migration runner.

Applies files in `db/migrate/sqlite/*.sql` lexicographically, records each in
`schema_migrations`, runs each file in a single transaction so a syntax
error rolls back the whole file (no partial-state leaks).

Note: uses individual conn.execute() calls rather than executescript() because
Python 3.11's executescript() issues an implicit COMMIT before running, which
breaks the outer BEGIN/ROLLBACK transaction management.
"""
from __future__ import annotations

import sqlite3
from contextlib import suppress
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection


class MigrationError(RuntimeError):
    pass


def migration_dir_for_dialect(dialect: str, repo_root: Path | None = None) -> Path:
    if dialect == "postgres":
        relative = Path("db") / "migrate" / "postgres"
    elif dialect == "sqlite":
        relative = Path("db") / "migrate" / "sqlite"
    else:
        raise ValueError(f"unsupported migration dialect: {dialect}")

    if repo_root is not None:
        return repo_root / relative

    # Installed wheels carry runtime SQL under api/_runtime so production
    # deploys do not depend on a source checkout being present.
    api_root = Path(__file__).resolve().parents[1]
    packaged = api_root / "_runtime" / relative
    if packaged.exists():
        return packaged

    # api/repositories/migrate.py -> repo root is parents[2].
    return api_root.parent / relative


def get_migration_dirs(repo_root: Path) -> dict[str, Path]:
    return {
        "sqlite": repo_root / "db" / "migrate" / "sqlite",
        "postgres": repo_root / "db" / "migrate" / "postgres",
    }


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )


def applied_migrations(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT filename FROM schema_migrations ORDER BY filename"
    ).fetchall()
    return [r[0] if isinstance(r, tuple) else r["filename"] for r in rows]


def _iter_statements(sql: str):
    """Yield non-empty SQL statements, skipping BEGIN/COMMIT/ROLLBACK tokens.

    The runner manages the transaction; migration files may include those tokens
    for documentation but they're stripped here. Line comments are stripped from
    the full SQL before splitting on ; so semicolons inside comments don't fool
    the parser.
    """
    # Strip line comments from the full text first (before splitting on ;)
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    stripped_sql = "\n".join(lines)
    for stmt in stripped_sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        first_word = stmt.split()[0].upper() if stmt.split() else ""
        if first_word in ("BEGIN", "COMMIT", "ROLLBACK"):
            continue
        yield stmt


def apply_pending(conn: sqlite3.Connection, mig_dir: Path) -> list[str]:
    """Apply every *.sql in mig_dir not yet recorded. Return filenames applied."""
    ensure_table(conn)
    done = set(applied_migrations(conn))
    pending = sorted(p for p in mig_dir.glob("*.sql") if p.name not in done)
    out: list[str] = []
    for path in pending:
        sql = path.read_text()
        try:
            conn.execute("BEGIN")
            for stmt in _iter_statements(sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)",
                (path.name,),
            )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            with suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise MigrationError(f"{path.name} failed: {exc}") from exc
        out.append(path.name)
    return out


def ensure_table_sqlalchemy(conn: Connection) -> None:
    conn.execute(
        text(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
    )


def applied_migrations_sqlalchemy(conn: Connection) -> list[str]:
    rows = conn.execute(
        text("SELECT filename FROM schema_migrations ORDER BY filename")
    ).mappings().all()
    return [row["filename"] for row in rows]


def apply_pending_sqlalchemy(conn: Connection, mig_dir: Path) -> list[str]:
    """Apply every *.sql in mig_dir through a SQLAlchemy transaction."""
    ensure_table_sqlalchemy(conn)
    done = set(applied_migrations_sqlalchemy(conn))
    pending = sorted(p for p in mig_dir.glob("*.sql") if p.name not in done)
    out: list[str] = []
    for path in pending:
        sql = path.read_text()
        try:
            for stmt in _iter_statements(sql):
                conn.execute(text(stmt))
            conn.execute(
                text("INSERT INTO schema_migrations (filename) VALUES (:filename)"),
                {"filename": path.name},
            )
        except Exception as exc:
            raise MigrationError(f"{path.name} failed: {exc}") from exc
        out.append(path.name)
    return out
