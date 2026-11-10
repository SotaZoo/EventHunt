"""Durable, one-time browser transactions for Supabase OAuth flows."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from typing import Any, Literal

from api.repositories.database import CompatConnection

AuthPurpose = Literal["login", "consent"]


def _timestamp(conn: Any, value: datetime) -> datetime | str:
    normalized = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if getattr(conn, "dialect", "sqlite") == "postgres":
        return normalized
    return normalized.isoformat(timespec="seconds")


def _flow_cookie_hash(flow_cookie: str) -> str:
    value = flow_cookie.strip()
    if not value:
        raise ValueError("flow_cookie must not be blank")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required(value: str, name: str, *, maximum: int = 255) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum} characters")
    return normalized


def create_auth_transaction(
    conn: sqlite3.Connection | CompatConnection,
    *,
    state: str,
    flow_cookie: str,
    pkce_verifier: str,
    purpose: AuthPurpose,
    expires_at: datetime,
    authorization_id: str | None = None,
) -> sqlite3.Row | Any:
    if purpose not in ("login", "consent"):
        raise ValueError("purpose must be login or consent")
    state = _required(state, "state")
    pkce_verifier = _required(pkce_verifier, "pkce_verifier")
    if authorization_id is not None:
        authorization_id = _required(authorization_id, "authorization_id")
    conn.execute(
        "DELETE FROM auth_transactions WHERE expires_at <= ?",
        (_timestamp(conn, datetime.now(UTC)),),
    )
    conn.execute(
        """INSERT INTO auth_transactions
            (state, flow_cookie_hash, pkce_verifier, purpose, authorization_id, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            state,
            _flow_cookie_hash(flow_cookie),
            pkce_verifier,
            purpose,
            authorization_id,
            _timestamp(conn, expires_at),
        ),
    )
    return conn.execute(
        "SELECT * FROM auth_transactions WHERE state = ?",
        (state,),
    ).fetchone()


def consume_auth_transaction(
    conn: sqlite3.Connection | CompatConnection,
    *,
    state: str,
    flow_cookie: str,
    now: datetime | None = None,
) -> sqlite3.Row | Any | None:
    """Atomically consume a live transaction bound to this browser flow."""
    state = _required(state, "state")
    consumed_at = _timestamp(conn, now or datetime.now(UTC))
    result = conn.execute(
        """UPDATE auth_transactions
           SET consumed_at = ?
           WHERE state = ?
             AND flow_cookie_hash = ?
             AND consumed_at IS NULL
             AND expires_at > ?""",
        (consumed_at, state, _flow_cookie_hash(flow_cookie), consumed_at),
    )
    if result.rowcount != 1:
        return None
    return conn.execute(
        "SELECT * FROM auth_transactions WHERE state = ?",
        (state,),
    ).fetchone()


def bind_auth_transaction_subject(
    conn: sqlite3.Connection | CompatConnection,
    *,
    state: str,
    subject: str,
) -> sqlite3.Row | Any:
    """Bind a pending authorization to one Supabase subject, once."""
    state = _required(state, "state")
    subject = _required(subject, "subject")
    existing = conn.execute(
        "SELECT * FROM auth_transactions WHERE state = ?",
        (state,),
    ).fetchone()
    if existing is None:
        raise ValueError("auth transaction does not exist")
    if existing["subject"] not in (None, subject):
        raise ValueError("auth transaction is bound to a different subject")
    if existing["subject"] is None:
        conn.execute(
            "UPDATE auth_transactions SET subject = ? WHERE state = ? AND subject IS NULL",
            (subject, state),
        )
    return conn.execute(
        "SELECT * FROM auth_transactions WHERE state = ?",
        (state,),
    ).fetchone()
