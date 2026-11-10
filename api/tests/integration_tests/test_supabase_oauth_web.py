from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

import api.app as app_module
from api.app import create_app
from api.integrations.supabase.auth import (
    AuthorizationDetails,
    OAuthGrant,
    SupabaseAuthError,
    SupabaseIdentity,
    SupabaseOAuthStart,
    SupabaseSession,
)
from api.repositories.events import connect
from api.repositories.invites import create_alpha_invite, revoke_alpha_invite
from api.repositories.users import deactivate_user
from api.tests.fixtures.web_helpers import migrated_db

SESSION_SECRET = "test-session-secret-" + ("x" * 32)


class FakeSupabaseAuthGateway:
    def __init__(self) -> None:
        self.redirect_uri: str | None = None
        self.refresh_tokens: list[str] = []
        self.consent_calls: list[tuple[str, str]] = []
        self.revoked_clients: list[str] = []
        self.logged_out: list[str] = []
        self.identity = SupabaseIdentity(
            subject="supabase-sub-1",
            email="friend@example.com",
            email_verified=True,
            display_name="Friend",
        )

    def _session(self, *, refresh_token: str = "refresh-token-1") -> SupabaseSession:
        return SupabaseSession(
            access_token="access-token-1",
            refresh_token=refresh_token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            identity=self.identity,
        )

    async def start_google_sign_in(self, redirect_uri: str) -> SupabaseOAuthStart:
        self.redirect_uri = redirect_uri
        return SupabaseOAuthStart(
            authorization_url="https://project-ref.supabase.co/auth/v1/authorize?provider=google",
            code_verifier="v" * 64,
        )

    async def exchange_code(self, *, auth_code: str, code_verifier: str, redirect_uri: str):
        assert auth_code == "auth-code"
        assert code_verifier == "v" * 64
        assert redirect_uri == self.redirect_uri
        return self._session()

    async def refresh_session(self, refresh_token: str) -> SupabaseSession:
        self.refresh_tokens.append(refresh_token)
        return self._session(refresh_token="rotated-refresh-token")

    async def get_identity(self, access_token: str) -> SupabaseIdentity:
        return self.identity

    async def get_authorization_details(self, authorization_id: str, access_token: str):
        return AuthorizationDetails(
            authorization_id=authorization_id,
            redirect_uri="http://127.0.0.1:1455/callback",
            client_id="codex-client",
            client_name="Codex",
            client_uri="https://openai.com/codex",
            user_subject=self.identity.subject,
            user_email=self.identity.email,
            scopes=("openid", "email"),
        )

    async def submit_consent(self, authorization_id: str, action: str, access_token: str):
        self.consent_calls.append((authorization_id, action))
        return f"http://127.0.0.1:1455/callback?decision={action}"

    async def list_grants(self, access_token: str):
        return [
            OAuthGrant(
                client_id="codex-client",
                client_name="Codex",
                client_uri="https://openai.com/codex",
                scopes=("openid", "email"),
                granted_at="2030-01-01T00:00:00Z",
            )
        ]

    async def revoke_grant(self, client_id: str, access_token: str):
        self.revoked_clients.append(client_id)

    async def logout(self, access_token: str):
        self.logged_out.append(access_token)


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.com")
    db = migrated_db(tmp_path)
    conn = connect(db)
    create_alpha_invite(conn, email="friend@example.com")
    conn.close()
    gateway = FakeSupabaseAuthGateway()
    app = create_app(
        db_path=db,
        session_secret=SESSION_SECRET,
        supabase_auth_gateway=gateway,
    )
    return TestClient(app, base_url="https://events.example.com"), db, gateway


def _start_and_callback(client: TestClient, gateway: FakeSupabaseAuthGateway):
    started = client.get("/auth/supabase/start", follow_redirects=False)
    assert started.status_code == 303
    state = parse_qs(urlsplit(gateway.redirect_uri).query)["state"][0]
    return client.get(
        f"/auth/supabase/callback?code=auth-code&state={state}",
        follow_redirects=False,
    ), state


