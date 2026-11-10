from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from api.models.events import RawEvent
from api.repositories.events import connect, update_fit, upsert_raw
from api.repositories.migrate import apply_pending
from api.repositories.users import create_user

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"


def migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "events.db"
    conn = connect(db)
    apply_pending(conn, MIG_DIR)
    conn.close()
    return db


def add_user(
    db: Path,
    *,
    email: str = "yang@example.com",
    display_name: str = "Yang",
    invite_code: str = "yang-secret",
) -> sqlite3.Row:
    conn = connect(db)
    try:
        return create_user(
            conn,
            email=email,
            display_name=display_name,
            invite_code=invite_code,
            interest_flags={"ai": True},
        )
    finally:
        conn.close()


def add_scored_event(
    db: Path,
    *,
    url: str | None = None,
    title: str = "AI Infra Night",
    score: float = 0.91,
    source: str = "luma",
    days_from_now: int = 3,
    starts_at: datetime | None = None,
    reasoning: str = "Strong match for infra founders",
    venue: str | None = "Cerebral Valley",
) -> int:
    conn = connect(db)
    try:
        starts_at = starts_at or datetime.now(UTC) + timedelta(days=days_from_now)
        event_url = url or (
            f"https://example.test/{source}/"
            f"{title.lower().replace(' ', '-')}-{days_from_now}-{round(score * 100)}"
        )
        raw = RawEvent(
            source=source,
            url=event_url,
            title=title,
            starts_at=starts_at,
            venue=venue,
            neighborhood=None,
            description=None,
            host="AI Builders",
            cover_image=None,
        )
        event_id = upsert_raw(conn, raw)
        update_fit(
            conn,
            event_id,
            fit_score=score,
            reasoning=reasoning,
            urgency="medium",
            urgency_reason=None,
            prompt_version="v1",
            taste_sha="taste",
        )
        return event_id
    finally:
        conn.close()
