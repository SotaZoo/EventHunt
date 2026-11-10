from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from api.models.events import RawEvent
from api.repositories.events import (
    compute_fit_input_hash,
    connect,
    count_requeueable_scoring_failures,
    fetch_pending_scoring,
    mark_event_cancelled,
    mark_event_retracted,
    mark_posted,
    mark_scoring_failed,
    requeue_scoring_failures,
    update_fit,
    upsert_raw,
)
from api.repositories.migrate import apply_pending

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Migrated, empty DB."""
    db = tmp_path / "events.db"
    c = connect(db)
    apply_pending(c, MIG_DIR)
    return c


def _raw(url: str = "https://lu.ma/a") -> RawEvent:
    return RawEvent(
        source="luma",
        url=url,
        title="AI infra meetup",
        starts_at=datetime(2099, 5, 1, 18, 0, tzinfo=UTC),
        venue="Cerebral Valley",
        neighborhood=None,
        description=None,
        host=None,
        cover_image=None,
    )


def test_connect_enables_wal(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_connect_sets_foreign_keys(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_upsert_raw_inserts(conn: sqlite3.Connection) -> None:
    rid = upsert_raw(conn, _raw())
    row = conn.execute("SELECT * FROM events WHERE id = ?", (rid,)).fetchone()
    assert row["title"] == "AI infra meetup"
    assert row["scoring_status"] == "pending"
    assert row["fit_input_hash"] is not None


def test_upsert_raw_updates_existing(conn: sqlite3.Connection) -> None:
    rid = upsert_raw(conn, _raw())
    r2 = _raw()
    r2.title = "AI infra meetup (v2)"
    rid2 = upsert_raw(conn, r2)
    assert rid == rid2
    row = conn.execute("SELECT title FROM events WHERE id = ?", (rid,)).fetchone()
    assert row["title"] == "AI infra meetup (v2)"


def test_upsert_raw_resets_scoring_when_hash_changes(conn: sqlite3.Connection) -> None:
    rid = upsert_raw(conn, _raw())
    update_fit(
        conn, rid, fit_score=0.9, reasoning="match", urgency="low",
        urgency_reason=None, prompt_version="v2", taste_sha="abc123",
        notable_people=[
            {"name": "Jensen Huang", "role_clause": "NVIDIA CEO", "confidence": 0.96}
        ],
        event_type="talk",
        food_provided="yes",
    )
    row = conn.execute(
        """SELECT scoring_status, fit_score, notable_people, event_type, food_provided
           FROM events WHERE id=?""",
        (rid,),
    ).fetchone()
    assert row["scoring_status"] == "scored"
    assert row["event_type"] == "talk"
    assert row["food_provided"] == "yes"
    assert "Jensen" in row["notable_people"]

    # Mutate venue → hash changes → scoring AND extraction reset to NULL.
    r2 = _raw()
    r2.venue = "Different Place"
    upsert_raw(conn, r2)
    row = conn.execute(
        """SELECT scoring_status, fit_score, prompt_version, taste_sha,
                  notable_people, event_type, food_provided
           FROM events WHERE id=?""",
        (rid,),
    ).fetchone()
    assert row["scoring_status"] == "pending"
    assert row["fit_score"] is None
    assert row["prompt_version"] is None
    assert row["taste_sha"] is None
    assert row["notable_people"] is None
    assert row["event_type"] is None
    assert row["food_provided"] is None


def test_update_fit_stores_v2_extraction_fields(conn: sqlite3.Connection) -> None:
    """Persistence round-trip: notable_people stored as JSON text,
    event_type / food_provided as plain enum text. Empty notable_people
    is stored as NULL so SELECTs can filter cheaply."""
    import json

    rid = upsert_raw(conn, _raw())
    update_fit(
        conn, rid, fit_score=0.9, reasoning="match", urgency="low",
        urgency_reason=None, prompt_version="v2", taste_sha="abc",
        notable_people=[
            {"name": "A", "role_clause": "founder", "confidence": 0.95},
            {"name": "B", "role_clause": "panelist", "confidence": 0.60},
        ],
        event_type="workshop",
        food_provided="no",
    )
    row = conn.execute(
        """SELECT notable_people, event_type, food_provided
           FROM events WHERE id=?""",
        (rid,),
    ).fetchone()
    decoded = json.loads(row["notable_people"])
    assert [p["name"] for p in decoded] == ["A", "B"]
    # Low-confidence entries are kept at the storage layer; render-time filter.
    assert decoded[1]["confidence"] == 0.60
    assert row["event_type"] == "workshop"
    assert row["food_provided"] == "no"


def test_update_fit_treats_empty_notable_people_as_null(
    conn: sqlite3.Connection,
) -> None:
    rid = upsert_raw(conn, _raw())
    update_fit(
        conn, rid, fit_score=0.9, reasoning="match", urgency="low",
        urgency_reason=None, prompt_version="v2", taste_sha="abc",
        notable_people=[],
        event_type="other",
        food_provided="unknown",
    )
    row = conn.execute(
        "SELECT notable_people FROM events WHERE id=?", (rid,),
    ).fetchone()
    assert row["notable_people"] is None


def test_update_fit_backward_compatible_without_v2_kwargs(
    conn: sqlite3.Connection,
) -> None:
    """Existing call sites (v1 cycle, tests, web helpers) don't pass the
    new kwargs. They must keep working; the extraction columns default
    to NULL so the renderer skips those lines."""
    rid = upsert_raw(conn, _raw())
    update_fit(
        conn, rid, fit_score=0.9, reasoning="match", urgency="low",
        urgency_reason=None, prompt_version="v1", taste_sha="abc",
    )
    row = conn.execute(
        "SELECT notable_people, event_type, food_provided FROM events WHERE id=?",
        (rid,),
    ).fetchone()
    assert row["notable_people"] is None
    assert row["event_type"] is None
    assert row["food_provided"] is None


def test_compute_fit_input_hash_stable(conn: sqlite3.Connection) -> None:
    h1 = compute_fit_input_hash("Title", "2026-05-01T18:00:00+00:00", "Venue")
    h2 = compute_fit_input_hash("Title", "2026-05-01T18:00:00+00:00", "Venue")
    assert h1 == h2
    h3 = compute_fit_input_hash("Title", "2026-05-01T18:00:00+00:00", "Other")
    assert h3 != h1


def test_fetch_pending_scoring_orders_newest_first(conn: sqlite3.Connection) -> None:
    rid_a = upsert_raw(conn, _raw("https://lu.ma/a"))
    rid_b = upsert_raw(conn, _raw("https://lu.ma/b"))
    rows = fetch_pending_scoring(conn, limit=10)
    assert {r["id"] for r in rows} == {rid_a, rid_b}
    # newest first_seen first
    assert rows[0]["id"] == rid_b


def test_mark_posted_sets_posted_at(conn: sqlite3.Connection) -> None:
    rid = upsert_raw(conn, _raw())
    mark_posted(conn, rid)
    row = conn.execute("SELECT posted_at FROM events WHERE id=?", (rid,)).fetchone()
    assert row["posted_at"] is not None


def test_mark_scoring_failed(conn: sqlite3.Connection) -> None:
    rid = upsert_raw(conn, _raw())
    mark_scoring_failed(conn, rid, "timeout after 60s")
    row = conn.execute(
        "SELECT scoring_status, scoring_failed_reason FROM events WHERE id=?",
        (rid,)
    ).fetchone()
    assert row["scoring_status"] == "failed"
    assert row["scoring_failed_reason"] == "timeout after 60s"


def test_requeue_scoring_failures_resets_only_safe_future_rows(
    conn: sqlite3.Connection,
) -> None:
    def insert_failed(name: str, *, start_at: datetime) -> int:
        raw = _raw(f"https://lu.ma/{name}")
        raw.starts_at = start_at
        rid = upsert_raw(conn, raw)
        update_fit(
            conn, rid, fit_score=0.7, reasoning="old", urgency="low",
            urgency_reason=None, prompt_version="v2", taste_sha="abc",
            notable_people=[
                {"name": "A", "role_clause": "speaker", "confidence": 0.95}
            ],
            event_type="talk",
            food_provided="yes",
        )
        mark_scoring_failed(conn, rid, "CliFallbackError")
        return rid

    now = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    future = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)
    past = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    safe = insert_failed("safe", start_at=future)
    past_row = insert_failed("past", start_at=past)
    posted_msg = insert_failed("posted-msg", start_at=future)
    posted_legacy = insert_failed("posted-legacy", start_at=future)
    claimed = insert_failed("claimed", start_at=future)
    wrong_reason = insert_failed("wrong-reason", start_at=future)
    retracted = insert_failed("retracted", start_at=future)
    cancelled = insert_failed("cancelled", start_at=future)

    conn.execute(
        "UPDATE events SET discord_push_message_id = ? WHERE id = ?",
        ("msg_1", posted_msg),
    )
    mark_posted(conn, posted_legacy)
    conn.execute(
        "UPDATE events SET posting_claimed_at = ?, posting_claim_token = ? WHERE id = ?",
        (now.isoformat(timespec="seconds"), "token", claimed),
    )
    mark_scoring_failed(conn, wrong_reason, "OtherError")
    mark_event_retracted(conn, retracted)
    mark_event_cancelled(conn, cancelled)

    count = count_requeueable_scoring_failures(
        conn,
        reason="CliFallbackError",
        now_iso=now.isoformat(timespec="seconds"),
    )
    assert count == 1

    requeued = requeue_scoring_failures(
        conn,
        reason="CliFallbackError",
        now_iso=now.isoformat(timespec="seconds"),
    )
    assert requeued == 1

    rows = {
        row["id"]: row
        for row in conn.execute(
            """SELECT id, scoring_status, scoring_failed_reason, fit_score,
                      prompt_version, notable_people, event_type, food_provided
                 FROM events"""
        ).fetchall()
    }
    assert rows[safe]["scoring_status"] == "pending"
    assert rows[safe]["scoring_failed_reason"] is None
    assert rows[safe]["fit_score"] is None
    assert rows[safe]["prompt_version"] is None
    assert rows[safe]["notable_people"] is None
    assert rows[safe]["event_type"] is None
    assert rows[safe]["food_provided"] is None

    for event_id in (
        past_row,
        posted_msg,
        posted_legacy,
        claimed,
        wrong_reason,
        retracted,
        cancelled,
    ):
        assert rows[event_id]["scoring_status"] == "failed"