def _csrf(client: TestClient, path: str = "/settings") -> str:
    page = client.get(path)
    return page.text.split('name="_csrf_token" value="', 1)[1].split('"', 1)[0]


def test_uninvited_identity_returns_to_preview_without_product_session(tmp_path, monkeypatch):
    client, db, gateway = _client(tmp_path, monkeypatch)
    gateway.identity = SupabaseIdentity(
        subject="not-invited", email='friend+other@example.com',
        email_verified=True, display_name="Another account",
    )
    response, _ = _start_and_callback(client, gateway)
    assert response.status_code == 403
    assert "This account isn't invited yet" in response.text
    assert 'value="friend+other@example.com"' in response.text
    assert "Use another Google account" in response.text
    assert 'data-public-preview="true"' in response.text
    assert not client.cookies.get("nyc_ai_events_session")
    assert not client.cookies.get("nyc_ai_events_sb_access")
    assert client.get("/api/discovery", follow_redirects=False).status_code == 303
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM invitation_requests").fetchone()[0] == 0


def test_open_google_signup_admits_new_user_without_invitation(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_HUNT_OPEN_GOOGLE_SIGNUP", "1")
    client, db, gateway = _client(tmp_path, monkeypatch)
    gateway.identity = SupabaseIdentity(subject="new-google-user", email="new@gmail.com", email_verified=True,
                                        display_name="New member", has_google_identity=True)
    response, _ = _start_and_callback(client, gateway)
    assert response.status_code == 303
    assert response.headers["location"] == "/events"
    assert client.get("/api/discovery").status_code == 200
    assert client.get("/settings/ai").status_code == 200
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM invitation_requests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM alpha_invites WHERE normalized_email='new@gmail.com'").fetchone()[0] == 0


def test_open_google_signup_removes_access_panel_and_rejects_new_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_HUNT_OPEN_GOOGLE_SIGNUP", "1")
    client, db, _ = _client(tmp_path, monkeypatch)
    page = client.get('/events')
    assert '<header class="public-header"><a href="/auth/supabase/start">Sign in</a></header>' in page.text
    assert 'Early Access' not in page.text
    assert 'id="earlyAccess"' not in page.text
    assert 'id="inviteForm"' not in page.text
    assert 'Request an invite' not in page.text
    assert 'invited Google account' not in client.get('/login').text
    response = client.post('/api/invitation-requests', json={'email': 'new@gmail.com'})
    assert response.status_code == 409
    with connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM invitation_requests').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM invitation_mail').fetchone()[0] == 0


def test_closing_google_signup_restores_the_complete_invitation_panel(tmp_path, monkeypatch):
    monkeypatch.setenv('EVENT_HUNT_OPEN_GOOGLE_SIGNUP', '1')
    client, _, _ = _client(tmp_path, monkeypatch)
    assert 'Early Access' not in client.get('/events').text
    monkeypatch.setenv('EVENT_HUNT_OPEN_GOOGLE_SIGNUP', '0')
    page = client.get('/events')
    for content in ['Early Access', 'id="inviteForm"', 'id="requestEmail"', 'Request an invite', 'Already invited?']:
        assert content in page.text
    assert '<header class="public-header"><a href="/auth/supabase/start">Sign in</a></header>' in page.text


@pytest.mark.parametrize('verified,google', [(False, True), (True, False)])
def test_open_signup_requires_verified_google_identity(tmp_path, monkeypatch, verified, google):
    monkeypatch.setenv('EVENT_HUNT_OPEN_GOOGLE_SIGNUP', '1')
    client, _, gateway = _client(tmp_path, monkeypatch)
    gateway.identity = SupabaseIdentity('new-subject', 'new@gmail.com', verified, 'New member', google)
    response, _ = _start_and_callback(client, gateway)
    assert response.status_code == 403
    assert client.get('/api/discovery', follow_redirects=False).status_code == 303


