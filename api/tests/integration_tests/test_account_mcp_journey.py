"""Web consent/removal and actual MCP HTTP calls with a synthetic provider."""

import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

from api.app import create_app
from api.mcp.server import SupabaseMCPTokenVerifier
from api.repositories.events import connect
from api.repositories.invites import create_alpha_invite
from api.tests.fixtures.oauth_provider import CLIENT_ID, ISSUER, PROJECT, OAuthProviderFixture
from api.tests.fixtures.web_helpers import add_scored_event, migrated_db


def _app(db, provider):
    verifier = SupabaseMCPTokenVerifier(
        issuer_url=ISSUER, jwks_url=f"{ISSUER}/.well-known/jwks.json",
        resource_url=provider.resource_url, db_path=db,
        auth_gateway=provider, http_client=provider.provider_http,
    )
    return create_app(
        db_path=db, session_secret="synthetic-journey-secret",
        supabase_auth_gateway=provider, supabase_mcp_token_verifier=verifier,
    )


def _sign_in(client):
    started = client.get("/auth/supabase/start", follow_redirects=False)
    callback = parse_qs(urlsplit(started.headers["location"]).query)["callback"][0]
    completed = client.get(callback + "&code=synthetic-auth-code", follow_redirects=False)
    assert completed.status_code == 303


def _csrf(client, path="/settings"):
    return client.get(path).text.split('name="_csrf_token" value="', 1)[1].split('"', 1)[0]


def _mcp_headers(client, token):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    initialized = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "synthetic-client", "version": "1"}},
    })
    assert initialized.status_code == 200
    headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
    headers["MCP-Protocol-Version"] = initialized.json()["result"]["protocolVersion"]
    notified = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert notified.status_code == 202
    return headers


def _query(client, headers):
    now = datetime.now(UTC)
    return client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "find_events", "arguments": {
            "start": now.isoformat(), "end": (now + timedelta(days=7)).isoformat(),
        }},
    })


