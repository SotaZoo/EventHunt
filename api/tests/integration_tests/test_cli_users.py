from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import api.repositories._storage as storage
from api.commands.main import app
from api.repositories.events import connect
from api.repositories.users import get_user_by_invite_code

runner = CliRunner()
FRIEND_SECRET = "test-session-secret-" + ("x" * 32)
GOOGLE_CLIENT_ID = "1234567890-alpha.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "GOCSPX-alpha-secret"


def _migrate(db: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(db))
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.stdout


def _invite_from_output(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("Invite code:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(output)


def test_user_add_creates_user_and_prints_one_time_invite(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    _migrate(db, monkeypatch)

    result = runner.invoke(
        app,
        ["user", "add", "--email", "yang@example.com", "--name", "Yang"],
    )

    assert result.exit_code == 0, result.stdout
    invite = _invite_from_output(result.stdout)
    conn = connect(db)
    user = get_user_by_invite_code(conn, invite)
    row = conn.execute("SELECT invite_code_hash FROM users WHERE email = ?", ("yang@example.com",)).fetchone()
    conn.close()
    assert user is not None
    assert invite not in row["invite_code_hash"]


def test_user_add_duplicate_email_exits_cleanly(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    _migrate(db, monkeypatch)
    runner.invoke(app, ["user", "add", "--email", "yang@example.com", "--name", "Yang"])

    result = runner.invoke(
        app,
        ["user", "add", "--email", "yang@example.com", "--name", "Yang 2"],
    )

    assert result.exit_code != 0
    assert "already exists" in result.stdout


def test_user_list_does_not_print_invites(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    _migrate(db, monkeypatch)
    add = runner.invoke(app, ["user", "add", "--email", "yang@example.com", "--name", "Yang"])
    invite = _invite_from_output(add.stdout)

    result = runner.invoke(app, ["user", "list"])

    assert result.exit_code == 0, result.stdout
    assert "yang@example.com" in result.stdout
    assert invite not in result.stdout


def test_user_rotate_invite_invalidates_old_code(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    _migrate(db, monkeypatch)
    add = runner.invoke(app, ["user", "add", "--email", "yang@example.com", "--name", "Yang"])
    old_invite = _invite_from_output(add.stdout)

    rotate = runner.invoke(app, ["user", "rotate-invite", "--email", "yang@example.com"])
    new_invite = _invite_from_output(rotate.stdout)

    conn = connect(db)
    assert get_user_by_invite_code(conn, old_invite) is None
    assert get_user_by_invite_code(conn, new_invite) is not None
    conn.close()


def test_user_deactivate_blocks_future_logins(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    _migrate(db, monkeypatch)
    add = runner.invoke(app, ["user", "add", "--email", "yang@example.com", "--name", "Yang"])
    invite = _invite_from_output(add.stdout)

    result = runner.invoke(app, ["user", "deactivate", "--email", "yang@example.com"])

    assert result.exit_code == 0, result.stdout
    conn = connect(db)
    assert get_user_by_invite_code(conn, invite) is None
    conn.close()


def test_user_invite_google_adds_allowlisted_email(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    _migrate(db, monkeypatch)

    result = runner.invoke(app, ["user", "invite-google", "--email", "Friend@Example.COM"])

    assert result.exit_code == 0, result.stdout
    assert "friend@example.com" in result.stdout
    conn = connect(db)
    invite = storage.find_active_alpha_invite(conn, "friend@example.com")
    conn.close()
    assert invite is not None


def test_user_list_google_invites_does_not_print_secrets(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    _migrate(db, monkeypatch)
    runner.invoke(app, ["user", "invite-google", "--email", "friend@example.com"])

    result = runner.invoke(app, ["user", "list-google-invites"])

    assert result.exit_code == 0, result.stdout
    assert "friend@example.com" in result.stdout
    assert "pending" in result.stdout


def test_user_revoke_google_invite_deactivates_accepted_user(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    _migrate(db, monkeypatch)
    runner.invoke(app, ["user", "invite-google", "--email", "friend@example.com"])
    conn = connect(db)
    user = storage.create_or_accept_google_user(
        conn,
        subject="google-sub-1",
        email="friend@example.com",
        email_verified=True,
        display_name="Friend",
    )
    conn.close()

    result = runner.invoke(app, ["user", "revoke-google-invite", "--email", "friend@example.com"])

    assert result.exit_code == 0, result.stdout
    conn = connect(db)
    assert storage.find_active_alpha_invite(conn, "friend@example.com") is None
    row = conn.execute("SELECT is_active FROM users WHERE id = ?", (user["id"],)).fetchone()
    conn.close()
    assert row["is_active"] == 0


def test_user_revoke_google_invite_is_idempotent(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    _migrate(db, monkeypatch)

    result = runner.invoke(app, ["user", "revoke-google-invite", "--email", "friend@example.com"])

    assert result.exit_code == 0, result.stdout
    assert "No active Google invite" in result.stdout


def test_friend_mode_serve_requires_https_base_url(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("NYC_AI_EVENTS_FRIEND_MODE", "1")
    monkeypatch.setenv("NYC_AI_EVENTS_SESSION_SECRET", FRIEND_SECRET)
    monkeypatch.setenv("NYC_AI_EVENTS_GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID)
    monkeypatch.setenv("NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET", GOOGLE_CLIENT_SECRET)

    result = runner.invoke(app, ["serve", "--host", "127.0.0.1"])

    assert result.exit_code == 2
    assert "NYC_AI_EVENTS_PUBLIC_BASE_URL" in result.stdout


def test_friend_mode_serve_rejects_placeholder_session_secret(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("NYC_AI_EVENTS_FRIEND_MODE", "1")
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.com")
    monkeypatch.setenv("NYC_AI_EVENTS_SESSION_SECRET", "replace-with-openssl-rand-hex-32")
    monkeypatch.setenv("NYC_AI_EVENTS_GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID)
    monkeypatch.setenv("NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET", GOOGLE_CLIENT_SECRET)

    result = runner.invoke(app, ["serve", "--host", "127.0.0.1"])

    assert result.exit_code == 2
    assert "NYC_AI_EVENTS_SESSION_SECRET" in result.stdout
    assert "at least 32" in result.stdout


def test_friend_mode_serve_rejects_placeholder_google_oauth(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("NYC_AI_EVENTS_FRIEND_MODE", "1")
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.com")
    monkeypatch.setenv("NYC_AI_EVENTS_SESSION_SECRET", FRIEND_SECRET)
    monkeypatch.setenv("NYC_AI_EVENTS_GOOGLE_CLIENT_ID", "replace-with-google-oauth-client-id")
    monkeypatch.setenv("NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET", "replace-with-google-oauth-client-secret")

    result = runner.invoke(app, ["serve", "--host", "127.0.0.1"])

    assert result.exit_code == 2
    assert "NYC_AI_EVENTS_GOOGLE_CLIENT_ID" in result.stdout
    assert "NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET" in result.stdout


def test_friend_mode_serve_requires_google_oauth_env(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.setenv("NYC_AI_EVENTS_FRIEND_MODE", "1")
    monkeypatch.setenv("NYC_AI_EVENTS_SESSION_SECRET", FRIEND_SECRET)
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.com")
    monkeypatch.delenv("NYC_AI_EVENTS_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET", raising=False)

    result = runner.invoke(app, ["serve", "--host", "127.0.0.1"])

    assert result.exit_code == 2
    assert "NYC_AI_EVENTS_GOOGLE_CLIENT_ID" in result.stdout


def test_friend_mode_serve_accepts_supabase_oauth(tmp_path, monkeypatch) -> None:
    db = tmp_path / "events.db"
    _migrate(db, monkeypatch)
    monkeypatch.setenv("NYC_AI_EVENTS_FRIEND_MODE", "1")
    monkeypatch.setenv("NYC_AI_EVENTS_SESSION_SECRET", FRIEND_SECRET)
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.com")
    monkeypatch.setenv("NYC_AI_EVENTS_SUPABASE_URL", "https://project-ref.supabase.co")
    monkeypatch.setenv("NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_gate0")
    monkeypatch.delenv("NYC_AI_EVENTS_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET", raising=False)
    started: list[object] = []
    monkeypatch.setattr("uvicorn.run", lambda web_app, **kwargs: started.append(web_app))

    result = runner.invoke(app, ["serve", "--host", "127.0.0.1"])

    assert result.exit_code == 0, result.stdout
    assert len(started) == 1