@pytest.mark.parametrize('blocked', ['revoked', 'inactive'])
def test_open_signup_does_not_reinstate_blocked_account(tmp_path, monkeypatch, blocked):
    monkeypatch.setenv('EVENT_HUNT_OPEN_GOOGLE_SIGNUP', '1')
    client, db, gateway = _client(tmp_path, monkeypatch)
    if blocked == 'revoked':
        with connect(db) as conn:
            revoke_alpha_invite(conn, 'friend@example.com')
    else:
        _start_and_callback(client, gateway)
        with connect(db) as conn:
            deactivate_user(conn, 'friend@example.com')
    gateway.identity = SupabaseIdentity('new-subject', 'friend@example.com', True, 'Friend', True)
    response, _ = _start_and_callback(client, gateway)
    assert response.status_code == 403
    assert 'Early Access' not in response.text
    assert 'id="signInError"' in response.text
    assert 'This Google account cannot sign in right now.' in response.text
    assert 'Use another Google account' in response.text
    assert response.status_code == 403
    assert client.get('/api/discovery', follow_redirects=False).status_code == 303


def test_closing_signup_blocks_new_members_and_keeps_admitted_member(tmp_path, monkeypatch):
    monkeypatch.setenv('EVENT_HUNT_OPEN_GOOGLE_SIGNUP', '1')
    client, _, gateway = _client(tmp_path, monkeypatch)
    gateway.identity = SupabaseIdentity('new-subject', 'new@gmail.com', True, 'New member', True)
    assert _start_and_callback(client, gateway)[0].status_code == 303
    monkeypatch.delenv('EVENT_HUNT_OPEN_GOOGLE_SIGNUP')
    assert _start_and_callback(client, gateway)[0].status_code == 303
    gateway.identity = SupabaseIdentity('another-subject', 'another@gmail.com', True, 'Another member', True)
    assert _start_and_callback(client, gateway)[0].status_code == 403


@pytest.mark.parametrize('open_signup', ['0', '1'])
def test_signin_does_not_silently_change_linked_email(tmp_path, monkeypatch, open_signup):
    client, _, gateway = _client(tmp_path, monkeypatch)
    assert _start_and_callback(client, gateway)[0].status_code == 303
    monkeypatch.setenv('EVENT_HUNT_OPEN_GOOGLE_SIGNUP', open_signup)
    gateway.identity = SupabaseIdentity('supabase-sub-1', 'replacement@gmail.com', True, 'Friend', True)
    assert _start_and_callback(client, gateway)[0].status_code == 403


def test_new_google_member_can_continue_to_mcp_consent(tmp_path, monkeypatch):
    monkeypatch.setenv('EVENT_HUNT_OPEN_GOOGLE_SIGNUP', '1')
    client, _, gateway = _client(tmp_path, monkeypatch)
    gateway.identity = SupabaseIdentity('new-subject', 'new@gmail.com', True, 'New member', True)
    client.get('/auth/supabase/start?authorization_id=authorization-1', follow_redirects=False)
    state = parse_qs(urlsplit(gateway.redirect_uri).query)['state'][0]
    response = client.get(f'/auth/supabase/callback?code=auth-code&state={state}', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/oauth/consent?authorization_id=authorization-1'
    assert client.get(response.headers['location']).status_code == 200


def test_login_uses_supabase_google_route(tmp_path, monkeypatch) -> None:
    client, _, _ = _client(tmp_path, monkeypatch)

    response = client.get("/login")

    assert response.status_code == 200
    assert 'href="/auth/supabase/start"' in response.text


def test_account_opens_approved_ai_integration_without_legacy_settings(tmp_path, monkeypatch) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)

    page = client.get("/settings")

    assert page.status_code == 200
    assert "Account" in page.text
    assert "friend@example.com" in page.text
    assert 'href="/settings/ai"' in page.text
    assert "Sign out" in page.text
    assert "Interest flags" not in page.text
    assert "global v1 score" not in page.text
    assert "Authorized agents" not in page.text


