from __future__ import annotations

import sqlite3
from pathlib import Path

from api.repositories.migrate import apply_pending

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"


M0_SCHEMA = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    starts_at TEXT,
    ends_at TEXT,
    venue TEXT,
    neighborhood TEXT,
    description TEXT,
    host TEXT,
    cover_image TEXT,
    ai_relevance INTEGER,
    category TEXT,
    tags_json TEXT DEFAULT '[]',
    summary TEXT,
    is_nyc INTEGER,
    classify_status TEXT NOT NULL DEFAULT 'unclassified',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX idx_events_starts ON events(starts_at);
CREATE INDEX idx_events_classify ON events(classify_status);
CREATE INDEX idx_events_relevance ON events(ai_relevance);
"""


def _seed_m0_db(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "events.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for stmt in M0_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    conn.execute(
        """INSERT INTO events
           (source, url, title, starts_at, classify_status, first_seen, last_seen)
           VALUES ('luma', 'https://lu.ma/x', 'Old Event', '2026-04-20T18:00:00+00:00',
                   'classified', '2026-04-19T00:00:00+00:00', '2026-04-19T00:00:00+00:00')"""
    )
    return conn


def test_migration_renames_starts_at(tmp_path: Path) -> None:
    conn = _seed_m0_db(tmp_path)
    apply_pending(conn, MIG_DIR)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    assert "start_at" in cols
    assert "starts_at" not in cols


def test_migration_drops_m0_classifier_columns(tmp_path: Path) -> None:
    conn = _seed_m0_db(tmp_path)
    apply_pending(conn, MIG_DIR)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    for dropped in {
        "ai_relevance", "classify_status", "tags_json", "is_nyc",
        "category", "summary", "cover_image",
    }:
        assert dropped not in cols, f"{dropped} should be dropped"


def test_migration_adds_v1_columns(tmp_path: Path) -> None:
    conn = _seed_m0_db(tmp_path)
    apply_pending(conn, MIG_DIR)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    for added in {
        "fit_input_hash", "fit_score", "fit_reasoning", "urgency",
        "urgency_reason", "scoring_status", "scoring_failed_reason",
        "prompt_version", "taste_sha", "posted_at",
    }:
        assert added in cols, f"{added} should be added"


def test_migration_backfills_posted_at_for_existing_rows(tmp_path: Path) -> None:
    """Pre-pivot rows must be marked posted so the channel doesn't flood."""
    conn = _seed_m0_db(tmp_path)
    apply_pending(conn, MIG_DIR)
    row = conn.execute(
        "SELECT posted_at FROM events WHERE url = 'https://lu.ma/x'"
    ).fetchone()
    assert row["posted_at"] is not None


def test_migration_preserves_existing_data(tmp_path: Path) -> None:
    conn = _seed_m0_db(tmp_path)
    apply_pending(conn, MIG_DIR)
    row = conn.execute(
        "SELECT title, source, start_at FROM events WHERE url='https://lu.ma/x'"
    ).fetchone()
    assert row["title"] == "Old Event"
    assert row["source"] == "luma"
    assert row["start_at"] == "2026-04-20T18:00:00+00:00"


def test_migration_indexes_present(tmp_path: Path) -> None:
    conn = _seed_m0_db(tmp_path)
    apply_pending(conn, MIG_DIR)
    idx = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
    ).fetchall()}
    assert "idx_events_posted_at" in idx
    assert "idx_events_scoring_status" in idx
    assert "idx_events_start_at" in idx
    assert "idx_events_fit_score" in idx
    # Old indexes on dropped columns should not survive
    assert "idx_events_classify" not in idx
    assert "idx_events_relevance" not in idx


def test_migration_idempotent(tmp_path: Path) -> None:
    conn = _seed_m0_db(tmp_path)
    apply_pending(conn, MIG_DIR)
    # Running again should be a no-op
    assert apply_pending(conn, MIG_DIR) == []


def test_check_constraint_fit_score_range(tmp_path: Path) -> None:
    conn = _seed_m0_db(tmp_path)
    apply_pending(conn, MIG_DIR)
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE events SET fit_score = 1.5 WHERE url = 'https://lu.ma/x'"
        )


def test_check_constraint_urgency_enum(tmp_path: Path) -> None:
    conn = _seed_m0_db(tmp_path)
    apply_pending(conn, MIG_DIR)
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE events SET urgency = 'extreme' WHERE id = 1")
