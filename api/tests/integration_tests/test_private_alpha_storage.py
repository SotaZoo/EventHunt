from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api.models.events import RawEvent
from api.repositories.event_states import (
    fetch_events_for_user,
    set_user_event_rating,
    set_user_event_status,
)
from api.repositories.events import (
    connect,
    list_sources,
    update_fit,
    upsert_raw,
)
from api.repositories.identities import (
    create_or_accept_google_user,
    get_user_by_identity,
    link_identity,
)
from api.repositories.invites import (
    create_alpha_invite,
    find_active_alpha_invite,
    hash_invite_code,
    list_alpha_invites,
    revoke_alpha_invite,
)
from api.repositories.migrate import apply_pending
from api.repositories.users import (
    create_user,
    get_user_by_invite_code,
    normalize_email,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "events.db"
    c = connect(db)
    apply_pending(c, MIG_DIR)
    return c


def _raw(url: str, title: str) -> RawEvent:
    return RawEvent(
        source="luma",
        url=url,
        title=title,
        starts_at=datetime(2035, 5, 1, 18, 0, tzinfo=UTC),
        venue="Cerebral Valley",
        neighborhood=None,
        description=None,
        host=None,
        cover_image=None,
    )


def _scored_event(conn: sqlite3.Connection, url: str, title: str, score: float) -> int:
    event_id = upsert_raw(conn, _raw(url, title))
    update_fit(
        conn,
        event_id,
        fit_score=score,
        reasoning="good fit",
        urgency="medium",
        urgency_reason=None,
        prompt_version="v1",
        taste_sha="taste",
    )
    return event_id


def test_private_alpha_migration_adds_user_tables(conn: sqlite3.Connection) -> None:
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    assert {"users", "user_event_states"}.issubset(tables)


def test_0004_migration_preserves_users_and_event_state(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    for path in sorted(MIG_DIR.glob("000[0-3]_*.sql")):
        (mig_dir / path.name).write_text(path.read_text())
    c = connect(db)
    apply_pending(c, mig_dir)
    user = create_user(
        c,
        email="yang@example.com",
        display_name="Yang",
        invite_code="yang-secret",
    )
    # This historical migration test uses the pre-0004 schema, not today's
    # repository API (which writes 0006 extraction and 0010 attendance fields).
    event_id = c.execute(
        "INSERT INTO events (source, url, title) VALUES (?, ?, ?)",
        ("luma", "https://lu.ma/migrate", "Migrated Event"),
    ).lastrowid
    c.execute(
        """UPDATE events SET
            fit_score=?, fit_reasoning=?, urgency=?, urgency_reason=?,
            scoring_status='scored', scoring_failed_reason=NULL,
            prompt_version=?, taste_sha=?
          WHERE id=?""",
        (0.91, "good fit", "medium", None, "v1", "taste", event_id),
    )
    set_user_event_status(c, user["id"], event_id, "saved")
    set_user_event_rating(c, user["id"], event_id, 1)
    invite_hash = hash_invite_code("yang-secret")

    migration_0004 = MIG_DIR / "0004_google_alpha_auth.sql"
    (mig_dir / migration_0004.name).write_text(migration_0004.read_text())
    applied = apply_pending(c, mig_dir)

    assert applied == ["0004_google_alpha_auth.sql"]
    row = c.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    state = c.execute(
        """SELECT * FROM user_event_states
           WHERE user_id = ?
             AND event_id = ?""",
        (user["id"], event_id),
    ).fetchone()
    assert row["email"] == "yang@example.com"
    assert row["invite_code_hash"] == invite_hash
    assert row["is_active"] == 1
    assert state["status"] == "saved"
    assert state["rating"] == 1
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_hash_invite_code_is_stable_and_not_plaintext() -> None:
    first = hash_invite_code("alpha-secret")
    second = hash_invite_code("alpha-secret")

    assert first == second
    assert first != "alpha-secret"
    assert len(first) == 64


def test_create_user_stores_hashed_invite_and_loads_active_user(
    conn: sqlite3.Connection,
) -> None:
    user = create_user(
        conn,
        email="yang@example.com",
        display_name="Yang",
        invite_code="alpha-secret",
        interest_flags={"ai": True, "founders": True},
    )

    row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert row["invite_code_hash"] == hash_invite_code("alpha-secret")
    assert row["invite_code_hash"] != "alpha-secret"
    assert get_user_by_invite_code(conn, "alpha-secret")["id"] == user["id"]


def test_google_only_users_can_have_no_invite_hash(conn: sqlite3.Connection) -> None:
    first = create_user(
        conn,
        email="friend@example.com",
        display_name="Friend",
        invite_code=None,
    )
    second = create_user(
        conn,
        email="other@example.com",
        display_name="Other",
        invite_code=None,
    )

    rows = conn.execute(
        "SELECT invite_code_hash FROM users WHERE id IN (?, ?) ORDER BY id",
        (first["id"], second["id"]),
    ).fetchall()

    assert [row["invite_code_hash"] for row in rows] == [None, None]


def test_get_user_by_invite_code_rejects_inactive_users(conn: sqlite3.Connection) -> None:
    user = create_user(
        conn,
        email="friend@example.com",
        display_name="Friend",
        invite_code="friend-secret",
    )

    conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user["id"],))

    assert get_user_by_invite_code(conn, "friend-secret") is None