def test_ai_integration_uses_public_mcp_prompt_and_actual_grants(tmp_path, monkeypatch) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)

    page = client.get("/settings/ai")

    assert page.status_code == 200
    assert "MCP setup" in page.text
    assert "Paste into your AI agent" in page.text
    assert 'aria-label="Copy setup prompt"' in page.text
    assert "remote MCP at https://events.example.com/mcp. Use OAuth" in page.text
    assert "Authorized clients" in page.text
    assert "Codex" in page.text
    assert "Remove access" in page.text
    assert 'href="/settings"' in page.text
    assert "Setup Codex" not in page.text


def test_feedback_opens_draft_only_for_configured_recipient(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NYC_AI_EVENTS_FEEDBACK_EMAIL", "feedback@example.test")
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)

    page = client.get("/settings")

    assert 'href="mailto:feedback@example.test?subject=Event+Hunt+feedback"' in page.text
    assert "Feedback" in page.text


def test_feedback_identifies_event_hunt_in_company_inbox(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NYC_AI_EVENTS_FEEDBACK_EMAIL", raising=False)
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)

    page = client.get("/settings")

    assert page.status_code == 200
    assert 'href="mailto:info@sotazoo.com?subject=Event+Hunt+feedback"' in page.text
    assert "Feedback" in page.text


@pytest.mark.parametrize("recipient", [
    "", "not-an-email", "a@@b", "a@", "@example.test",
    "a@example.test\r\nBcc: other@example.test",
])
def test_invalid_feedback_recipient_keeps_account_available(tmp_path, monkeypatch, recipient) -> None:
    monkeypatch.setenv("NYC_AI_EVENTS_FEEDBACK_EMAIL", recipient)
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)

    page = client.get("/settings")

    assert page.status_code == 200
    assert "Feedback" not in page.text
    assert 'href="/settings/ai"' in page.text


def test_supabase_start_rate_limits_anonymous_requests(tmp_path, monkeypatch) -> None:
    client, db, _ = _client(tmp_path, monkeypatch)

    for _ in range(10):
        response = client.get("/auth/supabase/start", follow_redirects=False)
        assert response.status_code == 303

    blocked = client.get("/auth/supabase/start", follow_redirects=False)

    assert blocked.status_code == 429
    conn = connect(db)
    count = conn.execute("SELECT COUNT(*) FROM auth_transactions").fetchone()[0]
    conn.close()
    assert count == 10


