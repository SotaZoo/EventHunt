"""Tests for the spec §11 telemetry helper ``write_scout_metric``.

Verifies the contract that each event_kind round-trips cleanly into the
``scout_metrics`` append-only table, that invalid kinds raise loudly,
and that an IO failure in the helper does NOT propagate into callers
(telemetry must not break the cycle, bot, or capacity poller).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from api.repositories._storage import (
    SCOUT_METRIC_KINDS,
    connect,
    write_scout_metric,
)
from api.repositories.migrate import apply_pending

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "scout_metrics.db"
    c = connect(db_path)
    apply_pending(c, MIG_DIR)
    yield c
    c.close()


def _read_metrics(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT event_kind, event_id, payload FROM scout_metrics "
        "ORDER BY id ASC"
    ).fetchall()
    return [
        {"event_kind": r["event_kind"], "event_id": r["event_id"],
         "payload": json.loads(r["payload"]) if r["payload"] else None}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Helper contract: all 7 spec enum values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(SCOUT_METRIC_KINDS))
def test_each_valid_event_kind_round_trips(conn: sqlite3.Connection, kind: str) -> None:
    write_scout_metric(conn, event_kind=kind, payload={"k": 1})
    rows = _read_metrics(conn)
    assert len(rows) == 1
    assert rows[0]["event_kind"] == kind
    assert rows[0]["payload"] == {"k": 1}


def test_unknown_event_kind_raises_value_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="unknown scout_metric event_kind"):
        write_scout_metric(conn, event_kind="not_a_real_kind")
    # And the table stays empty — no partial write.
    assert _read_metrics(conn) == []


# ---------------------------------------------------------------------------
# Payload semantics
# ---------------------------------------------------------------------------


def _insert_stub_event(conn: sqlite3.Connection, event_id: int) -> None:
    """Seed a minimal events row so FK-bound metric writes pass."""
    conn.execute(
        """INSERT INTO events (id, source, url, title)
           VALUES (?, 'stub', ?, 'stub')""",
        (event_id, f"https://example.com/{event_id}"),
    )


def test_payload_is_json_encoded_compact(conn: sqlite3.Connection) -> None:
    _insert_stub_event(conn, 42)
    write_scout_metric(
        conn,
        event_kind="push_attempt",
        event_id=42,
        payload={"status": "ok", "message_id_captured": True},
    )
    raw = conn.execute(
        "SELECT payload FROM scout_metrics ORDER BY id DESC LIMIT 1"
    ).fetchone()["payload"]
    # Compact separators (no whitespace).
    assert raw == '{"status":"ok","message_id_captured":true}'


def test_none_payload_stored_as_null(conn: sqlite3.Connection) -> None:
    write_scout_metric(conn, event_kind="tick_scan")
    raw = conn.execute(
        "SELECT payload FROM scout_metrics ORDER BY id DESC LIMIT 1"
    ).fetchone()["payload"]
    assert raw is None


def test_event_id_optional_and_stored(conn: sqlite3.Connection) -> None:
    _insert_stub_event(conn, 99)
    write_scout_metric(conn, event_kind="capacity_poll")
    write_scout_metric(conn, event_kind="capacity_patch", event_id=99)
    rows = _read_metrics(conn)
    assert rows[0]["event_id"] is None
    assert rows[1]["event_id"] == 99


# ---------------------------------------------------------------------------
# Resilience: a write failure must not propagate into the caller
# ---------------------------------------------------------------------------


def test_write_failure_does_not_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """If sqlite raises during INSERT, the helper swallows and logs."""
    class _BadConn:
        def execute(self, *_a, **_kw):
            raise sqlite3.OperationalError("simulated failure")

    # Should not raise — helper has its own try/except.
    write_scout_metric(_BadConn(), event_kind="tick_scan", payload={"x": 1})


def test_event_kinds_enum_matches_migration_check_constraint(
    conn: sqlite3.Connection,
) -> None:
    """The Python set is the first defense; SQL CHECK is the second.

    This test attempts an INSERT with a kind from the Python set against
    the actual table — the SQL CHECK constraint must accept all of them.
    Catches drift between the Python frozenset and the migration enum.
    """
    for kind in SCOUT_METRIC_KINDS:
        # Bypass the helper to exercise the SQL CHECK directly.
        conn.execute(
            "INSERT INTO scout_metrics (event_kind) VALUES (?)", (kind,),
        )
    # All inserts succeeded; the CHECK constraint matches the Python set.
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM scout_metrics"
    ).fetchone()
    assert rows["n"] == len(SCOUT_METRIC_KINDS)
