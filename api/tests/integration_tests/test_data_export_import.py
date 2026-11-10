from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.models.events import RawEvent
from api.repositories.events import connect, update_fit, upsert_raw
from api.repositories.migrate import apply_pending
from api.repositories.users import create_user
from scripts.export_sqlite_to_json import export_sqlite
from scripts.import_json_to_postgres import _coerce_rows_for_table, import_payload

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"


def _migrated(path: Path) -> None:
    conn = connect(path)
    try:
        apply_pending(conn, MIG_DIR)
    finally:
        conn.close()


def test_export_sqlite_includes_dependency_order_tables(tmp_path: Path) -> None:
    db = tmp_path / "source.db"
    _migrated(db)
    conn = connect(db)
    try:
        event_id = upsert_raw(
            conn,
            RawEvent(source="luma", url="https://lu.ma/a", title="A"),
        )
        update_fit(
            conn,
            event_id,
            fit_score=0.9,
            reasoning="good",
            urgency="low",
            urgency_reason=None,
            prompt_version="v1",
            taste_sha="taste",
        )
        create_user(conn, email="yang@example.com", display_name="Yang", invite_code="secret")
    finally:
        conn.close()

    payload = export_sqlite(db)

    assert [*payload] == [
        "events",
        "users",
        "linked_identities",
        "alpha_invites",
        "user_event_states",
    ]
    assert payload["events"][0]["id"] == event_id
    assert payload["users"][0]["email"] == "yang@example.com"


def test_import_payload_requires_empty_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _migrated(source)
    _migrated(target)
    conn = connect(target)
    try:
        create_user(conn, email="existing@example.com", display_name="Existing", invite_code="secret")
    finally:
        conn.close()
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps({"tables": export_sqlite(source)}))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{target}")

    with pytest.raises(RuntimeError, match="target database is not empty"):
        import_payload(payload_path)


def test_postgres_import_coerces_sqlite_boolean_values() -> None:
    users = _coerce_rows_for_table(
        "users",
        [
            {
                "id": 1,
                "email": "active@example.com",
                "display_name": "Active",
                "is_active": 1,
            },
            {
                "id": 2,
                "email": "inactive@example.com",
                "display_name": "Inactive",
                "is_active": 0,
            },
        ],
    )
    identities = _coerce_rows_for_table(
        "linked_identities",
        [
            {
                "provider": "google",
                "subject": "abc",
                "user_id": 1,
                "email": "active@example.com",
                "email_verified": 1,
            }
        ],
    )

    assert users[0]["is_active"] is True
    assert users[1]["is_active"] is False
    assert identities[0]["email_verified"] is True