def test_unauthenticated_consent_preserves_authorization_id(tmp_path, monkeypatch) -> None:
    client, _, _ = _client(tmp_path, monkeypatch)

    response = client.get(
        "/oauth/consent?authorization_id=authorization-1",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == ("/auth/supabase/start?authorization_id=authorization-1")


def test_cancelled_google_sign_in_can_retry_pending_consent(tmp_path, monkeypatch) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    client.get("/auth/supabase/start?authorization_id=authorization-1", follow_redirects=False)
    state = parse_qs(urlsplit(gateway.redirect_uri).query)["state"][0]

    cancelled = client.get(
        f"/auth/supabase/callback?error=access_denied&state={state}", follow_redirects=False,
    )

    assert cancelled.status_code == 401
    assert 'href="/auth/supabase/start?authorization_id=authorization-1"' in cancelled.text
    assert gateway.consent_calls == []
    client.get("/auth/supabase/start?authorization_id=authorization-1", follow_redirects=False)
    retry_state = parse_qs(urlsplit(gateway.redirect_uri).query)["state"][0]
    completed = client.get(
        f"/auth/supabase/callback?code=auth-code&state={retry_state}", follow_redirects=False,
    )
    assert completed.headers["location"] == "/oauth/consent?authorization_id=authorization-1"
    assert gateway.consent_calls == []


def test_failed_exchange_can_retry_pending_consent(tmp_path, monkeypatch) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    exchange_code = gateway.exchange_code
    client.get("/auth/supabase/start?authorization_id=authorization-1", follow_redirects=False)
    state = parse_qs(urlsplit(gateway.redirect_uri).query)["state"][0]

    async def failed_exchange(**_kwargs):
        raise SupabaseAuthError("exchange unavailable")

    gateway.exchange_code = failed_exchange
    failed = client.get(
        f"/auth/supabase/callback?code=auth-code&state={state}", follow_redirects=False,
    )
    assert failed.status_code == 401
    assert 'href="/auth/supabase/start?authorization_id=authorization-1"' in failed.text
    assert gateway.consent_calls == []

    gateway.exchange_code = exchange_code
    client.get("/auth/supabase/start?authorization_id=authorization-1", follow_redirects=False)
    retry_state = parse_qs(urlsplit(gateway.redirect_uri).query)["state"][0]
    completed = client.get(
        f"/auth/supabase/callback?code=auth-code&state={retry_state}", follow_redirects=False,
    )
    assert completed.headers["location"] == "/oauth/consent?authorization_id=authorization-1"
    assert gateway.consent_calls == []


def test_expired_provider_session_restarts_consent_without_replaying_decision(
    tmp_path, monkeypatch,
) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)
    csrf = _csrf(client)
    get_identity, refresh_session = gateway.get_identity, gateway.refresh_session

    async def expired_session(_token):
        raise SupabaseAuthError("provider session expired")

    gateway.get_identity = gateway.refresh_session = expired_session
    assert client.cookies.get("nyc_ai_events_session")
    response = client.post(
        "/oauth/consent",
        data={"authorization_id": "authorization-1", "decision": "approve", "_csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/supabase/start?authorization_id=authorization-1"
    assert gateway.consent_calls == []

    gateway.get_identity, gateway.refresh_session = get_identity, refresh_session
    client.get(response.headers["location"], follow_redirects=False)
    state = parse_qs(urlsplit(gateway.redirect_uri).query)["state"][0]
    completed = client.get(
        f"/auth/supabase/callback?code=auth-code&state={state}", follow_redirects=False,
    )
    assert completed.headers["location"] == "/oauth/consent?authorization_id=authorization-1"
    assert gateway.consent_calls == []


@pytest.mark.parametrize("csrf", [None, "invalid-token"])
def test_confirmed_removal_requires_csrf_and_preserves_access(tmp_path, monkeypatch, csrf) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)
    data = {"confirmed": "yes"}
    if csrf is not None:
        data["_csrf_token"] = csrf

    response = client.post("/settings/oauth-grants/codex-client/revoke", data=data)

    assert response.status_code == 403
    assert gateway.revoked_clients == []
    assert gateway.logged_out == []
    assert client.get("/settings/ai").status_code == 200
    assert "Codex" in client.get("/settings/ai").text
    assert client.get("/events").status_code == 200


@pytest.mark.parametrize("client_id, expected", [("unnamed-client-id", "unnamed-client-id"), (None, "AI app")])
def test_consent_explains_identity_scopes_and_unnamed_client(tmp_path, monkeypatch, client_id, expected) -> None:
    client, db, gateway = _client(tmp_path, monkeypatch)
    long_email = "a" * 63 + "@" + "b" * 63 + "." + "c" * 63 + ".test"
    gateway.identity = SupabaseIdentity("supabase-sub-1", long_email, True, "Friend")
    conn = connect(db)
    create_alpha_invite(conn, email=long_email)
    conn.close()
    _start_and_callback(client, gateway)

    async def details(authorization_id, _access_token):
        return AuthorizationDetails(
            authorization_id, "http://127.0.0.1:1455/callback", client_id, None, None,
            gateway.identity.subject, long_email, ("openid", "email", "profile", "phone", "offline_access"),
        )

    gateway.get_authorization_details = details
    page = client.get("/oauth/consent?authorization_id=authorization-1")
    assert page.status_code == 200
    assert f"Authorize {expected}" in page.text
    assert long_email in page.text
    assert "View your email address" in page.text
    assert "View your name and profile photo" in page.text
    assert "View your phone number" in page.text
    assert "Keep this app connected" in page.text
    assert "Requested scope: offline_access" not in page.text
    assert gateway.consent_calls == []


def test_expired_website_session_restarts_sign_in_without_replaying_consent(
    tmp_path, monkeypatch,
) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)
    csrf = _csrf(client)
    client.cookies.delete("nyc_ai_events_session")

    response = client.post(
        "/oauth/consent",
        data={"authorization_id": "authorization-1", "decision": "approve", "_csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/supabase/start?authorization_id=authorization-1"
    assert gateway.consent_calls == []
    client.get("/auth/supabase/start?authorization_id=authorization-1", follow_redirects=False)
    retry_state = parse_qs(urlsplit(gateway.redirect_uri).query)["state"][0]
    completed = client.get(
        f"/auth/supabase/callback?code=auth-code&state={retry_state}", follow_redirects=False,
    )
    assert completed.headers["location"] == "/oauth/consent?authorization_id=authorization-1"
    assert gateway.consent_calls == []


def test_callback_consumes_one_time_flow_and_sets_secure_sessions(tmp_path, monkeypatch) -> None:
    client, db, gateway = _client(tmp_path, monkeypatch)

    response, state = _start_and_callback(client, gateway)

    assert response.status_code == 303
    assert response.headers["location"] == "/events"
    set_cookies = response.headers.get_list("set-cookie")
    assert any("nyc_ai_events_session=" in value for value in set_cookies)
    assert any("nyc_ai_events_sb_access=" in value and "Secure" in value for value in set_cookies)
    assert any(
        "nyc_ai_events_sb_refresh=" in value and "HttpOnly" in value for value in set_cookies
    )
    conn = connect(db)
    row = conn.execute(
        "SELECT consumed_at, subject FROM auth_transactions WHERE state = ?",
        (state,),
    ).fetchone()
    conn.close()
    assert row["consumed_at"] is not None
    assert row["subject"] == "supabase-sub-1"

    replay = client.get(
        f"/auth/supabase/callback?code=auth-code&state={state}",
        follow_redirects=False,
    )
    assert replay.status_code == 401


def test_consent_requires_csrf_then_redirects_to_supabase_result(tmp_path, monkeypatch) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    response, _ = _start_and_callback(client, gateway)
    assert response.status_code == 303

    page = client.get("/oauth/consent?authorization_id=authorization-1")
    assert page.status_code == 200
    assert "Authorize Codex" in page.text
    assert "Signed in as" in page.text
    assert "friend@example.com" in page.text
    assert "Read event recommendations" in page.text
    assert "View your email address" in page.text
    assert "Allow access" in page.text
    assert "Cancel" in page.text
    assert "Redirect URI" not in page.text

    rejected = client.post(
        "/oauth/consent",
        data={"authorization_id": "authorization-1", "decision": "approve"},
        follow_redirects=False,
    )
    assert rejected.status_code == 403

    csrf = page.text.split('name="_csrf_token" value="', 1)[1].split('"', 1)[0]
    approved = client.post(
        "/oauth/consent",
        data={
            "authorization_id": "authorization-1",
            "decision": "approve",
            "_csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert approved.status_code == 303
    assert approved.headers["location"].endswith("decision=approve")
    assert gateway.consent_calls == [("authorization-1", "approve")]


def test_settings_lists_and_revokes_agent_grant(tmp_path, monkeypatch) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)

    page = client.get("/settings/ai")

    assert page.status_code == 200
    assert "Authorized clients" in page.text
    assert "Codex" in page.text
    csrf = page.text.split('name="_csrf_token" value="', 1)[1].split('"', 1)[0]
    revoked = client.post(
        "/settings/oauth-grants/codex-client/revoke",
        data={"_csrf_token": csrf, "confirmed": "yes"},
        follow_redirects=False,
    )
    assert revoked.status_code == 303
    assert revoked.headers["location"] == "/settings/ai"
    assert gateway.revoked_clients == ["codex-client"]


def test_remove_access_requires_confirmation(tmp_path, monkeypatch) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)

    page = client.post(
        "/settings/oauth-grants/codex-client/revoke",
        data={"_csrf_token": _csrf(client, "/settings/ai")},
    )

    assert page.status_code == 200
    assert "Remove access for Codex?" in page.text
    assert 'name="confirmed" value="yes"' in page.text
    assert 'href="/settings/ai"' in page.text
    assert gateway.revoked_clients == []


def test_missing_client_before_confirmation_keeps_remaining_access(tmp_path, monkeypatch) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)

    page = client.post(
        "/settings/oauth-grants/missing-client/revoke",
        data={"_csrf_token": _csrf(client, "/settings/ai")},
    )

    assert page.status_code == 404
    assert "This client no longer has access." in page.text
    assert "Codex" in page.text
    assert gateway.revoked_clients == []


def test_unavailable_provider_session_keeps_website_and_blocks_removal(tmp_path, monkeypatch) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)
    csrf = _csrf(client)

    async def expired_session(_token: str):
        raise SupabaseAuthError("provider session expired")

    gateway.get_identity = expired_session
    gateway.refresh_session = expired_session
    page = client.get("/settings/ai")

    assert page.status_code == 200
    assert "Sign in again to manage access." in page.text
    assert 'href="/auth/supabase/start"' in page.text
    assert "No authorized clients." not in page.text
    response = client.post(
        "/settings/oauth-grants/codex-client/revoke",
        data={"_csrf_token": csrf, "confirmed": "yes"},
    )
    assert response.status_code == 401
    assert "Sign in again to manage access." in response.text
    assert gateway.revoked_clients == []
    assert client.get("/events").status_code == 200


