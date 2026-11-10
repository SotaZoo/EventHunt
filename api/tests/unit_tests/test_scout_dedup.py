"""Deduplication regression: once pushed, never re-scanned.

Spec §1: the scan filters ``discord_push_message_id IS NULL``. Setting
that column (via ``set_discord_push_message_id``) must make the row
invisible to subsequent scans, otherwise the scout-bot would re-push
every cycle until the event's ``start_at`` slips into the past — visible
to the channel as a spam loop.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api.repositories.events import (
    connect,
    fetch_top_unpushed_future_event,
    set_discord_push_message_id,
)
from api.repositories.migrate import apply_pending

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "events.db"
    c = connect(db)
    apply_pending(c, MIG_DIR)
    yield c
    c.close()


def _insert(conn, *, url: str, fit_score: float) -> int:
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    start_at = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    cur = conn.execute(
        """INSERT INTO events
            (source, url, title, start_at, fit_score, scoring_status,
             first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("luma", url, "T", start_at, fit_score, "scored", now_iso, now_iso),
    )
    return int(cur.lastrowid)


def test_event_disappears_from_scan_once_message_id_is_set(conn) -> None:
    event_id = _insert(conn, url="https://lu.ma/once", fit_score=0.95)

    # First scan: row is unpushed → it's the top pick.
    row = fetch_top_unpushed_future_event(conn, high_bar_threshold=0.85)
    assert row is not None
    assert row["id"] == event_id
    assert row["discord_push_message_id"] is None

    # Simulate the post-Discord write that records the message id.
    set_discord_push_message_id(conn, event_id, "1234567890")

    # Second scan: same DB, same threshold — must return None now.
    assert fetch_top_unpushed_future_event(conn, high_bar_threshold=0.85) is None


def test_pushed_event_does_not_block_next_unpushed_one(conn) -> None:
    """Two events; pushing one shouldn't hide the other."""
    pushed_id = _insert(conn, url="https://lu.ma/a", fit_score=0.99)
    other_id = _insert(conn, url="https://lu.ma/b", fit_score=0.90)

    # Push the higher-scored one.
    set_discord_push_message_id(conn, pushed_id, "msg_high")

    row = fetch_top_unpushed_future_event(conn, high_bar_threshold=0.85)
    assert row is not None
    assert row["id"] == other_id