def test_consent_query_remove_restart_and_reconnect_preserve_other_client(tmp_path, monkeypatch):
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.com")
    monkeypatch.setenv("NYC_AI_EVENTS_SUPABASE_URL", PROJECT)
    monkeypatch.setenv("NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_synthetic")
    monkeypatch.delenv("NYC_AI_EVENTS_AGENT_API_TOKEN", raising=False)
    db = migrated_db(tmp_path)
    conn = connect(db)
    create_alpha_invite(conn, email="discovery@example.test")
    conn.close()
    add_scored_event(db, title="Journey event", score=.9, starts_at=datetime.now(UTC) + timedelta(days=1))
    provider = OAuthProviderFixture(resource_url="https://events.example.com/mcp")

    with TestClient(_app(db, provider), base_url="https://events.example.com") as client:
        _sign_in(client)
        allowed = client.post("/oauth/consent", data={
            "authorization_id": "journey-request", "decision": "approve",
            "_csrf_token": _csrf(client),
        }, follow_redirects=False)
        assert allowed.status_code == 303
        assert allowed.headers["location"].endswith("decision=approve")
        token = provider.issue_token()
        other_token = provider.issue_token("other-client")
        headers = _mcp_headers(client, token)
        other_headers = _mcp_headers(client, other_token)
        result = _query(client, headers)
        assert result.status_code == 200
        assert result.json()["result"]["structuredContent"]["events"][0]["title"] == "Journey event"

        removed = client.post(f"/settings/oauth-grants/{CLIENT_ID}/revoke", data={
            "_csrf_token": _csrf(client), "confirmed": "yes",
        }, follow_redirects=False)
        assert removed.status_code == 303
        assert _query(client, headers).status_code == 401  # The existing MCP session is rejected too.
        assert _query(client, other_headers).status_code == 200
        assert client.get("/settings").status_code == 200
        assert client.get("/api/discovery").status_code == 200
        assert "Example AI" not in client.get("/settings/ai").text
        cookies = dict(client.cookies)

    with TestClient(_app(db, provider), base_url="https://events.example.com") as restarted:
        restarted.cookies.update(cookies)
        assert restarted.get("/settings").status_code == 200
        assert restarted.get("/mcp", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        assert _query(restarted, _mcp_headers(restarted, other_token)).status_code == 200
        reapproved = restarted.post("/oauth/consent", data={
            "authorization_id": "new-journey-request", "decision": "approve",
            "_csrf_token": _csrf(restarted),
        }, follow_redirects=False)
        assert reapproved.status_code == 303
        new_token = provider.issue_token()
        assert _query(restarted, _mcp_headers(restarted, new_token)).status_code == 200
        assert restarted.get("/mcp", headers={"Authorization": f"Bearer {token}"}).status_code == 401


class UnavailableOAuthProvider(OAuthProviderFixture):
    unavailable_path = "/oauth/userinfo"
    failure = None

    def _provider(self, request):
        if self.failure is not None and request.url.path.endswith(self.unavailable_path):
            if self.failure == "timeout":
                raise httpx.ReadTimeout("private-provider-detail", request=request)
            if self.failure == "invalid_json":
                return httpx.Response(200, content=b"private-provider-detail")
            if self.failure == "invalid_shape":
                return httpx.Response(200, json={})
            if self.failure == "invalid_array":
                return httpx.Response(200, json=[])
            if self.failure == "invalid_key":
                return httpx.Response(200, json={
                    "keys": [{"kid": "synthetic-key", "alg": "RS256", "kty": "RSA"}],
                })
            return httpx.Response(self.failure, json={"detail": "private-provider-detail"})
        return super()._provider(request)


@pytest.fixture
def unavailable_provider_journey(tmp_path, monkeypatch):
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.com")
    monkeypatch.setenv("NYC_AI_EVENTS_SUPABASE_URL", PROJECT)
    monkeypatch.setenv("NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_synthetic")
    monkeypatch.delenv("NYC_AI_EVENTS_AGENT_API_TOKEN", raising=False)
    db = migrated_db(tmp_path)
    conn = connect(db)
    create_alpha_invite(conn, email="discovery@example.test")
    conn.close()
    provider = UnavailableOAuthProvider(resource_url="https://events.example.com/mcp")
    try:
        with TestClient(_app(db, provider), base_url="https://events.example.com") as client:
            _sign_in(client)
            yield client, provider, db
    finally:
        asyncio.run(provider.provider_http.aclose())


def _assert_authentication_unavailable(response, caplog):
    assert response.status_code == 503
    assert response.json()["error"] == "temporarily_unavailable"
    assert "www-authenticate" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert "private-provider-detail" not in response.text + caplog.text


@pytest.mark.parametrize("failure", [
    503, 429, 404, "timeout", "invalid_json", "invalid_shape", "invalid_array",
])
def test_mcp_session_survives_userinfo_outage_without_reauthorization(
    unavailable_provider_journey, caplog, failure,
):
    client, provider, _db = unavailable_provider_journey
    headers = _mcp_headers(client, provider.issue_token())
    assert _query(client, headers).status_code == 200
    sessions = dict(provider.sessions)

    provider.failure = failure
    _assert_authentication_unavailable(_query(client, headers), caplog)

    provider.failure = None
    assert _query(client, headers).status_code == 200
    assert provider.sessions == sessions
    provider.sessions.clear()
    assert _query(client, headers).status_code == 401


@pytest.mark.parametrize("failure", [
    503, "timeout", "invalid_json", "invalid_shape", "invalid_array", "invalid_key",
])
def test_mcp_initialize_recovers_from_jwks_outage_with_same_token(
    unavailable_provider_journey, caplog, failure,
):
    client, provider, _db = unavailable_provider_journey
    token = provider.issue_token()
    provider.unavailable_path = "/.well-known/jwks.json"
    provider.failure = failure
    response = client.post("/mcp", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
    }, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "synthetic-client", "version": "1"}},
    })
    _assert_authentication_unavailable(response, caplog)

    provider.failure = None
    assert _query(client, _mcp_headers(client, token)).status_code == 200


def test_mcp_identity_lookup_failure_does_not_invalidate_credentials(
    unavailable_provider_journey, caplog,
):
    client, provider, db = unavailable_provider_journey
    headers = _mcp_headers(client, provider.issue_token())
    conn = connect(db)
    try:
        conn.execute("ALTER TABLE users RENAME TO temporarily_unavailable_users")
        _assert_authentication_unavailable(_query(client, headers), caplog)
    finally:
        conn.execute("ALTER TABLE temporarily_unavailable_users RENAME TO users")
        conn.close()
    assert _query(client, headers).status_code == 200