def test_alpha_invite_lifecycle_is_email_normalized(conn: sqlite3.Connection) -> None:
    invite = create_alpha_invite(conn, email=" Friend@Example.COM ")
    duplicate = create_alpha_invite(conn, email="friend@example.com")

    assert invite["id"] == duplicate["id"]
    assert invite["normalized_email"] == "friend@example.com"
    assert find_active_alpha_invite(conn, "FRIEND@example.com")["id"] == invite["id"]
    assert [row["normalized_email"] for row in list_alpha_invites(conn)] == [
        "friend@example.com"
    ]


def test_google_identity_accepts_invite_and_reuses_linked_user(
    conn: sqlite3.Connection,
) -> None:
    create_alpha_invite(conn, email="friend@example.com")

    user = create_or_accept_google_user(
        conn,
        subject="google-sub-1",
        email="Friend@Example.com",
        email_verified=True,
        display_name="Friend",
    )
    again = create_or_accept_google_user(
        conn,
        subject="google-sub-1",
        email="friend@example.com",
        email_verified=True,
        display_name="Changed",
    )

    assert user is not None
    assert again is not None
    assert again["id"] == user["id"]
    assert get_user_by_identity(conn, provider="google", subject="google-sub-1")["id"] == user["id"]
    invite = find_active_alpha_invite(conn, "friend@example.com")
    assert invite["accepted_user_id"] == user["id"]


def test_existing_google_identity_claims_a_fresh_unclaimed_invite(
    conn: sqlite3.Connection,
) -> None:
    existing = create_user(
        conn,
        email="friend@example.com",
        display_name="Friend",
        invite_code=None,
    )
    link_identity(
        conn,
        provider="google",
        subject="google-sub-existing",
        user_id=existing["id"],
        email="friend@example.com",
        email_verified=True,
    )
    create_alpha_invite(conn, email="friend@example.com")

    accepted = create_or_accept_google_user(
        conn,
        subject="google-sub-existing",
        email="friend@example.com",
        email_verified=True,
        display_name="Friend",
    )

    assert accepted is not None
    assert accepted["id"] == existing["id"]
    invite = find_active_alpha_invite(conn, "friend@example.com")
    assert invite["accepted_user_id"] == existing["id"]


def test_existing_google_identity_cannot_take_another_users_invite(
    conn: sqlite3.Connection,
) -> None:
    create_alpha_invite(conn, email="shared@example.com")
    owner = create_or_accept_google_user(
        conn,
        subject="google-sub-owner",
        email="shared@example.com",
        email_verified=True,
        display_name="Owner",
    )
    existing = create_user(
        conn,
        email="existing@example.com",
        display_name="Existing",
        invite_code=None,
    )
    link_identity(
        conn,
        provider="google",
        subject="google-sub-existing",
        user_id=existing["id"],
        email="shared@example.com",
        email_verified=True,
    )

    rejected = create_or_accept_google_user(
        conn,
        subject="google-sub-existing",
        email="shared@example.com",
        email_verified=True,
        display_name="Existing",
    )

    assert owner is not None
    assert rejected is None
    invite = find_active_alpha_invite(conn, "shared@example.com")
    assert invite["accepted_user_id"] == owner["id"]


def test_google_identity_requires_verified_allowlisted_email(
    conn: sqlite3.Connection,
) -> None:
    assert (
        create_or_accept_google_user(
            conn,
            subject="google-sub-1",
            email="friend@example.com",
            email_verified=False,
            display_name="Friend",
        )
        is None
    )
    uninvited = create_or_accept_google_user(
        conn,
        subject="google-sub-2",
        email="stranger@example.com",
        email_verified=True,
        display_name="Stranger",
    )

    assert uninvited is None
    assert get_user_by_identity(conn, provider="google", subject="google-sub-2") is None


