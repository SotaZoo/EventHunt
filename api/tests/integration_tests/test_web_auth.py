from __future__ import annotations

from starlette.testclient import TestClient

import api.repositories._storage as storage
from api.app import create_app
from api.integrations.google.oauth import GoogleOAuthError, GoogleUserInfo
from api.repositories.events import connect
from api.tests.fixtures.web_helpers import add_user, migrated_db

FRIEND_SECRET = "test-session-secret-" + ("x" * 32)
GOOGLE_CLIENT_ID = "1234567890-alpha.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "GOCSPX-alpha-secret"


class FakeGoogleOAuthClient:
    def __init__(
        self,
        userinfo: GoogleUserInfo | None = None,
        *,
        fail_callback: bool = False,
    ) -> None:
        self.userinfo = userinfo or GoogleUserInfo(
            subject="google-sub-1",
            email="friend@example.com",
            email_verified=True,
            display_name="Friend",
        )
        self.fail_callback = fail_callback
        self.redirect_uri: str | None = None

    async def authorize_redirect(self, request, redirect_uri: str):
        from starlette.responses import RedirectResponse

        self.redirect_uri = redirect_uri
        request.session["state"] = "state-ok"
        return RedirectResponse(
            f"https://accounts.google.com/o/oauth2/v2/auth?state=state-ok&redirect_uri={redirect_uri}",
            status_code=303,
        )

    async def authorize_userinfo(self, request) -> GoogleUserInfo:
        if self.fail_callback or request.query_params.get("state") != request.session.get("state"):
            raise GoogleOAuthError("state mismatch")
        return self.userinfo


def _client(tmp_path, *, google_oauth_client=None):
    db = migrated_db(tmp_path)
    add_user(db, invite_code="yang-secret")
    app = create_app(
        db_path=db,
        session_secret=FRIEND_SECRET,
        google_oauth_client=google_oauth_client,
    )
    return TestClient(app), db


def _login(client: TestClient, invite_code: str = "yang-secret"):
    return client.post("/login", data={"invite_code": invite_code}, follow_redirects=False)


def test_login_page_renders(tmp_path) -> None:
    client, _ = _client(tmp_path)

    response = client.get("/login")

    assert response.status_code == 200
    assert "<title>Sign in — Event Hunt</title>" in response.text
    assert "<h1>Event Hunt</h1>" in response.text
    assert "Google sign-in is not available yet." in response.text
    assert "Local invite code" in response.text
    assert "Log in with Google" not in response.text
    assert "Sign in with your invited Google account." in response.text


def test_favicon_does_not_generate_browser_console_404(tmp_path) -> None:
    client, _ = _client(tmp_path)

    response = client.get("/favicon.ico")

    assert response.status_code == 204


def test_login_page_shows_google_when_configured(tmp_path) -> None:
    client, _ = _client(tmp_path, google_oauth_client=FakeGoogleOAuthClient())

    response = client.get("/login")

    assert response.status_code == 200
    assert "Log in with Google" in response.text
    assert "Sign in with your invited Google account." in response.text


