"""Calibration tests for ``fetch_top_unpushed_future_event``.

Spec §1: the scout-bot only pushes events whose ``fit_score`` clears the
``high_bar_threshold`` (default 0.85). This file pins that boundary by
constructing ten synthetic events with varied fit_scores and asserting
the top picked is the highest-above-bar (and that ``None`` comes back
when nothing crosses). Catches a future "drop the >= comparison to >"
regression that would silently quiet the channel.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api.repositories.events import (
    connect,
    fetch_top_unpushed_future_event,
)
from api.repositories.migrate import apply_pending

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"


def _insert_scored_event(
    conn,
    *,
    url: str,
    fit_score: float,
    start_at: datetime | None = None,
    discord_push_message_id: str | None = None,
    cancelled_at: str | None = None,
    scoring_status: str = "scored",
) -> int:
    """Insert a synthetic event row, returning its id.

    Bypasses ``upsert_raw`` / ``update_fit`` so each test row sets exactly
    the columns it cares about — the scan SQL filters on many fields, and
    we want each filter to be tested in isolation.
    """
    if start_at is None:
        start_at = datetime.now(UTC) + timedelta(days=3)
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    cur = conn.execute(
        """INSERT INTO events
            (source, url, title, start_at, fit_score, scoring_status,
             first_seen, last_seen, discord_push_message_id, cancelled_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "luma", url, f"Event {url}",
            start_at.isoformat(),
            fit_score,
            scoring_status,
            now_iso, now_iso,
            discord_push_message_id, cancelled_at,
        ),
    )
    return int(cur.lastrowid)


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "events.db"
    c = connect(db)
    apply_pending(c, MIG_DIR)
    yield c
    c.close()


def test_returns_highest_above_high_bar(conn) -> None:
    """Ten events spanning 0.10..0.99; top-1 above 0.85 is the winner."""
    scores = [0.10, 0.22, 0.40, 0.55, 0.68, 0.79, 0.84, 0.86, 0.93, 0.99]
    for i, s in enumerate(scores):
        _insert_scored_event(conn, url=f"https://lu.ma/e{i}", fit_score=s)
    row = fetch_top_unpushed_future_event(conn, high_bar_threshold=0.85)
    assert row is not None
    assert row["fit_score"] == 0.99


def test_returns_none_when_no_event_crosses_bar(conn) -> None:
    """All events strictly below 0.85 → no push; channel stays quiet."""
    for i, s in enumerate([0.10, 0.40, 0.60, 0.80, 0.849]):
        _insert_scored_event(conn, url=f"https://lu.ma/q{i}", fit_score=s)
    assert fetch_top_unpushed_future_event(conn, high_bar_threshold=0.85) is None


def test_boundary_includes_equal_to_threshold(conn) -> None:
    """Exactly-0.85 must qualify; the SQL uses ``>=``, not ``>``."""
    _insert_scored_event(conn, url="https://lu.ma/edge", fit_score=0.85)
    row = fetch_top_unpushed_future_event(conn, high_bar_threshold=0.85)
    assert row is not None
    assert row["fit_score"] == 0.85


def test_excludes_already_pushed_event(conn) -> None:
    """``discord_push_message_id IS NOT NULL`` → already pushed, skip."""
    _insert_scored_event(
        conn,
        url="https://lu.ma/pushed",
        fit_score=0.95,
        discord_push_message_id="msg_1",
    )
    _insert_scored_event(conn, url="https://lu.ma/fresh", fit_score=0.90)
    row = fetch_top_unpushed_future_event(conn, high_bar_threshold=0.85)
    assert row is not None
    assert row["url"] == "https://lu.ma/fresh"


def test_excludes_cancelled_event(conn) -> None:
    """``cancelled_at IS NOT NULL`` → defensive skip even if score is high."""
    _insert_scored_event(
        conn,
        url="https://lu.ma/cancelled",
        fit_score=0.99,
        cancelled_at="2026-05-01T00:00:00+00:00",
    )
    _insert_scored_event(conn, url="https://lu.ma/active", fit_score=0.86)
    row = fetch_top_unpushed_future_event(conn, high_bar_threshold=0.85)
    assert row is not None
    assert row["url"] == "https://lu.ma/active"


def test_excludes_past_event(conn) -> None:
    """``start_at`` must be in the future (UTC compare)."""
    past = datetime.now(UTC) - timedelta(days=1)
    _insert_scored_event(
        conn, url="https://lu.ma/past", fit_score=0.99, start_at=past,
    )
    _insert_scored_event(conn, url="https://lu.ma/future", fit_score=0.86)
    row = fetch_top_unpushed_future_event(conn, high_bar_threshold=0.85)
    assert row is not None
    assert row["url"] == "https://lu.ma/future"


def test_excludes_unscored_event(conn) -> None:
    """``scoring_status != 'scored'`` → not ready to push."""
    _insert_scored_event(
        conn,
        url="https://lu.ma/pending",
        fit_score=0.99,
        scoring_status="pending",
    )
    _insert_scored_event(conn, url="https://lu.ma/done", fit_score=0.88)
    row = fetch_top_unpushed_future_event(conn, high_bar_threshold=0.85)
    assert row is not None
    assert row["url"] == "https://lu.ma/done"


def test_past_today_event_with_iso_offset_is_not_future(conn) -> None:
    """Past-today ISO-with-offset string must not pass the future filter.

    Regression for the silent ``start_at > now()`` bug: SQLite stores
    Luma's ``start_at`` as ``'YYYY-MM-DDTHH:MM:SS+00:00'`` (ISO with the
    'T' separator) and compares against ``datetime('now')`` which returns
    ``'YYYY-MM-DD HH:MM:SS'`` (space-separated). Raw string compare puts
    'T' (84) > ' ' (32), so an event at 10am today scans as "future" at
    2pm — a high-scoring past-today event would slip through and post to
    ``#scout`` as "still future".

    The fix in ``_future_start_at_sql`` wraps with ``datetime()`` so both
    sides normalize to the same shape. Reverting that wrapper makes this
    test fail.
    """
    # Past-today event: 4 hours ago, high score that WOULD cross the bar.
    past_today = datetime.now(UTC) - timedelta(hours=4)
    _insert_scored_event(
        conn,
        url="https://lu.ma/past-today",
        fit_score=0.95,
        start_at=past_today,
    )
    row = fetch_top_unpushed_future_event(conn, high_bar_threshold=0.85)
    assert row is None, (
        "scout scan picked up a past-today event; the start_at compare "
        "is back to raw string compare (regression of _future_start_at_sql)"
    )
