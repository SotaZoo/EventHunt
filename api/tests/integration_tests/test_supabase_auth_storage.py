from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api.repositories.auth_transactions import (
    bind_auth_transaction_subject,
    consume_auth_transaction,
    create_auth_transaction,
)
from api.repositories.events import connect
from api.repositories.identities import (
    create_or_accept_supabase_user,
    get_user_by_identity,
)
from api.repositories.invites import create_alpha_invite, revoke_alpha_invite
from api.repositories.migrate import apply_pending
from api.repositories.users import deactivate_user

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "events.db")
    apply_pending(connection, MIG_DIR)
    return connection


def test_auth_transaction_is_one_time_and_bound_to_browser_cookie(
    conn: sqlite3.Connection,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    created = create_auth_transaction(
        conn,
        state="state-1",
        flow_cookie="browser-secret-1",
        pkce_verifier="pkce-verifier-1",
        purpose="login",
        expires_at=now + timedelta(minutes=10),
    )

    assert created["flow_cookie_hash"] != "browser-secret-1"
    assert (
        consume_auth_transaction(
            conn,
            state="state-1",
            flow_cookie="wrong-browser-secret",
            now=now,
        )
        is None
    )

    consumed = consume_auth_transaction(
        conn,
        state="state-1",
        flow_cookie="browser-secret-1",
        now=now,
    )

    assert consumed is not None
    assert consumed["pkce_verifier"] == "pkce-verifier-1"
    assert consumed["consumed_at"] is not None
    assert (
        consume_auth_transaction(
            conn,
            state="state-1",
            flow_cookie="browser-secret-1",
            now=now,
        )
        is None
    )


def test_auth_transaction_rejects_expired_record(conn: sqlite3.Connection) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    create_auth_transaction(
        conn,
        state="expired-state",
        flow_cookie="expired-cookie",
        pkce_verifier="expired-verifier",
        purpose="login",
        expires_at=now - timedelta(seconds=1),
    )

    assert (
        consume_auth_transaction(
            conn,
            state="expired-state",
            flow_cookie="expired-cookie",
            now=now,
        )
        is None
    )


def test_creating_auth_transaction_removes_expired_records(
    conn: sqlite3.Connection,
) -> None:
    now = datetime.now(UTC)
    create_auth_transaction(
        conn,
        state="expired-state",
        flow_cookie="expired-cookie",
        pkce_verifier="expired-verifier",
        purpose="login",
        expires_at=now - timedelta(seconds=1),
    )

    create_auth_transaction(
        conn,
        state="live-state",
        flow_cookie="live-cookie",
        pkce_verifier="live-verifier",
        purpose="login",
        expires_at=now + timedelta(minutes=10),
    )

    rows = conn.execute("SELECT state FROM auth_transactions ORDER BY state").fetchall()
    assert [row["state"] for row in rows] == ["live-state"]


def test_concurrent_consent_transactions_remain_independent(
    conn: sqlite3.Connection,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    for suffix in ("a", "b"):
        create_auth_transaction(
            conn,
            state=f"state-{suffix}",
            flow_cookie=f"cookie-{suffix}",
            pkce_verifier=f"verifier-{suffix}",
            purpose="consent",
            authorization_id=f"authorization-{suffix}",
            expires_at=now + timedelta(minutes=10),
        )

    first = consume_auth_transaction(
        conn,
        state="state-a",
        flow_cookie="cookie-a",
        now=now,
    )
    second = consume_auth_transaction(
        conn,
        state="state-b",
        flow_cookie="cookie-b",
        now=now,
    )

    assert first["authorization_id"] == "authorization-a"
    assert second["authorization_id"] == "authorization-b"


def test_pending_consent_binds_to_one_supabase_subject(conn: sqlite3.Connection) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    create_auth_transaction(
        conn,
        state="state-consent",
        flow_cookie="cookie-consent",
        pkce_verifier="verifier-consent",
        purpose="consent",
        authorization_id="authorization-1",
        expires_at=now + timedelta(minutes=10),
    )

    bound = bind_auth_transaction_subject(conn, state="state-consent", subject="subject-1")
    same = bind_auth_transaction_subject(conn, state="state-consent", subject="subject-1")

    assert bound["subject"] == "subject-1"
    assert same["subject"] == "subject-1"
    with pytest.raises(ValueError, match="different subject"):
        bind_auth_transaction_subject(conn, state="state-consent", subject="subject-2")


def test_supabase_identity_requires_verified_unrevoked_invite(
    conn: sqlite3.Connection,
) -> None:
    assert (
        create_or_accept_supabase_user(
            conn,
            subject="supabase-sub-unverified",
            email="friend@example.com",
            email_verified=False,
            display_name="Friend",
        )
        is None
    )

    create_alpha_invite(conn, email="revoked@example.com")
    revoke_alpha_invite(conn, "revoked@example.com")
    assert (
        create_or_accept_supabase_user(
            conn,
            subject="supabase-sub-revoked",
            email="revoked@example.com",
            email_verified=True,
            display_name="Revoked",
        )
        is None
    )


def test_supabase_identity_links_active_invited_user_and_never_reactivates(
    conn: sqlite3.Connection,
) -> None:
    create_alpha_invite(conn, email="friend@example.com")
    user = create_or_accept_supabase_user(
        conn,
        subject="supabase-sub-1",
        email="Friend@Example.com",
        email_verified=True,
        display_name="Friend",
    )

    assert user is not None
    assert (
        get_user_by_identity(
            conn,
            provider="supabase",
            subject="supabase-sub-1",
        )["id"]
        == user["id"]
    )

    deactivate_user(conn, "friend@example.com")
    create_alpha_invite(conn, email="friend@example.com")
    assert (
        create_or_accept_supabase_user(
            conn,
            subject="supabase-sub-2",
            email="friend@example.com",
            email_verified=True,
            display_name="Friend",
        )
        is None
    )

    stored = conn.execute("SELECT is_active FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert stored["is_active"] == 0