def test_friend_mode_hides_and_rejects_invite_code_login(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NYC_AI_EVENTS_FRIEND_MODE", "1")
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.test")
    client, _ = _client(tmp_path, google_oauth_client=FakeGoogleOAuthClient())

    page = client.get("/login")
    response = client.post(
        "/login",
        data={"invite_code": "yang-secret"},
        follow_redirects=False,
    )

    assert page.status_code == 200
    assert "Log in with Google" in page.text
    assert "Invite code" not in page.text
    assert response.status_code == 403
    assert "disabled" in response.text


def test_create_app_fails_closed_in_friend_mode_without_required_env(
    tmp_path,
    monkeypatch,
) -> None:
    db = migrated_db(tmp_path)
    monkeypatch.setenv("NYC_AI_EVENTS_FRIEND_MODE", "1")
    monkeypatch.delenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("NYC_AI_EVENTS_SESSION_SECRET", raising=False)
    monkeypatch.delenv("NYC_AI_EVENTS_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET", raising=False)

    try:
        create_app(db_path=db)
    except RuntimeError as exc:
        message = str(exc)
    else:  # pragma: no cover - assertion failure path
        raise AssertionError("create_app should fail closed in friend mode")

    assert "NYC_AI_EVENTS_PUBLIC_BASE_URL" in message
    assert "NYC_AI_EVENTS_SESSION_SECRET" in message
    assert "NYC_AI_EVENTS_GOOGLE_CLIENT_ID" in message
    assert "NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET" in message


def test_create_app_rejects_placeholder_friend_mode_session_secret(tmp_path, monkeypatch) -> None:
    db = migrated_db(tmp_path)
    monkeypatch.setenv("NYC_AI_EVENTS_FRIEND_MODE", "1")
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.test")
    monkeypatch.setenv("NYC_AI_EVENTS_SESSION_SECRET", "replace-with-openssl-rand-hex-32")
    monkeypatch.setenv("NYC_AI_EVENTS_GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID)
    monkeypatch.setenv("NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET", GOOGLE_CLIENT_SECRET)

    try:
        create_app(db_path=db, google_oauth_client=FakeGoogleOAuthClient())
    except RuntimeError as exc:
        message = str(exc)
    else:  # pragma: no cover - assertion failure path
        raise AssertionError("create_app should reject placeholder friend-mode secrets")

    assert "NYC_AI_EVENTS_SESSION_SECRET" in message
    assert "at least 32" in message


def test_create_app_rejects_placeholder_friend_mode_public_url_and_google_env(
    tmp_path,
    monkeypatch,
) -> None:
    db = migrated_db(tmp_path)
    monkeypatch.setenv("NYC_AI_EVENTS_FRIEND_MODE", "1")
    monkeypatch.setenv(
        "NYC_AI_EVENTS_PUBLIC_BASE_URL",
        "https://replace-with-approved-https-origin",
    )
    monkeypatch.setenv("NYC_AI_EVENTS_SESSION_SECRET", FRIEND_SECRET)
    monkeypatch.setenv("NYC_AI_EVENTS_GOOGLE_CLIENT_ID", "replace-with-google-oauth-client-id")
    monkeypatch.setenv("NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET", "replace-with-google-oauth-client-secret")

    try:
        create_app(db_path=db)
    except RuntimeError as exc:
        message = str(exc)
    else:  # pragma: no cover - assertion failure path
        raise AssertionError("create_app should reject placeholder friend-mode config")

    assert "NYC_AI_EVENTS_PUBLIC_BASE_URL" in message
    assert "NYC_AI_EVENTS_GOOGLE_CLIENT_ID" in message
    assert "NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET" in message


def test_create_app_accepts_complete_friend_mode_config(tmp_path, monkeypatch) -> None:
    db = migrated_db(tmp_path)
    monkeypatch.setenv("NYC_AI_EVENTS_FRIEND_MODE", "1")
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.test")
    monkeypatch.setenv("NYC_AI_EVENTS_SESSION_SECRET", FRIEND_SECRET)
    monkeypatch.setenv("NYC_AI_EVENTS_GOOGLE_CLIENT_ID", GOOGLE_CLIENT_ID)
    monkeypatch.setenv("NYC_AI_EVENTS_GOOGLE_CLIENT_SECRET", GOOGLE_CLIENT_SECRET)

    app = create_app(db_path=db, google_oauth_client=FakeGoogleOAuthClient())

    assert app.state.invite_login_enabled is False


def test_google_start_requires_configured_client(tmp_path) -> None:
    client, _ = _client(tmp_path)

    response = client.get("/auth/google/start", follow_redirects=False)

    assert response.status_code == 503
    assert "Google sign-in is not configured yet" in response.text


def test_google_start_sets_only_oauth_transaction_cookie(tmp_path) -> None:
    oauth = FakeGoogleOAuthClient()
    client, _ = _client(tmp_path, google_oauth_client=oauth)

    response = client.get("/auth/google/start", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://accounts.google.com/")
    assert "nyc_ai_events_oauth=" in response.headers["set-cookie"]
    assert "nyc_ai_events_session=" not in response.headers["set-cookie"]
    assert oauth.redirect_uri == "http://testserver/auth/google/callback"


def test_google_start_marks_oauth_cookie_secure_in_friend_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NYC_AI_EVENTS_FRIEND_MODE", "1")
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.test")
    client, _ = _client(tmp_path, google_oauth_client=FakeGoogleOAuthClient())

    response = client.get("/auth/google/start", follow_redirects=False)

    assert response.status_code == 303
    assert "nyc_ai_events_oauth=" in response.headers["set-cookie"]
    assert "secure" in response.headers["set-cookie"].lower()


def test_google_start_uses_public_base_url_for_redirect_uri(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.com")
    oauth = FakeGoogleOAuthClient()
    client, _ = _client(tmp_path, google_oauth_client=oauth)

    response = client.get("/auth/google/start", follow_redirects=False)

    assert response.status_code == 303
    assert oauth.redirect_uri == "https://events.example.com/auth/google/callback"


def test_google_callback_accepts_allowlisted_friend(tmp_path) -> None:
    oauth = FakeGoogleOAuthClient()
    client, db = _client(tmp_path, google_oauth_client=oauth)
    conn = connect(db)
    storage.create_alpha_invite(conn, email="friend@example.com")
    conn.close()
    client.get("/auth/google/start", follow_redirects=False)

    response = client.get(
        "/auth/google/callback?code=ok&state=state-ok",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/events"
    assert "nyc_ai_events_session=" in response.headers["set-cookie"]
    conn = connect(db)
    user = storage.get_user_by_identity(conn, provider="google", subject="google-sub-1")
    conn.close()
    assert user is not None
    assert user["email"] == "friend@example.com"


def test_google_callback_marks_session_cookie_secure_in_friend_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NYC_AI_EVENTS_FRIEND_MODE", "1")
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.test")
    oauth = FakeGoogleOAuthClient()
    db = migrated_db(tmp_path)
    add_user(db, invite_code="yang-secret")
    app = create_app(
        db_path=db,
        session_secret=FRIEND_SECRET,
        google_oauth_client=oauth,
    )
    client = TestClient(app, base_url="https://events.example.test")
    conn = connect(db)
    storage.create_alpha_invite(conn, email="friend@example.com")
    conn.close()
    client.get("/auth/google/start", follow_redirects=False)

    response = client.get(
        "/auth/google/callback?code=ok&state=state-ok",
        follow_redirects=False,
    )

    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "nyc_ai_events_session=" in cookie
    assert "secure" in cookie.lower()


def test_google_callback_reuses_linked_identity_without_new_user(tmp_path) -> None:
    oauth = FakeGoogleOAuthClient()
    client, db = _client(tmp_path, google_oauth_client=oauth)
    conn = connect(db)
    storage.create_alpha_invite(conn, email="friend@example.com")
    conn.close()

    for _ in range(2):
        client.get("/auth/google/start", follow_redirects=False)
        response = client.get(
            "/auth/google/callback?code=ok&state=state-ok",
            follow_redirects=False,
        )
        assert response.status_code == 303

    conn = connect(db)
    count = conn.execute("SELECT count(*) FROM users WHERE email = ?", ("friend@example.com",)).fetchone()[0]
    conn.close()
    assert count == 1


def test_google_callback_rejects_invalid_state(tmp_path) -> None:
    client, db = _client(tmp_path, google_oauth_client=FakeGoogleOAuthClient())
    conn = connect(db)
    storage.create_alpha_invite(conn, email="friend@example.com")
    conn.close()
    client.get("/auth/google/start", follow_redirects=False)

    response = client.get(
        "/auth/google/callback?code=ok&state=wrong",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "could not be verified" in response.text
    assert "nyc_ai_events_session=" not in response.headers.get("set-cookie", "")


def test_google_callback_handles_provider_error_and_missing_code(tmp_path) -> None:
    client, _ = _client(tmp_path, google_oauth_client=FakeGoogleOAuthClient())

    denied = client.get(
        "/auth/google/callback?error=access_denied",
        follow_redirects=False,
    )
    missing = client.get("/auth/google/callback?state=state-ok", follow_redirects=False)

    assert denied.status_code == 401
    assert "cancelled" in denied.text
    assert missing.status_code == 400
    assert "incomplete" in missing.text


def test_google_callback_rejects_unverified_email(tmp_path) -> None:
    oauth = FakeGoogleOAuthClient(
        GoogleUserInfo(
            subject="google-sub-1",
            email="friend@example.com",
            email_verified=False,
            display_name="Friend",
        )
    )
    client, db = _client(tmp_path, google_oauth_client=oauth)
    conn = connect(db)
    storage.create_alpha_invite(conn, email="friend@example.com")
    conn.close()
    client.get("/auth/google/start", follow_redirects=False)

    response = client.get(
        "/auth/google/callback?code=ok&state=state-ok",
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "email must be verified" in response.text


def test_google_callback_rejects_verified_google_user_without_invite(tmp_path) -> None:
    client, db = _client(tmp_path, google_oauth_client=FakeGoogleOAuthClient())
    client.get("/auth/google/start", follow_redirects=False)

    response = client.get(
        "/auth/google/callback?code=ok&state=state-ok",
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "cannot sign in right now" in response.text
    assert "nyc_ai_events_session=" not in response.headers.get("set-cookie", "")
    conn = connect(db)
    user = storage.get_user_by_identity(conn, provider="google", subject="google-sub-1")
    conn.close()
    assert user is None


def test_google_callback_rejects_revoked_linked_user(tmp_path) -> None:
    oauth = FakeGoogleOAuthClient()
    client, db = _client(tmp_path, google_oauth_client=oauth)
    conn = connect(db)
    storage.create_alpha_invite(conn, email="friend@example.com")
    user = storage.create_or_accept_google_user(
        conn,
        subject="google-sub-1",
        email="friend@example.com",
        email_verified=True,
        display_name="Friend",
    )
    assert user is not None
    storage.revoke_alpha_invite(conn, "friend@example.com")
    conn.close()
    client.get("/auth/google/start", follow_redirects=False)

    response = client.get(
        "/auth/google/callback?code=ok&state=state-ok",
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "cannot sign in right now" in response.text


def test_invalid_invite_code_does_not_create_session(tmp_path) -> None:
    client, _ = _client(tmp_path)

    response = _login(client, "wrong-secret")

    assert response.status_code == 401
    assert "Invite code not recognized" in response.text
    assert "nyc_ai_events_session" not in response.cookies


def test_inactive_user_cannot_login(tmp_path) -> None:
    client, db = _client(tmp_path)
    conn = connect(db)
    conn.execute("UPDATE users SET is_active = 0")
    conn.close()

    response = _login(client)

    assert response.status_code == 401
    assert "Invite code not recognized" in response.text


def test_valid_login_sets_http_only_session_cookie(tmp_path) -> None:
    client, _ = _client(tmp_path)

    response = _login(client)

    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "nyc_ai_events_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


def test_personal_pages_require_login(tmp_path) -> None:
    client, _ = _client(tmp_path)

    for path in ("/mine", "/hidden", "/settings"):
        response = client.get(path, follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_personal_event_actions_require_login(tmp_path) -> None:
    client, _ = _client(tmp_path)

    for path, data in (
        ("/events/1/status", {"status": "saved"}),
        ("/events/1/rating", {"rating": "1"}),
        ("/events/1/action", {}),
    ):
        response = client.post(path, data=data, follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_authenticated_events_route_rejects_deactivated_user(tmp_path) -> None:
    client, db = _client(tmp_path)
    _login(client)
    conn = connect(db)
    conn.execute("UPDATE users SET is_active = 0")
    conn.close()

    response = client.get("/events", follow_redirects=False)

    assert response.status_code == 200
    assert 'data-public-preview="true"' in response.text
    assert client.get("/api/discovery", follow_redirects=False).status_code == 303
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_logout_clears_session_and_returns_to_public_preview(tmp_path) -> None:
    client, _ = _client(tmp_path)
    _login(client)
    csrf = client.get("/settings").text.split('name="_csrf_token" value="', 1)[1].split('"', 1)[0]

    response = client.post(
        "/logout",
        data={"_csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/events"
    assert "Max-Age=0" in response.headers["set-cookie"]
    preview = client.get(response.headers["location"])
    assert 'data-public-preview="true"' in preview.text
    assert 'id="earlyAccess"' in preview.text
    assert 'aria-label="Open profile"' not in preview.text
    assert client.get("/api/discovery", follow_redirects=False).status_code == 303


def test_status_requires_csrf(tmp_path) -> None:
    client, _ = _client(tmp_path)
    _login(client)

    response = client.post(
        "/events/1/status",
        data={"status": "saved"},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_repeated_failed_logins_are_rate_limited(tmp_path) -> None:
    client, _ = _client(tmp_path)

    for _ in range(5):
        _login(client, "wrong-secret")
    response = _login(client, "wrong-secret")

    assert response.status_code == 429