def test_revoke_alpha_invite_deactivates_accepted_user_and_blocks_identity(
    conn: sqlite3.Connection,
) -> None:
    create_alpha_invite(conn, email="friend@example.com")
    user = create_or_accept_google_user(
        conn,
        subject="google-sub-1",
        email="friend@example.com",
        email_verified=True,
        display_name="Friend",
    )

    revoked = revoke_alpha_invite(conn, "friend@example.com")

    assert user is not None
    assert revoked is not None
    assert revoked["revoked_at"] is not None
    assert get_user_by_identity(conn, provider="google", subject="google-sub-1") is None
    row = conn.execute("SELECT is_active FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert row["is_active"] == 0


def test_reinvite_reactivates_existing_user_without_duplicate(
    conn: sqlite3.Connection,
) -> None:
    create_alpha_invite(conn, email="friend@example.com")
    user = create_or_accept_google_user(
        conn,
        subject="google-sub-1",
        email="friend@example.com",
        email_verified=True,
        display_name="Friend",
    )
    assert user is not None
    revoke_alpha_invite(conn, "friend@example.com")
    create_alpha_invite(conn, email="friend@example.com")

    reactivated = create_or_accept_google_user(
        conn,
        subject="google-sub-2",
        email="friend@example.com",
        email_verified=True,
        display_name="Friend",
    )

    assert reactivated is not None
    assert reactivated["id"] == user["id"]
    count = conn.execute(
        "SELECT count(*) FROM users WHERE email = ?",
        ("friend@example.com",),
    ).fetchone()[0]
    assert count == 1
    assert reactivated["is_active"] == 1


def test_link_identity_rejects_subject_reassignment(conn: sqlite3.Connection) -> None:
    first = create_user(
        conn,
        email="first@example.com",
        display_name="First",
        invite_code=None,
    )
    second = create_user(
        conn,
        email="second@example.com",
        display_name="Second",
        invite_code=None,
    )
    link_identity(
        conn,
        provider="google",
        subject="shared-sub",
        user_id=first["id"],
        email="first@example.com",
        email_verified=True,
    )

    with pytest.raises(ValueError, match="another user"):
        link_identity(
            conn,
            provider="google",
            subject="shared-sub",
            user_id=second["id"],
            email="second@example.com",
            email_verified=True,
        )


def test_normalize_email_lowercases_and_strips() -> None:
    assert normalize_email(" Yang@Example.COM ") == "yang@example.com"


def test_user_event_status_is_scoped_by_user(conn: sqlite3.Connection) -> None:
    yang = create_user(
        conn,
        email="yang@example.com",
        display_name="Yang",
        invite_code="yang-secret",
    )
    friend = create_user(
        conn,
        email="friend@example.com",
        display_name="Friend",
        invite_code="friend-secret",
    )
    event_id = _scored_event(conn, "https://lu.ma/a", "AI Infra", 0.91)

    set_user_event_status(conn, yang["id"], event_id, "saved")

    assert fetch_events_for_user(conn, yang["id"])[0]["user_status"] == "saved"
    assert fetch_events_for_user(conn, friend["id"])[0]["user_status"] == "new"


def test_user_event_status_and_rating_validate_allowed_values(
    conn: sqlite3.Connection,
) -> None:
    user = create_user(
        conn,
        email="yang@example.com",
        display_name="Yang",
        invite_code="yang-secret",
    )
    event_id = _scored_event(conn, "https://lu.ma/a", "AI Infra", 0.91)

    with pytest.raises(ValueError):
        set_user_event_status(conn, user["id"], event_id, "maybe")

    with pytest.raises(ValueError):
        set_user_event_rating(conn, user["id"], event_id, 2)

    set_user_event_status(conn, user["id"], event_id, "applied")
    assert fetch_events_for_user(conn, user["id"])[0]["user_status"] == "applied"


def test_set_user_event_rating_accepts_signal_values(conn: sqlite3.Connection) -> None:
    user = create_user(
        conn,
        email="yang@example.com",
        display_name="Yang",
        invite_code="yang-secret",
    )
    event_id = _scored_event(conn, "https://lu.ma/a", "AI Infra", 0.91)

    set_user_event_rating(conn, user["id"], event_id, 1)
    assert fetch_events_for_user(conn, user["id"])[0]["user_rating"] == 1

    set_user_event_rating(conn, user["id"], event_id, None)
    assert fetch_events_for_user(conn, user["id"])[0]["user_rating"] is None


def test_fetch_events_for_user_default_hides_dismissed_and_rejected(
    conn: sqlite3.Connection,
) -> None:
    user = create_user(
        conn,
        email="yang@example.com",
        display_name="Yang",
        invite_code="yang-secret",
    )
    new_event = _scored_event(conn, "https://lu.ma/new", "New", 0.95)
    saved_event = _scored_event(conn, "https://lu.ma/saved", "Saved", 0.9)
    dismissed_event = _scored_event(conn, "https://lu.ma/no", "No", 0.88)
    rejected_event = _scored_event(conn, "https://lu.ma/rejected", "Rejected", 0.86)
    set_user_event_status(conn, user["id"], saved_event, "saved")
    set_user_event_status(conn, user["id"], dismissed_event, "dismissed")
    set_user_event_status(conn, user["id"], rejected_event, "rejected")

    rows = fetch_events_for_user(conn, user["id"])

    assert {r["id"] for r in rows} == {new_event, saved_event}
    assert {r["user_status"] for r in rows} == {"new", "saved"}


@pytest.mark.parametrize("status", [None, "all"])
def test_fetch_events_for_user_hides_cancelled_events(
    conn: sqlite3.Connection, status: str | None
) -> None:
    user = create_user(
        conn,
        email="yang@example.com",
        display_name="Yang",
        invite_code="yang-secret",
    )
    active_event = _scored_event(conn, "https://lu.ma/active", "Active", 0.9)
    cancelled_event = _scored_event(conn, "https://lu.ma/cancelled", "Cancelled", 0.95)
    conn.execute(
        "UPDATE events SET cancelled_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), cancelled_event),
    )

    rows = fetch_events_for_user(conn, user["id"], status=status)

    assert [row["id"] for row in rows] == [active_event]


def test_list_sources_hides_sources_with_only_cancelled_events(
    conn: sqlite3.Connection,
) -> None:
    _scored_event(conn, "https://lu.ma/active", "Active", 0.9)
    cancelled_event = _scored_event(conn, "https://lu.ma/cancelled", "Cancelled", 0.95)
    conn.execute(
        "UPDATE events SET source = ?, cancelled_at = ? WHERE id = ?",
        ("fixture", datetime.now(UTC).isoformat(), cancelled_event),
    )

    assert list_sources(conn) == ["luma"]


def test_fetch_events_for_user_status_new_includes_missing_state(
    conn: sqlite3.Connection,
) -> None:
    user = create_user(
        conn,
        email="yang@example.com",
        display_name="Yang",
        invite_code="yang-secret",
    )
    new_event = _scored_event(conn, "https://lu.ma/new", "New", 0.95)
    saved_event = _scored_event(conn, "https://lu.ma/saved", "Saved", 0.9)
    set_user_event_status(conn, user["id"], saved_event, "saved")

    rows = fetch_events_for_user(conn, user["id"], status="new")

    assert [r["id"] for r in rows] == [new_event]
    assert rows[0]["user_status"] == "new"


def test_fetch_events_for_user_status_all_includes_hidden_states(
    conn: sqlite3.Connection,
) -> None:
    user = create_user(
        conn,
        email="yang@example.com",
        display_name="Yang",
        invite_code="yang-secret",
    )
    saved_event = _scored_event(conn, "https://lu.ma/saved", "Saved", 0.9)
    rejected_event = _scored_event(conn, "https://lu.ma/rejected", "Rejected", 0.86)
    set_user_event_status(conn, user["id"], saved_event, "saved")
    set_user_event_status(conn, user["id"], rejected_event, "rejected")

    rows = fetch_events_for_user(conn, user["id"], status="all")

    assert {r["id"] for r in rows} == {saved_event, rejected_event}


def test_fetch_events_for_user_can_filter_by_min_score(conn: sqlite3.Connection) -> None:
    user = create_user(
        conn,
        email="yang@example.com",
        display_name="Yang",
        invite_code="yang-secret",
    )
    strong = _scored_event(conn, "https://lu.ma/strong", "Strong", 0.95)
    _scored_event(conn, "https://lu.ma/weak", "Weak", 0.60)

    rows = fetch_events_for_user(conn, user["id"], min_score=0.8)

    assert [r["id"] for r in rows] == [strong]


def test_fetch_events_for_user_uses_explicit_start_boundary(
    conn: sqlite3.Connection,
) -> None:
    user = create_user(
        conn,
        email="yang@example.com",
        display_name="Yang",
        invite_code="yang-secret",
    )
    boundary = datetime(2035, 5, 1, 4, 0, tzinfo=UTC)
    earlier_today = RawEvent(
        source="luma",
        url="https://lu.ma/early",
        title="Earlier Today",
        starts_at=boundary + timedelta(hours=1),
        venue="Cerebral Valley",
        neighborhood=None,
        description=None,
        host=None,
        cover_image=None,
    )
    yesterday = RawEvent(
        source="luma",
        url="https://lu.ma/yesterday",
        title="Yesterday",
        starts_at=boundary - timedelta(minutes=1),
        venue="Cerebral Valley",
        neighborhood=None,
        description=None,
        host=None,
        cover_image=None,
    )
    early_id = upsert_raw(conn, earlier_today)
    yesterday_id = upsert_raw(conn, yesterday)
    for event_id in (early_id, yesterday_id):
        update_fit(
            conn,
            event_id,
            fit_score=0.91,
            reasoning="good fit",
            urgency="medium",
            urgency_reason=None,
            prompt_version="v1",
            taste_sha="taste",
        )

    rows = fetch_events_for_user(conn, user["id"], start_at_from=boundary)

    assert [r["id"] for r in rows] == [early_id]
