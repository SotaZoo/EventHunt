"""``compute_friends_going`` allowlist semantics.

Spec §1 (friend-signal computation): only the four commitment statuses
(``registering``, ``applied``, ``pending_approval``, ``confirmed``) count
a friend as going. ``saved`` and ``interested`` signal curiosity, not
commitment — falsely telling the channel that a curious friend is going
would erode trust in the bot.

This file pins the allowlist boundary against a future "include
interested too" widening, and verifies the empty-list path that lets the
renderer omit the line entirely.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api.repositories._storage import (
    FRIEND_SIGNAL_STATUSES,
    USER_EVENT_STATUSES,
    compute_friends_going,
    connect,
    create_user,
    ensure_user_event_state,
    set_user_event_status,
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


def _new_event(conn, url: str) -> int:
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    start_at = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    cur = conn.execute(
        """INSERT INTO events
            (source, url, title, start_at, scoring_status, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("luma", url, "T", start_at, "scored", now_iso, now_iso),
    )
    return int(cur.lastrowid)


def _new_user(conn, *, email: str, display_name: str):
    return create_user(
        conn,
        email=email,
        display_name=display_name,
        invite_code=f"code-{email}",
    )


def test_no_user_event_states_returns_empty(conn) -> None:
    event_id = _new_event(conn, "https://lu.ma/empty")
    assert compute_friends_going(conn, event_id) == []


def test_registering_user_appears_in_result(conn) -> None:
    event_id = _new_event(conn, "https://lu.ma/r")
    user = _new_user(conn, email="alice@example.com", display_name="Alice")
    ensure_user_event_state(conn, user["id"], event_id, status="registering")
    assert compute_friends_going(conn, event_id) == ["Alice"]


def test_dismissed_status_is_excluded(conn) -> None:
    event_id = _new_event(conn, "https://lu.ma/d")
    user = _new_user(conn, email="bob@example.com", display_name="Bob")
    ensure_user_event_state(conn, user["id"], event_id, status="new")
    set_user_event_status(conn, user["id"], event_id, "dismissed")
    assert compute_friends_going(conn, event_id) == []


@pytest.mark.parametrize(
    "status",
    sorted(set(USER_EVENT_STATUSES) - set(FRIEND_SIGNAL_STATUSES)),
)
def test_non_allowlist_statuses_are_excluded(conn, status: str) -> None:
    """Every status NOT in the four-state allowlist must be filtered out.

    Parametrized so adding a fifth ``USER_EVENT_STATUSES`` value in the
    future automatically tests it here — the test won't quietly accept
    a "include saved too" widening of the allowlist.
    """
    event_id = _new_event(conn, f"https://lu.ma/{status}")
    user = _new_user(conn, email=f"{status}@example.com", display_name=status)
    ensure_user_event_state(conn, user["id"], event_id, status="new")
    set_user_event_status(conn, user["id"], event_id, status)
    assert compute_friends_going(conn, event_id) == []


def test_mixed_statuses_returns_only_allowlist(conn) -> None:
    event_id = _new_event(conn, "https://lu.ma/mix")
    going = _new_user(conn, email="going@example.com", display_name="Going")
    curious = _new_user(conn, email="curious@example.com", display_name="Curious")
    ensure_user_event_state(conn, going["id"], event_id, status="new")
    set_user_event_status(conn, going["id"], event_id, "applied")
    ensure_user_event_state(conn, curious["id"], event_id, status="new")
    set_user_event_status(conn, curious["id"], event_id, "saved")

    assert compute_friends_going(conn, event_id) == ["Going"]


def test_result_is_alphabetically_sorted(conn) -> None:
    """Deterministic order so renders are stable for golden tests."""
    event_id = _new_event(conn, "https://lu.ma/sort")
    for name, email in [
        ("Charlie", "c@example.com"),
        ("Alice", "a@example.com"),
        ("Bob", "b@example.com"),
    ]:
        u = _new_user(conn, email=email, display_name=name)
        ensure_user_event_state(conn, u["id"], event_id, status="new")
        set_user_event_status(conn, u["id"], event_id, "confirmed")
    assert compute_friends_going(conn, event_id) == ["Alice", "Bob", "Charlie"]


def test_inactive_user_is_excluded(conn) -> None:
    """Deactivated users (revoked invite) must not appear in the line.

    Belt-and-suspenders: the friend signal is a privacy-sensitive render;
    a former alpha user whose access was revoked should not keep showing
    up in #scout pushes.
    """
    event_id = _new_event(conn, "https://lu.ma/inactive")
    user = _new_user(conn, email="gone@example.com", display_name="Gone")
    ensure_user_event_state(conn, user["id"], event_id, status="new")
    set_user_event_status(conn, user["id"], event_id, "registering")
    conn.execute(
        "UPDATE users SET is_active = 0 WHERE id = ?", (user["id"],),
    )
    assert compute_friends_going(conn, event_id) == []