def test_missing_access_cookie_refreshes_and_rotates_session(tmp_path, monkeypatch) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)
    client.cookies.delete("nyc_ai_events_sb_access")

    response = client.get("/settings")

    assert response.status_code == 200
    assert gateway.refresh_tokens == ["refresh-token-1"]
    assert any(
        "nyc_ai_events_sb_refresh=rotated-refresh-token" in value
        for value in response.headers.get_list("set-cookie")
    )


def test_expired_access_cookie_refreshes_and_rotates_session(tmp_path, monkeypatch) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)

    async def reject_expired_access(_access_token: str):
        raise SupabaseAuthError("expired access")

    gateway.get_identity = reject_expired_access
    response = client.get("/settings")

    assert response.status_code == 200
    assert gateway.refresh_tokens == ["refresh-token-1"]
    assert any(
        "nyc_ai_events_sb_refresh=rotated-refresh-token" in value
        for value in response.headers.get_list("set-cookie")
    )


def test_consent_error_keeps_rotated_supabase_session(tmp_path, monkeypatch) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)
    client.cookies.delete("nyc_ai_events_sb_access")

    async def fail_details(_authorization_id: str, _access_token: str):
        raise SupabaseAuthError("authorization unavailable")

    gateway.get_authorization_details = fail_details
    response = client.get(
        "/oauth/consent?authorization_id=authorization-1",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "Restart Connect in your AI app" in response.text
    assert any(
        "nyc_ai_events_sb_refresh=rotated-refresh-token" in value
        for value in response.headers.get_list("set-cookie")
    )


def test_consent_decision_error_keeps_rotated_supabase_session(
    tmp_path,
    monkeypatch,
) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)
    csrf = _csrf(client)
    client.cookies.delete("nyc_ai_events_sb_access")

    async def fail_details(_authorization_id: str, _access_token: str):
        raise SupabaseAuthError("authorization unavailable")

    gateway.get_authorization_details = fail_details
    response = client.post(
        "/oauth/consent",
        data={
            "authorization_id": "authorization-1",
            "decision": "approve",
            "_csrf_token": csrf,
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert any(
        "nyc_ai_events_sb_refresh=rotated-refresh-token" in value
        for value in response.headers.get_list("set-cookie")
    )


def test_revoke_error_keeps_rotated_supabase_session(tmp_path, monkeypatch) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)
    csrf = _csrf(client)
    client.cookies.delete("nyc_ai_events_sb_access")

    async def fail_revoke(_client_id: str, _access_token: str):
        raise SupabaseAuthError("revoke unavailable")

    gateway.revoke_grant = fail_revoke
    response = client.post(
        "/settings/oauth-grants/codex-client/revoke",
        data={"_csrf_token": csrf, "confirmed": "yes"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert any(
        "nyc_ai_events_sb_refresh=rotated-refresh-token" in value
        for value in response.headers.get_list("set-cookie")
    )


def test_database_connection_is_closed_before_supabase_network_wait(
    tmp_path,
    monkeypatch,
) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)
    original_connect = app_module._connect
    open_connections = 0

    class TrackedConnection:
        def __init__(self, conn):
            nonlocal open_connections
            self._conn = conn
            self._closed = False
            open_connections += 1

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self):
            nonlocal open_connections
            if not self._closed:
                self._conn.close()
                self._closed = True
                open_connections -= 1

    def tracked_connect(request):
        return TrackedConnection(original_connect(request))

    async def assert_no_open_connection(_access_token: str):
        assert open_connections == 0
        return gateway.identity

    monkeypatch.setattr(app_module, "_connect", tracked_connect)
    gateway.get_identity = assert_no_open_connection

    response = client.get("/settings")

    assert response.status_code == 200
    assert open_connections == 0


def test_supabase_callback_does_not_log_provider_secret(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    started = client.get("/auth/supabase/start", follow_redirects=False)
    assert started.status_code == 303
    state = parse_qs(urlsplit(gateway.redirect_uri).query)["state"][0]

    async def fail_exchange(**_kwargs):
        try:
            raise RuntimeError("provider-secret-marker")
        except RuntimeError as cause:
            raise SupabaseAuthError("safe request id") from cause

    gateway.exchange_code = fail_exchange
    with caplog.at_level(logging.WARNING):
        response = client.get(
            f"/auth/supabase/callback?code=auth-code&state={state}",
            follow_redirects=False,
        )

    assert response.status_code == 401
    assert "provider-secret-marker" not in caplog.text


def test_logout_revokes_supabase_web_session_and_clears_all_cookies(
    tmp_path,
    monkeypatch,
) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)
    csrf = _csrf(client)

    response = client.post(
        "/logout",
        data={"_csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert gateway.logged_out == ["access-token-1"]
    assert response.headers["location"] == "/events"
    preview = client.get(response.headers["location"])
    assert 'data-public-preview="true"' in preview.text
    assert 'id="earlyAccess"' in preview.text
    assert 'aria-label="Open profile"' not in preview.text
    set_cookies = response.headers.get_list("set-cookie")
    assert any("nyc_ai_events_session=" in value and "Max-Age=0" in value for value in set_cookies)
    assert any(
        "nyc_ai_events_sb_access=" in value and "Max-Age=0" in value for value in set_cookies
    )
    assert any(
        "nyc_ai_events_sb_refresh=" in value and "Max-Age=0" in value for value in set_cookies
    )


def test_logout_refreshes_missing_access_before_revoking_supabase_session(
    tmp_path,
    monkeypatch,
) -> None:
    client, _, gateway = _client(tmp_path, monkeypatch)
    _start_and_callback(client, gateway)
    csrf = _csrf(client)
    client.cookies.delete("nyc_ai_events_sb_access")

    response = client.post(
        "/logout",
        data={"_csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert gateway.refresh_tokens == ["refresh-token-1"]
    assert gateway.logged_out == ["access-token-1"]
