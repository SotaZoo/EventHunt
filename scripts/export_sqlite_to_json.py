from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

TABLES = [
    "events",
    "users",
    "linked_identities",
    "alpha_invites",
    "user_event_states",
]


def export_sqlite(db_path: Path) -> dict[str, list[dict[str, object]]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        payload: dict[str, list[dict[str, object]]] = {}
        for table in TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                payload[table] = []
                continue
            payload[table] = [
                dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            ]
        return payload
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/events.db"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"SQLite database not found: {args.db}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    backup = args.db.with_suffix(
        f".backup-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}{args.db.suffix}"
    )
    shutil.copy2(args.db, backup)
    payload = {
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_db": str(args.db),
        "backup_db": str(backup),
        "tables": export_sqlite(args.db),
    }
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"exported {args.db} -> {args.out}")
    print(f"backup: {backup}")


if __name__ == "__main__":
    main()
