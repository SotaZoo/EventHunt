from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from api.configs.settings import database_path
from api.models.event_annotations import EventHighlight, EventUrgencyNote, grounded_annotation
from api.models.events import RawEvent
from api.models.occurrences import provider_identity, utc_instant
from api.repositories.availability import record_ingested_availability
from api.repositories.catalog import catalog_rows, excluded_source_identities, visible_occurrences
from api.repositories.database import CompatConnection, create_database, open_database

DB_PATH = Path("data/events.db")
USER_EVENT_STATUSES = (
    "new",
    "saved",
    "dismissed",
    "interested",
    "registering",
    "applied",
    "pending_approval",
    "confirmed",
    "rejected",
)
DEFAULT_VISIBLE_STATUSES = (
    "new",
    "saved",
    "interested",
    "registering",
    "applied",
    "pending_approval",
    "confirmed",
)
USER_EVENT_RATINGS = {-1, 0, 1}
FRIEND_SIGNAL_STATUSES = (
    "registering",
    "applied",
    "pending_approval",
    "confirmed",
)
"""Statuses that count a friend as "going" for the scout-bot push card.

Allowlist (not deny-list): only these four states render in the friend-signal
line. ``saved`` and ``interested`` are explicitly excluded — they signal
curiosity, not commitment, and we don't want to falsely tell the channel
that a friend is going when they only bookmarked the event.
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dialect(conn: Any) -> str:
    return getattr(conn, "dialect", "sqlite")


def _is_postgres(conn: Any) -> bool:
    return _dialect(conn) == "postgres"


def _bool(conn: Any, value: bool) -> bool | int:
    return value if _is_postgres(conn) else 1 if value else 0


def _now_sql(conn: Any) -> str:
    return "CURRENT_TIMESTAMP" if _is_postgres(conn) else "datetime('now')"


def _future_start_at_sql(conn: Any) -> str:
    """SQL fragment for "events.start_at is strictly in the future, UTC".

    Why a helper: SQLite stores ``start_at`` as TEXT in ISO-8601 with
    offset (e.g. ``'2026-05-14T22:00:00+00:00'``). Raw string compare
    against ``datetime('now')`` (space-separated naive UTC like
    ``'2026-05-14 14:00:00'``) is incorrect at position 10 — ``'T'`` (84)
    sorts greater than ``' '`` (32), so a past-today event reads as
    "future" until midnight. The bug is silent: a high-scoring past-today
    event would slip through the scout-bot scan and post to ``#scout`` as
    "still future". Wrapping with ``datetime()`` parses the ISO string and
    re-emits in SQLite's native shape, making the compare correct.

    Postgres stores ``start_at`` as TEXT too (for parity with SQLite per
    spec §6) but Postgres ``CURRENT_TIMESTAMP`` is a typed TIMESTAMP and
    comparing against an ISO string auto-casts correctly. No wrapping
    needed on that side.
    """
    if _is_postgres(conn):
        return "start_at > CURRENT_TIMESTAMP"
    return "datetime(start_at) > datetime('now')"


def _posting_claim_available_sql(conn: Any) -> str:
    if _is_postgres(conn):
        return (
            "(posting_claimed_at IS NULL "
            "OR posting_claimed_at < CURRENT_TIMESTAMP - INTERVAL '1 hour')"
        )
    return (
        "(posting_claimed_at IS NULL "
        "OR julianday(posting_claimed_at) < julianday('now', '-1 hour'))"
    )


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with v1 PRAGMAs.

    Schema state is owned by db/migrate/sqlite/. Run `nyc-ai-scout migrate`
    (or apply_pending) once before the first cycle.
    """
    if path is None:
        path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def compute_fit_input_hash(title: str, start_at: str | None, venue: str | None,
                           *, host: str | None = None, description: str | None = None,
                           neighborhood: str | None = None, attendance_mode: str | None = None) -> str:
    """Fingerprint the captured source inputs used by the scoring worker."""
    instant = utc_instant(start_at)
    payload = json.dumps([title, instant.isoformat() if instant else start_at,
                          venue, host, description, neighborhood, attendance_mode], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_invite_code(invite_code: str) -> str:
    code = invite_code.strip()
    if not code:
        raise ValueError("invite_code must not be blank")
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return _validate_email(email)


def _validate_email(email: str) -> str:
    value = email.strip().lower()
    if not value or len(value) > 254:
        raise ValueError("email must be between 1 and 254 characters")
    # Minimal shape check: we don't run a full RFC 5322 validator (overkill
    # for a single-friend MVP allowlist), but reject the obviously bogus
    # inputs that bypass a length-only check — no ``@``, empty local part,
    # missing domain dot. This is what ``parse_friend_allowlist`` relies
    # on to skip fat-fingered entries silently rather than register a
    # malformed user.
    if "@" not in value:
        raise ValueError("email must contain '@'")
    local, _, domain = value.partition("@")
    if not local or not domain or "." not in domain:
        raise ValueError("email must have a non-empty local part and a domain with a dot")
    return value


def _validate_display_name(display_name: str) -> str:
    value = display_name.strip()
    if not value or len(value) > 80:
        raise ValueError("display_name must be between 1 and 80 characters")
    return value


def _validate_status(status: str) -> str:
    if status not in USER_EVENT_STATUSES:
        raise ValueError(f"status must be one of {', '.join(USER_EVENT_STATUSES)}")
    return status


def _validate_rating(rating: int | None) -> int | None:
    if rating is not None and rating not in USER_EVENT_RATINGS:
        raise ValueError("rating must be -1, 0, 1, or None")
    return rating


def _parse_datetime_utc(value: str | datetime | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def create_user(
    conn: sqlite3.Connection | CompatConnection,
    *,
    email: str,
    display_name: str,
    invite_code: str | None = None,
    interest_flags: Mapping[str, object] | None = None,
    is_active: bool = True,
) -> sqlite3.Row | Any:
    flags_json = json.dumps(interest_flags or {}, sort_keys=True, separators=(",", ":"))
    invite_code_hash = hash_invite_code(invite_code) if invite_code is not None else None
    values = (
        _validate_email(email),
        _validate_display_name(display_name),
        invite_code_hash,
        flags_json,
        _bool(conn, is_active),
    )
    if _is_postgres(conn):
        return conn.execute(
            """INSERT INTO users
                (email, display_name, invite_code_hash, interest_flags_json, is_active)
               VALUES (?, ?, ?, ?, ?)
               RETURNING *""",
            values,
        ).fetchone()
    cur = conn.execute(
        """INSERT INTO users
            (email, display_name, invite_code_hash, interest_flags_json, is_active)
           VALUES (?, ?, ?, ?, ?)""",
        values,
    )
    return conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()


def get_user_by_invite_code(
    conn: sqlite3.Connection | CompatConnection, invite_code: str
) -> sqlite3.Row | Any | None:
    return conn.execute(
        """SELECT * FROM users
           WHERE invite_code_hash = ?
             AND is_active = ?""",
        (hash_invite_code(invite_code), _bool(conn, True)),
    ).fetchone()


def _validate_identity_provider(provider: str) -> str:
    value = provider.strip().lower()
    if not value or len(value) > 32:
        raise ValueError("provider must be between 1 and 32 characters")
    return value


def _validate_identity_subject(subject: str) -> str:
    value = subject.strip()
    if not value or len(value) > 255:
        raise ValueError("subject must be between 1 and 255 characters")
    return value


def create_alpha_invite(
    conn: sqlite3.Connection | CompatConnection,
    *,
    email: str,
    invited_by_user_id: int | None = None,
) -> sqlite3.Row | Any:
    normalized = normalize_email(email)
    existing = find_active_alpha_invite(conn, normalized)
    if existing is not None:
        return existing
    now = _now_iso()
    values = (email.strip(), normalized, invited_by_user_id, now, now)
    if _is_postgres(conn):
        return conn.execute(
            """INSERT INTO alpha_invites
                (email, normalized_email, invited_by_user_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               RETURNING *""",
            values,
        ).fetchone()
    cur = conn.execute(
        """INSERT INTO alpha_invites
            (email, normalized_email, invited_by_user_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        values,
    )
    return conn.execute("SELECT * FROM alpha_invites WHERE id = ?", (cur.lastrowid,)).fetchone()


def find_active_alpha_invite(
    conn: sqlite3.Connection | CompatConnection,
    email: str,
) -> sqlite3.Row | Any | None:
    return conn.execute(
        """SELECT * FROM alpha_invites
           WHERE normalized_email = ?
             AND revoked_at IS NULL
           ORDER BY id DESC
           LIMIT 1""",
        (normalize_email(email),),
    ).fetchone()


def list_alpha_invites(conn: sqlite3.Connection | CompatConnection) -> list[sqlite3.Row | Any]:
    return conn.execute(
        """SELECT ai.*, u.email AS accepted_user_email
           FROM alpha_invites ai
           LEFT JOIN users u
             ON u.id = ai.accepted_user_id
           ORDER BY ai.id"""
    ).fetchall()


def revoke_alpha_invite(
    conn: sqlite3.Connection | CompatConnection,
    email: str,
) -> sqlite3.Row | Any | None:
    invite = find_active_alpha_invite(conn, email)
    if invite is None:
        return None
    now = _now_iso()
    conn.execute(
        """UPDATE alpha_invites
           SET revoked_at = ?,
               updated_at = ?
           WHERE id = ?""",
        (now, now, invite["id"]),
    )
    if invite["accepted_user_id"] is not None:
        conn.execute(
            """UPDATE users
               SET is_active = ?,
                   updated_at = ?
               WHERE id = ?""",
            (_bool(conn, False), now, invite["accepted_user_id"]),
        )
    return conn.execute("SELECT * FROM alpha_invites WHERE id = ?", (invite["id"],)).fetchone()


def list_users(conn: sqlite3.Connection | CompatConnection) -> list[sqlite3.Row | Any]:
    return conn.execute(
        """SELECT id, email, display_name, is_active, created_at
           FROM users
           ORDER BY id"""
    ).fetchall()


def rotate_user_invite(
    conn: sqlite3.Connection | CompatConnection, email: str, invite_code: str
) -> int:
    cur = conn.execute(
        f"""UPDATE users
            SET invite_code_hash = ?,
                updated_at = {_now_sql(conn)}
            WHERE email = ?""",
        (hash_invite_code(invite_code), normalize_email(email)),
    )
    return int(cur.rowcount)


def deactivate_user(conn: sqlite3.Connection | CompatConnection, email: str) -> int:
    cur = conn.execute(
        f"""UPDATE users
            SET is_active = ?,
                updated_at = {_now_sql(conn)}
            WHERE email = ?""",
        (_bool(conn, False), normalize_email(email)),
    )
    return int(cur.rowcount)


def get_user_by_identity(
    conn: sqlite3.Connection | CompatConnection,
    *,
    provider: str,
    subject: str,
) -> sqlite3.Row | Any | None:
    return conn.execute(
        """SELECT u.*
           FROM linked_identities li
           JOIN users u
             ON u.id = li.user_id
           WHERE li.provider = ?
             AND li.subject = ?
             AND u.is_active = ?""",
        (
            _validate_identity_provider(provider),
            _validate_identity_subject(subject),
            _bool(conn, True),
        ),
    ).fetchone()


def get_active_user(
    conn: sqlite3.Connection | CompatConnection,
    user_id: int,
) -> sqlite3.Row | Any | None:
    return conn.execute(
        """SELECT * FROM users
           WHERE id = ?
             AND is_active = ?""",
        (user_id, _bool(conn, True)),
    ).fetchone()


def link_identity(
    conn: sqlite3.Connection | CompatConnection,
    *,
    provider: str,
    subject: str,
    user_id: int,
    email: str,
    email_verified: bool,
) -> sqlite3.Row | Any:
    provider = _validate_identity_provider(provider)
    subject = _validate_identity_subject(subject)
    normalized = normalize_email(email)
    existing = conn.execute(
        """SELECT * FROM linked_identities
           WHERE provider = ?
             AND subject = ?""",
        (provider, subject),
    ).fetchone()
    if existing is not None and existing["user_id"] != user_id:
        raise ValueError("identity is already linked to another user")
    now = _now_iso()
    conn.execute(
        """INSERT INTO linked_identities
            (provider, subject, user_id, email, email_verified, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(provider, subject) DO UPDATE SET
            email = excluded.email,
            email_verified = excluded.email_verified,
            updated_at = excluded.updated_at""",
        (provider, subject, user_id, normalized, _bool(conn, email_verified), now, now),
    )
    return conn.execute(
        """SELECT * FROM linked_identities
           WHERE provider = ?
             AND subject = ?""",
        (provider, subject),
    ).fetchone()


def create_or_accept_google_user(
    conn: sqlite3.Connection | CompatConnection,
    *,
    subject: str,
    email: str,
    email_verified: bool,
    display_name: str | None = None,
) -> sqlite3.Row | Any | None:
    if not email_verified:
        return None
    normalized = normalize_email(email)
    invite = find_active_alpha_invite(conn, normalized)
    if invite is None:
        return None

    existing = get_user_by_identity(conn, provider="google", subject=subject)
    if existing is not None:
        accepted_user_id = invite["accepted_user_id"]
        if accepted_user_id is not None and accepted_user_id != existing["id"]:
            return None
        if accepted_user_id is None:
            now = _now_iso()
            conn.execute(
                """UPDATE alpha_invites
                   SET accepted_user_id = ?,
                       accepted_at = COALESCE(accepted_at, ?),
                       updated_at = ?
                   WHERE id = ?""",
                (existing["id"], now, now, invite["id"]),
            )
        link_identity(
            conn,
            provider="google",
            subject=subject,
            user_id=existing["id"],
            email=email,
            email_verified=email_verified,
        )
        return existing

    user: sqlite3.Row | None = None
    if invite["accepted_user_id"] is not None:
        user = conn.execute(
            """SELECT * FROM users
               WHERE id = ?
               LIMIT 1""",
            (invite["accepted_user_id"],),
        ).fetchone()
        if user is None:
            return None
        if not user["is_active"]:
            now = _now_iso()
            conn.execute(
                """UPDATE users
                   SET is_active = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (_bool(conn, True), now, user["id"]),
            )
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    else:
        user = conn.execute(
            """SELECT * FROM users
               WHERE email = ?
               LIMIT 1""",
            (normalized,),
        ).fetchone()
        if user is None:
            user = create_user(
                conn,
                email=normalized,
                display_name=display_name or normalized.split("@", 1)[0],
                invite_code=None,
                interest_flags={"ai": True},
            )
        elif not user["is_active"]:
            now = _now_iso()
            conn.execute(
                """UPDATE users
                   SET is_active = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (_bool(conn, True), now, user["id"]),
            )
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()

    now = _now_iso()
    conn.execute(
        """UPDATE alpha_invites
           SET accepted_user_id = ?,
               accepted_at = COALESCE(accepted_at, ?),
               updated_at = ?
           WHERE id = ?""",
        (user["id"], now, now, invite["id"]),
    )

    link_identity(
        conn,
        provider="google",
        subject=subject,
        user_id=user["id"],
        email=normalized,
        email_verified=True,
    )
    return conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()


def create_or_accept_supabase_user(
    conn: sqlite3.Connection | CompatConnection,
    *,
    subject: str,
    email: str,
    email_verified: bool,
    display_name: str | None = None,
    allow_signup: bool = False,
) -> sqlite3.Row | Any | None:
    """Admit a verified identity by invitation or explicit signup policy, never reactivate."""
    if not email_verified:
        return None
    subject = _validate_identity_subject(subject)
    normalized = normalize_email(email)
    invite = find_active_alpha_invite(conn, normalized)
    revoked = conn.execute(
        "SELECT id FROM alpha_invites WHERE normalized_email = ? AND revoked_at IS NOT NULL LIMIT 1",
        (normalized,),
    ).fetchone()
    if invite is None and revoked is not None:
        return None

    linked = conn.execute(
        """SELECT u.*
           FROM linked_identities li
           JOIN users u ON u.id = li.user_id
           WHERE li.provider = 'supabase'
             AND li.subject = ?""",
        (subject,),
    ).fetchone()
    if linked is not None:
        if not linked["is_active"] or normalize_email(linked["email"]) != normalized:
            return None
        accepted_user_id = invite["accepted_user_id"] if invite else None
        if accepted_user_id is not None and accepted_user_id != linked["id"]:
            return None
        user = linked
    elif invite is None and not allow_signup:
        return None
    elif invite is not None and invite["accepted_user_id"] is not None:
        user = conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (invite["accepted_user_id"],),
        ).fetchone()
        if user is None or not user["is_active"]:
            return None
    else:
        user = conn.execute(
            "SELECT * FROM users WHERE email = ? LIMIT 1",
            (normalized,),
        ).fetchone()
        if user is not None and not user["is_active"]:
            return None
        if user is None:
            user = create_user(
                conn,
                email=normalized,
                display_name=display_name or normalized.split("@", 1)[0],
                invite_code=None,
                interest_flags={"ai": True},
            )

    if invite is not None:
        now = _now_iso()
        conn.execute(
            """UPDATE alpha_invites
               SET accepted_user_id = ?,
                   accepted_at = COALESCE(accepted_at, ?),
                   updated_at = ?
               WHERE id = ?""",
            (user["id"], now, now, invite["id"]),
        )
    link_identity(
        conn,
        provider="supabase",
        subject=subject,
        user_id=user["id"],
        email=normalized,
        email_verified=True,
    )
    return conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()


def ensure_user_event_state(
    conn: sqlite3.Connection | CompatConnection,
    user_id: int,
    event_id: int,
    *,
    status: str = "new",
) -> sqlite3.Row | Any:
    status = _validate_status(status)
    now = _now_iso()
    if _is_postgres(conn):
        conn.execute(
            """INSERT INTO user_event_states
                (user_id, event_id, status, created_at, updated_at, status_updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, event_id) DO NOTHING""",
            (user_id, event_id, status, now, now, now),
        )
    else:
        conn.execute(
            """INSERT OR IGNORE INTO user_event_states
                (user_id, event_id, status, created_at, updated_at, status_updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, event_id, status, now, now, now),
        )
    return conn.execute(
        """SELECT * FROM user_event_states
           WHERE user_id = ? AND event_id = ?""",
        (user_id, event_id),
    ).fetchone()


def set_user_event_status(
    conn: sqlite3.Connection | CompatConnection,
    user_id: int,
    event_id: int,
    status: str,
) -> sqlite3.Row | Any:
    status = _validate_status(status)
    now = _now_iso()
    conn.execute(
        """INSERT INTO user_event_states
            (user_id, event_id, status, created_at, updated_at, status_updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, event_id) DO UPDATE SET
            status = excluded.status,
            updated_at = excluded.updated_at,
            status_updated_at = excluded.status_updated_at""",
        (user_id, event_id, status, now, now, now),
    )
    return conn.execute(
        """SELECT * FROM user_event_states
           WHERE user_id = ? AND event_id = ?""",
        (user_id, event_id),
    ).fetchone()


def clear_user_event_state(
    conn: sqlite3.Connection | CompatConnection,
    user_id: int,
    event_id: int,
) -> None:
    conn.execute(
        """DELETE FROM user_event_states
           WHERE user_id = ?
             AND event_id = ?""",
        (user_id, event_id),
    )


def set_user_event_rating(
    conn: sqlite3.Connection | CompatConnection,
    user_id: int,
    event_id: int,
    rating: int | None,
) -> sqlite3.Row | Any:
    rating = _validate_rating(rating)
    now = _now_iso()
    ensure_user_event_state(conn, user_id, event_id)
    conn.execute(
        """UPDATE user_event_states
           SET rating = ?,
               updated_at = ?,
               rating_updated_at = ?
           WHERE user_id = ?
             AND event_id = ?""",
        (rating, now, now, user_id, event_id),
    )
    return conn.execute(
        """SELECT * FROM user_event_states
           WHERE user_id = ? AND event_id = ?""",
        (user_id, event_id),
    ).fetchone()


def fetch_events_for_user(
    conn: sqlite3.Connection | CompatConnection,
    user_id: int,
    *,
    status: str | None = None,
    min_score: float | None = None,
    source: str | None = None,
    include_past: bool = False,
    start_at_from: datetime | None = None,
    limit: int = 100,
) -> list[sqlite3.Row | Any]:
    params: list[object] = [user_id]
    where = [
        "e.scoring_status = 'scored'",
        "e.fit_score IS NOT NULL",
        "e.cancelled_at IS NULL",
    ]
    if status is None:
        placeholders = ", ".join("?" for _ in DEFAULT_VISIBLE_STATUSES)
        where.append(f"COALESCE(s.status, 'new') IN ({placeholders})")
        params.extend(DEFAULT_VISIBLE_STATUSES)
    elif status == "all":
        pass
    else:
        where.append("COALESCE(s.status, 'new') = ?")
        params.append(_validate_status(status))

    if min_score is not None:
        where.append("e.fit_score >= ?")
        params.append(max(0.0, min(1.0, float(min_score))))

    if source:
        where.append("e.source = ?")
        params.append(source)

    rows = conn.execute(
        f"""SELECT
                e.*,
                COALESCE(s.status, 'new') AS user_status,
                s.rating AS user_rating,
                s.status_updated_at,
                s.rating_updated_at
            FROM events e
            LEFT JOIN user_event_states s
              ON s.event_id = e.id
             AND s.user_id = ?
            WHERE {" AND ".join(where)}
            ORDER BY
                CASE WHEN e.start_at IS NULL THEN 1 ELSE 0 END,
                e.start_at ASC,
                e.fit_score DESC,
                e.id DESC""",
        params,
    ).fetchall()
    if not include_past:
        cutoff = start_at_from.astimezone(UTC) if start_at_from else datetime.now(UTC)
        rows = [
            row
            for row in rows
            if (starts_at := _parse_datetime_utc(row["start_at"])) is not None
            and starts_at >= cutoff
        ]
    rows.sort(
        key=lambda row: (
            _parse_datetime_utc(row["start_at"]) is None,
            _parse_datetime_utc(row["start_at"]) or datetime.max.replace(tzinfo=UTC),
            -float(row["fit_score"] or 0.0),
            -int(row["id"]),
        )
    )
    clamped_limit = max(1, min(500, int(limit)))
    return rows[:clamped_limit]


def event_exists(conn: sqlite3.Connection | CompatConnection, event_id: int) -> bool:
    return conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone() is not None


def event_counts(conn: sqlite3.Connection | CompatConnection) -> tuple[int, int]:
    total = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    scored = conn.execute(
        """SELECT count(*) FROM events
           WHERE scoring_status = 'scored'
             AND fit_score IS NOT NULL"""
    ).fetchone()[0]
    return int(total), int(scored)


def list_sources(conn: sqlite3.Connection | CompatConnection) -> list[str]:
    return [
        str(r["source"])
        for r in conn.execute(
            """SELECT DISTINCT source FROM events
               WHERE source IS NOT NULL
                 AND cancelled_at IS NULL
               ORDER BY source"""
        ).fetchall()
    ]


def get_previous_user_status(
    conn: sqlite3.Connection | CompatConnection,
    user_id: int,
    event_id: int,
) -> str:
    row = conn.execute(
        """SELECT status FROM user_event_states
           WHERE user_id = ?
             AND event_id = ?""",
        (user_id, event_id),
    ).fetchone()
    return str(row["status"]) if row else "new"


def get_event_source_url(
    conn: sqlite3.Connection | CompatConnection,
    event_id: int,
) -> sqlite3.Row | Any | None:
    return conn.execute("SELECT source, url FROM events WHERE id = ?", (event_id,)).fetchone()


def cleanup_posted_events(conn: sqlite3.Connection | CompatConnection, days: int) -> int:
    if _is_postgres(conn):
        sql = """DELETE FROM events
                 WHERE start_at IS NOT NULL
                   AND start_at < (CURRENT_TIMESTAMP - CAST((:days || ' days') AS interval))
                   AND posted_at IS NOT NULL
                   AND NOT EXISTS (
                        SELECT 1 FROM user_event_states s
                        WHERE s.event_id = events.id
                   )"""
        cur = conn.execute(sql, {"days": int(days)})
    else:
        has_user_states = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type = 'table'
                 AND name = 'user_event_states'"""
        ).fetchone()
        sql = """DELETE FROM events
                 WHERE start_at IS NOT NULL
                   AND start_at < datetime('now', ?)
                   AND posted_at IS NOT NULL"""
        if has_user_states:
            sql += """
                   AND NOT EXISTS (
                        SELECT 1 FROM user_event_states s
                        WHERE s.event_id = events.id
                   )"""
        cur = conn.execute(sql, (f"-{int(days)} days",))
    return int(cur.rowcount)


def write_runtime_marker(
    conn: sqlite3.Connection | CompatConnection,
    name: str,
    reason: str,
) -> None:
    now = _now_iso()
    conn.execute(
        """INSERT INTO runtime_markers (name, reason, created_at, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
            reason = excluded.reason,
            updated_at = excluded.updated_at""",
        (name, reason, now, now),
    )


def is_runtime_marked(conn: sqlite3.Connection | CompatConnection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM runtime_markers WHERE name = ?",
        (name,),
    ).fetchone() is not None


def clear_runtime_marker(conn: sqlite3.Connection | CompatConnection, name: str) -> None:
    conn.execute("DELETE FROM runtime_markers WHERE name = ?", (name,))


def runtime_marker_updated_at(
    conn: sqlite3.Connection | CompatConnection,
    name: str,
) -> datetime | None:
    """Return the ``updated_at`` timestamp of the marker, or None if absent.

    Used by the marker cooldown path (SCORING_DISABLED auto-expiry) so the
    cycle can self-heal after a transient CLI rate-limit window without
    waiting for operator intervention. SQLite stores TEXT (`datetime('now')`),
    Postgres stores TIMESTAMPTZ — both decode to a tz-aware ``datetime`` here.
    """
    row = conn.execute(
        "SELECT updated_at FROM runtime_markers WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    raw = row["updated_at"]
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str):
        # SQLite datetime('now') => "2026-05-16 02:30:11"; ISO 8601 also accepted.
        text = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def load_cycle_health_row(conn: sqlite3.Connection | CompatConnection) -> dict[str, Any]:
    row = conn.execute(
        """SELECT consecutive_all_failed, last_error, last_failed_at, last_success_at
           FROM cycle_health
           WHERE id = ?""",
        (_bool(conn, True),),
    ).fetchone()
    if row is None:
        return {
            "consecutive_all_failed": 0,
            "last_error": None,
            "last_failed_at": None,
            "last_success_at": None,
        }
    return {
        "consecutive_all_failed": row["consecutive_all_failed"],
        "last_error": row["last_error"],
        "last_failed_at": row["last_failed_at"],
        "last_success_at": row["last_success_at"],
    }


def save_cycle_health_row(
    conn: sqlite3.Connection | CompatConnection,
    payload: Mapping[str, Any],
) -> None:
    now = _now_iso()
    conn.execute(
        """INSERT INTO cycle_health
            (id, consecutive_all_failed, last_error, last_failed_at, last_success_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
            consecutive_all_failed = excluded.consecutive_all_failed,
            last_error = excluded.last_error,
            last_failed_at = excluded.last_failed_at,
            last_success_at = excluded.last_success_at,
            updated_at = excluded.updated_at""",
        (
            _bool(conn, True),
            int(payload.get("consecutive_all_failed") or 0),
            payload.get("last_error"),
            payload.get("last_failed_at"),
            payload.get("last_success_at"),
            now,
        ),
    )


def try_acquire_cycle_lock(
    conn: sqlite3.Connection | CompatConnection,
    *,
    name: str,
    owner: str,
    ttl_seconds: int = 3600,
) -> bool:
    if not _is_postgres(conn):
        return True
    now = datetime.now(UTC)
    locked_until = (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
    cur = conn.execute(
        """UPDATE cycle_locks
           SET owner = ?,
               locked_until = ?,
               updated_at = ?
           WHERE name = ?
             AND locked_until < CURRENT_TIMESTAMP""",
        (owner, locked_until, now.isoformat(timespec="seconds"), name),
    )
    if cur.rowcount:
        return True
    cur = conn.execute(
        """INSERT INTO cycle_locks (name, owner, locked_until, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(name) DO NOTHING""",
        (name, owner, locked_until, now.isoformat(timespec="seconds")),
    )
    return bool(cur.rowcount)


def release_cycle_lock(
    conn: sqlite3.Connection | CompatConnection,
    *,
    name: str,
    owner: str,
) -> None:
    if not _is_postgres(conn):
        return
    conn.execute(
        "DELETE FROM cycle_locks WHERE name = ? AND owner = ?",
        (name, owner),
    )


def upsert_raw(conn: sqlite3.Connection | CompatConnection, raw: RawEvent) -> int:
    """Insert or update by url. Returns row id.

    Reset derived facts only when effective scoring inputs change, not merely
    when a stored fingerprint uses an older format.
    """
    now = _now_iso()
    start_at_iso = raw.starts_at.isoformat() if raw.starts_at else None
    existing = conn.execute(
        "SELECT id, title, start_at, venue, neighborhood, attendance_mode, host, "
        "registration_url, description, source_content_checked_at "
        "FROM events WHERE url = ?", (raw.url,)
    ).fetchone()
    if existing:
        registration_url = raw.registration_url
        same_title = " ".join(existing["title"].casefold().split()) == " ".join(raw.title.casefold().split())
        same_start = utc_instant(existing["start_at"]) == utc_instant(start_at_iso)
        if registration_url is None and same_title and same_start:
            registration_url = existing["registration_url"]
        description = raw.description
        content_checked_at = now
        if raw.source == "meetup" and not (description or "").strip() and same_title and same_start:
            # An evidence-absent refresh cannot retract the last observed
            # registration assertion for the same occurrence.
            description = existing["description"]
            if description:
                content_checked_at = existing["source_content_checked_at"]
        attendance_mode = raw.attendance_mode or existing["attendance_mode"]
        new_hash = compute_fit_input_hash(
            raw.title, start_at_iso, raw.venue, host=raw.host, description=description,
            neighborhood=raw.neighborhood, attendance_mode=attendance_mode,
        )
        changed = (existing["title"] != raw.title or not same_start
                   or existing["venue"] != raw.venue or existing["host"] != raw.host
                   or existing["description"] != description
                   or existing["neighborhood"] != raw.neighborhood
                   or existing["attendance_mode"] != attendance_mode)
        if changed:
            # Ranking and annotations describe the same captured inputs.
            conn.execute(
                """UPDATE events SET
                    title=?, start_at=?, ends_at=?, venue=?, neighborhood=?,
                    description=?, host=?, last_seen=?, attendance_mode=COALESCE(?, attendance_mode),
                    registration_url=?, source_content_checked_at=?,
                    fit_input_hash=?, scoring_status='pending',
                    fit_score=NULL, fit_reasoning=NULL, urgency=NULL,
                    urgency_reason=NULL, scoring_failed_reason=NULL,
                    prompt_version=NULL, taste_sha=NULL,
                    notable_people=NULL, event_type=NULL, food_provided=NULL,
                    highlight=NULL, urgency_note=NULL, scored_at=NULL
                  WHERE url=?""",
                (
                    raw.title, start_at_iso,
                    raw.ends_at.isoformat() if raw.ends_at else None,
                    raw.venue, raw.neighborhood, description, raw.host,
                    now, raw.attendance_mode, registration_url, content_checked_at, new_hash, raw.url,
                ),
            )
        else:
            conn.execute(
                """UPDATE events SET
                    title=?, start_at=?, ends_at=?, venue=?, neighborhood=?,
                    description=?, host=?, last_seen=?, attendance_mode=COALESCE(?, attendance_mode),
                    registration_url=?, source_content_checked_at=?, fit_input_hash=?
                  WHERE url=?""",
                (
                    raw.title, start_at_iso,
                    raw.ends_at.isoformat() if raw.ends_at else None,
                    raw.venue, raw.neighborhood, description, raw.host,
                    now, raw.attendance_mode, registration_url, content_checked_at, new_hash, raw.url,
                ),
            )
        record_ingested_availability(conn, existing["id"], raw)
        return existing["id"]
    new_hash = compute_fit_input_hash(
        raw.title, start_at_iso, raw.venue, host=raw.host, description=raw.description,
        neighborhood=raw.neighborhood, attendance_mode=raw.attendance_mode,
    )
    values = (
        raw.source, raw.url, raw.title, start_at_iso,
        raw.ends_at.isoformat() if raw.ends_at else None,
        raw.venue, raw.neighborhood, raw.description, raw.host,
        now, now, new_hash, raw.attendance_mode, raw.registration_url, now,
    )
    if _is_postgres(conn):
        row = conn.execute(
            """INSERT INTO events
                (source, url, title, start_at, ends_at, venue, neighborhood,
                 description, host, first_seen, last_seen, fit_input_hash, attendance_mode, registration_url, source_content_checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            values,
        ).fetchone()
        event_id = int(row["id"])
        record_ingested_availability(conn, event_id, raw)
        return event_id
    cur = conn.execute(
        """INSERT INTO events
            (source, url, title, start_at, ends_at, venue, neighborhood,
             description, host, first_seen, last_seen, fit_input_hash, attendance_mode, registration_url, source_content_checked_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        values,
    )
    event_id = cur.lastrowid
    record_ingested_availability(conn, event_id, raw)
    return event_id  # type: ignore[return-value]


def fetch_pending_scoring(
    conn: sqlite3.Connection | CompatConnection, limit: int = 20,
    *, refresh_prompt_version: str | None = None, exclude_ids: frozenset[int] = frozenset(),
    now: datetime | None = None,
) -> list[sqlite3.Row | Any]:
    """Spend scoring capacity only on nonterminal source listings."""
    excluded = excluded_source_identities(catalog_rows(conn))
    refresh = ""
    params: list[Any] = []
    if refresh_prompt_version is not None:
        clock = now or datetime.now(UTC)
        if clock.tzinfo is None or clock.utcoffset() is None:
            raise ValueError("scoring refresh requires a timezone-aware clock")
        future = "start_at > ?" if _is_postgres(conn) else "datetime(start_at) > datetime(?)"
        refresh = (f" OR (scoring_status='scored' AND {future} "
                   "AND (prompt_version IS NULL OR prompt_version <> ?))")
        params.extend((clock.astimezone(UTC).isoformat(), refresh_prompt_version))
    pending = conn.execute(
        f"""SELECT * FROM events
           WHERE (scoring_status = 'pending' {refresh})
             AND cancelled_at IS NULL
             AND registration_availability NOT IN ('closed', 'full', 'canceled')
           ORDER BY CASE WHEN scoring_status='pending' THEN 0 ELSE 1 END,
                    first_seen DESC, id DESC""", params,
    ).fetchall()
    return [row for row in pending
            if row["id"] not in exclude_ids and provider_identity(row["url"]) not in excluded][:max(0, limit)]


def release_posting_claim(
    conn: sqlite3.Connection | CompatConnection,
    event_id: int,
    *,
    token: str,
) -> None:
    conn.execute(
        """UPDATE events
           SET posting_claimed_at = NULL,
               posting_claim_token = NULL
           WHERE id = ?
             AND posting_claim_token = ?""",
        (event_id, token),
    )


class _HashGuard(Enum):
    UNSET = "unset"


def _fit_hash_guard(expected: str | _HashGuard | None) -> tuple[str, tuple[Any, ...]]:
    """Distinguish an omitted guard from a captured legacy NULL fingerprint."""
    if expected is _HashGuard.UNSET:
        return "", ()
    if expected is None:
        return " AND fit_input_hash IS NULL", ()
    return " AND fit_input_hash=?", (expected,)


def update_fit(
    conn: sqlite3.Connection | CompatConnection,
    event_id: int,
    *,
    fit_score: float,
    reasoning: str,
    urgency: str,
    urgency_reason: str | None,
    prompt_version: str,
    taste_sha: str,
    notable_people: list[dict[str, Any]] | None = None,
    event_type: str | None = None,
    food_provided: str | None = None,
    highlight: dict[str, Any] | None = None,
    urgency_note: dict[str, Any] | None = None,
    expected_input_hash: str | _HashGuard | None = _HashGuard.UNSET,
) -> bool:
    """Persist a scored fit plus the v2 extraction fields.

    ``notable_people`` is stored as a JSON-encoded TEXT column (NULL when
    empty / not supplied). ``event_type`` and ``food_provided`` are plain
    TEXT and NULL when the LLM did not classify them — the renderer treats
    NULL as "skip the line."

    Always writes all three v2 columns (NULL when absent). Callers running
    against pre-0006 schemas must drive the row in via direct SQL — the
    storage layer no longer branches on the presence of v2 kwargs.
    """
    source = conn.execute(
        "SELECT title, description, host, venue, fit_input_hash FROM events WHERE id=?", (event_id,),
    ).fetchone()
    source = dict(source) if source is not None else {}
    if expected_input_hash is not _HashGuard.UNSET and source.get("fit_input_hash") != expected_input_hash:
        return False
    positive = grounded_annotation(highlight, EventHighlight, source)
    action = grounded_annotation(urgency_note, EventUrgencyNote, source)
    notable_json = (
        json.dumps(notable_people, ensure_ascii=False, separators=(",", ":"))
        if notable_people
        else None
    )
    hash_guard, hash_params = _fit_hash_guard(expected_input_hash)
    result = conn.execute(
        f"""UPDATE events SET
            fit_score=?, fit_reasoning=?, urgency=?, urgency_reason=?,
            scoring_status='scored', scoring_failed_reason=NULL,
            prompt_version=?, taste_sha=?,
            notable_people=?, event_type=?, food_provided=?,
            highlight=?, urgency_note=?, scored_at=?
          WHERE id=?{hash_guard}""",
        (
            fit_score, reasoning, urgency, urgency_reason,
            prompt_version, taste_sha,
            notable_json, event_type, food_provided,
            positive.model_dump_json() if positive else None,
            action.model_dump_json() if action else None, _now_iso(),
            event_id, *hash_params,
        ),
    )
    return result.rowcount > 0


def mark_scoring_failed(
    conn: sqlite3.Connection | CompatConnection, event_id: int, reason: str,
    *, expected_input_hash: str | _HashGuard | None = _HashGuard.UNSET,
) -> None:
    """Record failure; guarded worker calls preserve a valid score or newer input."""
    hash_guard, hash_params = _fit_hash_guard(expected_input_hash)
    state_guard = " AND scoring_status != 'scored'" if expected_input_hash is not _HashGuard.UNSET else ""
    conn.execute(
        f"""UPDATE events SET
            scoring_status='failed', scoring_failed_reason=?
          WHERE id=?{state_guard}{hash_guard}""",
        (reason, event_id, *hash_params),
    )


def _requeue_scoring_failure_filter(
    *,
    reason: str,
    now_iso: str,
    future_only: bool = True,
    unposted_only: bool = True,
) -> tuple[list[str], list[Any]]:
    where = [
        "scoring_status = 'failed'",
        "scoring_failed_reason = ?",
        "scoring_failed_reason != 'retracted_by_admin'",
        "cancelled_at IS NULL",
        "posted_at IS NULL",
        "posting_claimed_at IS NULL",
    ]
    params: list[Any] = [reason]
    if future_only:
        where.append("start_at IS NOT NULL")
        where.append("start_at >= ?")
        params.append(now_iso)
    if unposted_only:
        where.append("discord_push_message_id IS NULL")
    return where, params


def requeue_scoring_failures(
    conn: sqlite3.Connection | CompatConnection,
    *,
    reason: str,
    now_iso: str,
    future_only: bool = True,
    unposted_only: bool = True,
    limit: int | None = None,
) -> int:
    where, params = _requeue_scoring_failure_filter(
        reason=reason,
        now_iso=now_iso,
        future_only=future_only,
        unposted_only=unposted_only,
    )
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(limit)

    cur = conn.execute(
        f"""UPDATE events
               SET scoring_status = 'pending',
                   scoring_failed_reason = NULL,
                   fit_score = NULL,
                   fit_reasoning = NULL,
                   urgency = NULL,
                   urgency_reason = NULL,
                   prompt_version = NULL,
                   taste_sha = NULL,
                   notable_people = NULL,
                   event_type = NULL,
                   food_provided = NULL
             WHERE id IN (
                   SELECT id FROM events
                    WHERE {' AND '.join(where)}
                    ORDER BY start_at ASC, id ASC
                    {limit_sql}
             )""",
        tuple(params),
    )
    return int(cur.rowcount)


def count_requeueable_scoring_failures(
    conn: sqlite3.Connection | CompatConnection,
    *,
    reason: str,
    now_iso: str,
    future_only: bool = True,
    unposted_only: bool = True,
    limit: int | None = None,
) -> int:
    where, params = _requeue_scoring_failure_filter(
        reason=reason,
        now_iso=now_iso,
        future_only=future_only,
        unposted_only=unposted_only,
    )
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(limit)
    row = conn.execute(
        f"""SELECT COUNT(*)
              FROM (
                    SELECT id FROM events
                     WHERE {' AND '.join(where)}
                     ORDER BY start_at ASC, id ASC
                     {limit_sql}
              ) AS requeueable""",
        tuple(params),
    ).fetchone()
    return int(row[0] if row is not None else 0)


def mark_posted(
    conn: sqlite3.Connection | CompatConnection,
    event_id: int,
    *,
    discord_push_message_id: str | None = None,
) -> None:
    """Atomically transition an event from "claimed" to "posted".

    Sets ``posted_at`` (now), clears the claim columns, and optionally
    folds in the Discord ``?wait=true`` message id in the same UPDATE so
    the scout-bot success path is a single statement.

    Atomicity matters: ``_cycle_transaction`` opens SQLite with
    ``isolation_level=None`` (autocommit), so a previous two-statement
    sequence (``set_discord_push_message_id`` then ``mark_posted``) would
    let a crash between them strand a row with the message id set but
    ``posted_at IS NULL`` and a dangling claim. The next scan correctly
    won't re-pick (``discord_push_message_id IS NOT NULL`` filter), but
    the dangling claim would sit until the 1h TTL.

    Passing ``discord_push_message_id=None`` (the default) keeps legacy
    callers (the post-loop in ``run_cycle`` that doesn't track message
    ids) working unchanged; ``COALESCE`` preserves an existing value if
    a future caller wants to mark posted without overwriting the id.
    """
    conn.execute(
        """UPDATE events
           SET posted_at = ?,
               posting_claimed_at = NULL,
               posting_claim_token = NULL,
               discord_push_message_id = COALESCE(?, discord_push_message_id)
           WHERE id = ?""",
        (_now_iso(), discord_push_message_id, event_id),
    )


def fetch_top_unpushed_future_event(
    conn: sqlite3.Connection | CompatConnection,
    *,
    high_bar_threshold: float,
) -> sqlite3.Row | Any | None:
    """Return the single best unpushed future event above the high bar.

    Backs the scout-bot scan-and-push trigger (spec §1 / migration 0006).
    Filters:
      - ``discord_push_message_id IS NULL`` — never pushed.
      - ``start_at > now()`` — still future (UTC string compare; the scraper
        normalizes to ISO-8601 UTC, see ``_parse_datetime_utc``).
      - ``fit_score >= high_bar_threshold`` — above the high bar.
      - ``cancelled_at IS NULL`` — defensive; cancelled events should not push.
      - claim available — ``posting_claimed_at`` either NULL or older than 1h.

    Ordered by ``fit_score DESC, start_at`` to match the partial index
    ``idx_events_unpushed_future_by_score`` from migration 0006. LIMIT 1
    because the scout pushes at most one event per cycle tick.
    """
    claim_available = _posting_claim_available_sql(conn)
    return conn.execute(
        f"""SELECT * FROM events
            WHERE discord_push_message_id IS NULL
              AND cancelled_at IS NULL
              AND scoring_status = 'scored'
              AND start_at IS NOT NULL
              AND {_future_start_at_sql(conn)}
              AND fit_score >= ?
              AND {claim_available}
            ORDER BY fit_score DESC, start_at ASC
            LIMIT 1""",
        (high_bar_threshold,),
    ).fetchone()


def fetch_top_unpushed_future_score(
    conn: sqlite3.Connection | CompatConnection,
) -> float | None:
    """Sibling of ``fetch_top_unpushed_future_event`` without the bar.

    Returns the highest ``fit_score`` in the unpushed-future pool regardless
    of whether it crosses ``high_bar_threshold``. Used by the ``tick_scan``
    metric (spec §11) so Yang can plot the score distribution against the
    bar and tune the threshold after a week of production data.

    Returns ``None`` when the pool is empty.
    """
    row = conn.execute(
        f"""SELECT MAX(fit_score) AS top1 FROM events
            WHERE discord_push_message_id IS NULL
              AND cancelled_at IS NULL
              AND scoring_status = 'scored'
              AND start_at IS NOT NULL
              AND {_future_start_at_sql(conn)}
              AND fit_score IS NOT NULL""",
    ).fetchone()
    if row is None:
        return None
    top1 = row["top1"] if hasattr(row, "keys") else row[0]
    return float(top1) if top1 is not None else None


# Spec §11 telemetry: the closed enum of event_kind values mirrored from
# migration 0006's CHECK constraint. The Python set is the first line of
# defense; the SQL CHECK constraint is the second. Keep these two in sync.
SCOUT_METRIC_KINDS: frozenset[str] = frozenset({
    "tick_scan",
    "push_attempt",
    "capacity_poll",
    "capacity_patch",
    "retract",
    "slash_command",
    "friend_signal",
})


def write_scout_metric(
    conn: sqlite3.Connection | CompatConnection,
    *,
    event_kind: str,
    event_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append-only write to ``scout_metrics`` (spec §11).

    Never raises into the caller — telemetry must not break the cycle, the
    bot, or the capacity poller. A failed insert is logged and swallowed.

    The payload is JSON-encoded into the TEXT column. Aggregate-only data:
    counts, scores, status enums, threshold ints. **Never** event titles,
    friend display names, Discord user IDs, or webhook URLs (the column is
    treated as semi-public — Yang inspects it for tuning).
    """
    if event_kind not in SCOUT_METRIC_KINDS:
        # Programmer error — fail loud here even though we swallow IO errors
        # below. Bad kind would also be rejected by the SQL CHECK constraint
        # but a clear ValueError is friendlier than the sqlite OperationalError.
        raise ValueError(
            f"unknown scout_metric event_kind {event_kind!r}; "
            f"valid kinds: {sorted(SCOUT_METRIC_KINDS)}"
        )
    payload_text = json.dumps(payload, separators=(",", ":")) if payload else None
    try:
        conn.execute(
            "INSERT INTO scout_metrics (event_kind, event_id, payload) "
            "VALUES (?, ?, ?)",
            (event_kind, event_id, payload_text),
        )
    except Exception as exc:
        # Defensive: a telemetry-table problem must not abort the parent op.
        # Aggregate-only log — no event title or payload echo at warn level.
        import logging
        logging.getLogger(__name__).warning(
            "scout_metric write failed kind=%s exc=%s",
            event_kind, type(exc).__name__,
        )


@dataclass(frozen=True)
class ScoutClaim:
    """Token returned by ``claim_postable_for_scout`` for the matching release."""

    event_id: int
    token: str


def claim_postable_for_scout(
    conn: sqlite3.Connection | CompatConnection,
    *,
    event_id: int,
    token: str,
) -> ScoutClaim | None:
    """Atomically claim a specific event for the scout-bot push.

    Uses the SAME ``posting_claimed_at`` + ``posting_claim_token`` columns the
    legacy path uses (migration 0005). Atomicity guarantees only one cycle
    process pushes a given event even if two run concurrently — the second
    claimer hits the ``posting_claimed_at IS NULL OR older than 1h`` guard
    and gets zero rowcount back.

    Crash-recovery semantics (spec §1 — accepted rare-duplicate risk):
    if the process dies between (a) claim and (b) writing
    ``discord_push_message_id`` + ``mark_posted``, the claim sits for up to
    1 hour. The next tick's scan sees ``posting_claimed_at`` past the TTL
    and re-pushes. Cosmetic dup, observable via ``/scout retract``.
    """
    now = _now_iso()
    claim_available = _posting_claim_available_sql(conn)
    cur = conn.execute(
        f"""UPDATE events
            SET posting_claimed_at = ?,
                posting_claim_token = ?
            WHERE id = ?
              AND discord_push_message_id IS NULL
              AND {claim_available}""",
        (now, token, event_id),
    )
    if not cur.rowcount:
        return None
    return ScoutClaim(event_id=event_id, token=token)


def set_discord_push_message_id(
    conn: sqlite3.Connection | CompatConnection,
    event_id: int,
    message_id: str,
) -> None:
    """Write Discord's ``?wait=true`` response id back to the event row.

    Standalone helper for paths that record a message id WITHOUT marking
    the row posted (currently: nothing in production — the scout-bot push
    path folds this column into ``mark_posted``'s atomic UPDATE).
    Retained so test fixtures and future read-only sweeps can backfill
    the column without touching ``posted_at``.
    """
    conn.execute(
        """UPDATE events
           SET discord_push_message_id = ?
           WHERE id = ?""",
        (message_id, event_id),
    )


def fetch_events_for_time_window(
    conn: sqlite3.Connection | CompatConnection,
    *,
    start: datetime,
    end: datetime,
    limit: int = 3,
) -> list[sqlite3.Row | Any]:
    """Return up to ``limit`` scored events whose ``start_at`` falls in ``[start, end]``.

    Backs the ``/today`` and ``/week`` slash commands (spec §3, Task 4).
    Differs from the cycle's scan query in TWO ways:

    1. Returns ANY scored event in the window — pushed or not. Both
       commands are human-driven discovery; whether the event was already
       posted to #scout is irrelevant.
    2. Filters only on ``fit_score IS NOT NULL`` (i.e., scoring succeeded).
       The renderer in ``api/integrations/discord/bot.py`` may apply a
       further quality floor before display.

    Ordered by ``fit_score DESC, start_at ASC`` then ``id DESC`` — the
    same shape ``claim_postable_for_scout`` uses, so behavior is predictable.
    """
    clamped_limit = max(1, min(25, int(limit)))
    rows = visible_occurrences(catalog_rows(conn), since=start, until=end, scored_only=True)
    return sorted(rows, key=lambda row: (
        -float(row["fit_score"]), utc_instant(row["start_at"]), -int(row["id"]),
    ))[:clamped_limit]


def fetch_catalog_updated_at(
    conn: sqlite3.Connection | CompatConnection,
) -> str | None:
    """Return the newest source observation timestamp in the event catalog."""
    row = conn.execute("SELECT MAX(last_seen) AS updated_at FROM events").fetchone()
    if row is None:
        return None
    value = row["updated_at"]
    return str(value) if value is not None else None


def link_discord_user(
    conn: sqlite3.Connection | CompatConnection,
    *,
    email: str,
    discord_user_id: str,
) -> int:
    """Write ``users.discord_user_id`` for the friend matching ``email``.

    Returns the number of rows updated (0 if email not in DB or user
    inactive). Idempotent — re-linking the same pair is a no-op write.
    The migration 0006 unique partial index enforces one-to-one mapping
    between Discord ids and friend rows; collisions raise IntegrityError
    so callers should catch and report a friendly message.
    """
    normalized = normalize_email(email)
    value = discord_user_id.strip()
    if not value:
        raise ValueError("discord_user_id must not be blank")
    cur = conn.execute(
        f"""UPDATE users
            SET discord_user_id = ?,
                updated_at = {_now_sql(conn)}
            WHERE email = ?
              AND is_active = ?""",
        (value, normalized, _bool(conn, True)),
    )
    return int(cur.rowcount)


def mark_event_retracted(
    conn: sqlite3.Connection | CompatConnection,
    event_id: int,
    *,
    reason: str = "retracted_by_admin",
) -> int:
    """Mark an event row as retracted-by-admin (Task 4 /scout retract).

    MVP reuses ``scoring_failed_reason`` rather than adding a new column
    (per Task 4 spec). Sets ``scoring_status = 'failed'`` so the scan
    query for #scout pushes (``scoring_status = 'scored'``) skips this
    event going forward, and a triage dump shows the reason.

    Behavioral side effects (documented per code-review I-1):
      - Friend web UI hides the event. ``fetch_events_for_user`` filters
        on ``scoring_status = 'scored'``, so flipping to ``'failed'``
        removes the retracted event from the friend's list. This is
        INTENTIONAL — once Yang has called something a bad pick, we
        should not surface it again.
      - Scout-bot re-push is also blocked. ``fetch_top_unpushed_future_event``
        filters on ``scoring_status = 'scored'`` AND ``cancelled_at IS NULL``,
        so a retracted event will not pick up another claim.

    Resurrection edge (documented per code-review I-2): if the scraper
    later observes the same URL with a different title/start_at/venue,
    ``upsert_raw`` recomputes ``fit_input_hash`` and, on mismatch, resets
    ``scoring_status='pending'`` and clears ``scoring_failed_reason``.
    The retract is effectively erased and the event becomes eligible to
    re-appear in ``/today`` / ``/week`` once it re-scores. The next
    scout-bot push attempt still fails because ``discord_push_message_id``
    remains set, but the entry resurfaces in the human-facing listings.

    A proper fix is a dedicated ``retracted_at`` column (or a sticky
    ``'retracted'`` value in ``scoring_status`` that ``upsert_raw``
    preserves) — tracked under deferred items per spec §7.

    Returns rowcount so the caller can detect a missing event id.
    """
    cur = conn.execute(
        """UPDATE events
           SET scoring_status = 'failed',
               scoring_failed_reason = ?
           WHERE id = ?""",
        (reason, event_id),
    )
    return int(cur.rowcount)


CAPACITY_WARNING_THRESHOLDS = (50, 25, 10)
"""Capacity-ticker threshold buckets (spec §4).

Mirrors ``api.integrations.discord.webhook.CAPACITY_WARNING_THRESHOLDS``;
kept here so the repository layer can validate ``threshold`` arguments
without importing the renderer. Highest-first.
"""

_CAP_FIRED_COLUMNS = {50: "cap_50_fired", 25: "cap_25_fired", 10: "cap_10_fired"}


def fetch_active_pushed_events(
    conn: sqlite3.Connection | CompatConnection,
    *,
    limit: int = 100,
) -> list[sqlite3.Row | Any]:
    """Return events the capacity poller should refresh this hour.

    Spec §4 scan filter:
      - ``discord_push_message_id IS NOT NULL`` — we have a message id to
        PATCH against. Without one there's nothing to edit.
      - ``start_at > now()`` — still future. Past events drop out of the
        scan naturally (terminal state per spec §4 step 6).
      - ``cancelled_at IS NULL`` — single-fire cancellation; once we've
        PATCHed the apology, the row stays out of the scan forever.
      - ``scoring_status = 'scored'`` — admin retracts live in ``'failed'``
        (see ``mark_event_retracted``); without this filter the next poll
        PATCHes the apology message and overwrites the strikethrough (Z1
        regression).

    Ordered by ``start_at`` so the earliest event refreshes first — the
    closer the event, the more urgent the capacity signal.

    ``limit`` defaults to 100 (a reasonable batch for the hourly tick on
    a single-channel MVP). Bounded between 1 and 1000 to prevent a tester
    from issuing an unbounded scan.
    """
    clamped_limit = max(1, min(1000, int(limit)))
    return conn.execute(
        f"""SELECT * FROM events
            WHERE discord_push_message_id IS NOT NULL
              AND cancelled_at IS NULL
              AND scoring_status = 'scored'
              AND start_at IS NOT NULL
              AND {_future_start_at_sql(conn)}
            ORDER BY start_at ASC, id ASC
            LIMIT ?""",
        (clamped_limit,),
    ).fetchall()


def mark_capacity_threshold_fired(
    conn: sqlite3.Connection | CompatConnection,
    event_id: int,
    threshold: int,
) -> None:
    """Set ``cap_<threshold>_fired = 1`` for ``event_id``.

    Called by the capacity poller ONLY after the corresponding Discord
    PATCH returned 2xx. The PATCH-first ordering is the idempotency
    contract (spec §4): if the process dies between PATCH and this write,
    the next poll re-fires the same threshold and the channel sees a
    cosmetic duplicate edit. Accepted MVP risk; better than the inverse
    (write-first) which would silently swallow a real warning when the
    network drops at the wrong moment.

    ``threshold`` must be one of {50, 25, 10}; any other value is a bug
    (callers should use ``CAPACITY_WARNING_THRESHOLDS``) and raises.
    """
    column = _CAP_FIRED_COLUMNS.get(threshold)
    if column is None:
        raise ValueError(
            f"threshold must be one of {sorted(_CAP_FIRED_COLUMNS)}, got {threshold!r}"
        )
    # Static SQL — the column name comes from a fixed whitelist (no
    # string interpolation of caller-controlled data) so this is safe
    # against SQL injection.
    conn.execute(
        f"UPDATE events SET {column} = 1 WHERE id = ?",
        (event_id,),
    )


def mark_event_cancelled(
    conn: sqlite3.Connection | CompatConnection,
    event_id: int,
) -> None:
    """Set ``cancelled_at = now()`` on an event.

    Called by the capacity poller after a successful cancellation PATCH.
    Single-fire: ``fetch_active_pushed_events`` filters on
    ``cancelled_at IS NULL`` so subsequent polls skip the row.
    """
    conn.execute(
        "UPDATE events SET cancelled_at = ? WHERE id = ?",
        (_now_iso(), event_id),
    )


def update_last_known_capacity(
    conn: sqlite3.Connection | CompatConnection,
    event_id: int,
    capacity: int,
) -> None:
    """Write the latest observed capacity to ``last_known_capacity``.

    Always run AFTER the poll cycle's PATCH(es) for the event have
    settled — this column is the ``previous`` reference for the next
    poll's threshold-crossing detection. Writing it before a PATCH
    succeeds would let a transient PATCH failure silently lose the
    crossing on the next tick.
    """
    conn.execute(
        "UPDATE events SET last_known_capacity = ? WHERE id = ?",
        (int(capacity), event_id),
    )


def compute_friends_going(
    conn: sqlite3.Connection | CompatConnection,
    event_id: int,
) -> list[str]:
    """Return display names of friends in the friend-signal allowlist.

    See ``FRIEND_SIGNAL_STATUSES`` — only commitment states count, not
    curiosity (``saved`` / ``interested``). Order: alphabetical by display
    name so renders are deterministic for golden tests.

    Friend data must never appear in INFO+ logs. The renderer collapses
    2+ names to a count for in-channel privacy (see ``_friend_signal_line``).
    """
    placeholders = ", ".join("?" for _ in FRIEND_SIGNAL_STATUSES)
    rows = conn.execute(
        f"""SELECT u.display_name
            FROM user_event_states s
            JOIN users u ON u.id = s.user_id
            WHERE s.event_id = ?
              AND s.status IN ({placeholders})
              AND u.is_active = ?
            ORDER BY u.display_name ASC""",
        (event_id, *FRIEND_SIGNAL_STATUSES, _bool(conn, True)),
    ).fetchall()
    return [str(r["display_name"]) for r in rows]


__all__ = [
    "CAPACITY_WARNING_THRESHOLDS",
    "DB_PATH",
    "DEFAULT_VISIBLE_STATUSES",
    "FRIEND_SIGNAL_STATUSES",
    "SCOUT_METRIC_KINDS",
    "USER_EVENT_RATINGS",
    "USER_EVENT_STATUSES",
    "ScoutClaim",
    "claim_postable_for_scout",
    "cleanup_posted_events",
    "clear_runtime_marker",
    "clear_user_event_state",
    "compute_fit_input_hash",
    "compute_friends_going",
    "connect",
    "count_requeueable_scoring_failures",
    "create_alpha_invite",
    "create_database",
    "create_or_accept_google_user",
    "create_or_accept_supabase_user",
    "create_user",
    "deactivate_user",
    "ensure_user_event_state",
    "event_counts",
    "event_exists",
    "fetch_active_pushed_events",
    "fetch_events_for_time_window",
    "fetch_events_for_user",
    "fetch_pending_scoring",
    "fetch_top_unpushed_future_event",
    "fetch_top_unpushed_future_score",
    "find_active_alpha_invite",
    "get_active_user",
    "get_event_source_url",
    "get_previous_user_status",
    "get_user_by_identity",
    "get_user_by_invite_code",
    "hash_invite_code",
    "is_runtime_marked",
    "link_discord_user",
    "link_identity",
    "list_alpha_invites",
    "list_sources",
    "list_users",
    "load_cycle_health_row",
    "mark_capacity_threshold_fired",
    "mark_event_cancelled",
    "mark_event_retracted",
    "mark_posted",
    "mark_scoring_failed",
    "normalize_email",
    "open_database",
    "release_cycle_lock",
    "release_posting_claim",
    "requeue_scoring_failures",
    "revoke_alpha_invite",
    "rotate_user_invite",
    "runtime_marker_updated_at",
    "save_cycle_health_row",
    "set_discord_push_message_id",
    "set_user_event_rating",
    "set_user_event_status",
    "try_acquire_cycle_lock",
    "update_fit",
    "update_last_known_capacity",
    "upsert_raw",
    "write_runtime_marker",
    "write_scout_metric",
]
