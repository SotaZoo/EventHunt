from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from api.repositories.database import open_database

TABLES = [
    "events",
    "users",
    "linked_identities",
    "alpha_invites",
    "user_event_states",
]
BOOLEAN_COLUMNS = {
    "users": {"is_active"},
    "linked_identities": {"email_verified"},
}


def _table_count(conn, table: str) -> int:  # type: ignore[no-untyped-def]
    return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def _insert_rows(conn, table: str, rows: list[dict[str, Any]]) -> None:  # type: ignore[no-untyped-def]
    if not rows:
        return
    rows = _coerce_rows_for_table(table, rows)
    columns = list(rows[0])
    column_sql = ", ".join(columns)
    value_sql = ", ".join(f":{column}" for column in columns)
    stmt = text(f"INSERT INTO {table} ({column_sql}) VALUES ({value_sql})")
    conn.execute(stmt, rows)


def _coerce_rows_for_table(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bool_columns = BOOLEAN_COLUMNS.get(table)
    if not bool_columns:
        return rows
    coerced = []
    for row in rows:
        next_row = dict(row)
        for column in bool_columns:
            if column in next_row and next_row[column] is not None:
                next_row[column] = bool(next_row[column])
        coerced.append(next_row)
    return coerced


def _reset_postgres_sequences(conn) -> None:  # type: ignore[no-untyped-def]
    for table in ("events", "users", "alpha_invites"):
        conn.execute(
            text(
                f"""SELECT setval(
                    pg_get_serial_sequence('{table}', 'id'),
                    COALESCE((SELECT max(id) FROM {table}), 1),
                    (SELECT count(*) FROM {table}) > 0
                )"""
            )
        )


def import_payload(path: Path, *, force_empty_target: bool = False) -> None:
    payload = json.loads(path.read_text())
    tables: dict[str, list[dict[str, Any]]] = payload["tables"]
    db = open_database()
    with db.begin() as conn:
        non_empty = [table for table in TABLES if _table_count(conn, table) > 0]
        if non_empty and not force_empty_target:
            raise RuntimeError(
                "target database is not empty: "
                + ", ".join(non_empty)
                + ". Import expects an empty DB."
            )
        for table in TABLES:
            _insert_rows(conn, table, tables.get(table, []))
        if db.dialect == "postgres":
            _reset_postgres_sequences(conn)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="input", type=Path, required=True)
    parser.add_argument("--force-empty-target", action="store_true")
    args = parser.parse_args()
    import_payload(args.input, force_empty_target=args.force_empty_target)
    print(f"imported {args.input}")


if __name__ == "__main__":
    main()
