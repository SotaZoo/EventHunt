from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from api.integrations.supabase.auth import SupabaseAuthError, SupabaseAuthGateway

PROJECT_URL = "https://project-ref.supabase.co"
PUBLISHABLE_KEY = "sb_publishable_gate0"
ORIGIN = "https://events.example.com"


def _sdk_response(*, refresh_token: str = "refresh-2") -> SimpleNamespace:
    user = SimpleNamespace(
        id="supabase-sub-1",
        email="friend@example.com",
        email_confirmed_at=datetime(2030, 1, 1, tzinfo=UTC),
        user_metadata={"full_name": "Friend"},
    )
    session = SimpleNamespace(
        access_token="access-2",
        refresh_token=refresh_token,
        expires_at=1_893_456_000,
        user=user,
    )
    return SimpleNamespace(session=session, user=user)


class FakeAuth:
    def __init__(self, storage) -> None:
        self.storage = storage
        self.oauth_credentials = None
        self.exchange_params = None
        self.refresh_token = None
        self.logged_out = None
        self.admin = SimpleNamespace(sign_out=self.sign_out)

    async def sign_in_with_oauth(self, credentials):
        self.oauth_credentials = credentials
        await self.storage.set_item("fake-code-verifier", "v" * 64)
        return SimpleNamespace(
            url="https://project-ref.supabase.co/auth/v1/authorize?provider=google"
        )

    async def exchange_code_for_session(self, params):
        self.exchange_params = params
        return _sdk_response()

    async def refresh_session(self, refresh_token):
        self.refresh_token = refresh_token
        return _sdk_response(refresh_token="rotated-refresh-token")

    async def get_user(self, access_token):
        return SimpleNamespace(user=_sdk_response().user)

    async def sign_out(self, access_token, scope):
        self.logged_out = (access_token, scope)


class FakeSdkFactory:
    def __init__(self) -> None:
        self.auth_clients: list[FakeAuth] = []

    async def __call__(self, url, key, options):
        assert url == PROJECT_URL
        assert key == PUBLISHABLE_KEY
        assert options.flow_type == "pkce"
        assert options.auto_refresh_token is False
        auth = FakeAuth(options.storage)
        self.auth_clients.append(auth)
        return SimpleNamespace(auth=auth)


@pytest.mark.asyncio
async def test_google_start_uses_sdk_pkce_and_returns_verifier() -> None:
    factory = FakeSdkFactory()
    gateway = SupabaseAuthGateway(
        project_url=PROJECT_URL,
        publishable_key=PUBLISHABLE_KEY,
        origin=ORIGIN,
        sdk_factory=factory,
    )

    started = await gateway.start_google_sign_in(
        "https://events.example.com/auth/supabase/callback?state=opaque-state"
    )

    assert started.authorization_url.startswith(PROJECT_URL)
    assert started.code_verifier == "v" * 64
    credentials = factory.auth_clients[0].oauth_credentials
    assert credentials["provider"] == "google"
    assert credentials["options"]["redirect_to"].endswith("state=opaque-state")
    assert credentials["options"]["scopes"] == "openid email profile"
    assert credentials["options"]["query_params"] == {"prompt": "select_account"}


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,providers,confirmed,google', [
    ('google', [], True, True), ('email', ['email', 'google'], True, True),
    ('email', ['email'], True, False), ('email', 'google', True, False),
    (None, [], False, False),
])
async def test_identity_admission_uses_server_owned_facts(provider, providers, confirmed, google):
    user = SimpleNamespace(id='subject', email='new@gmail.com', email_confirmed_at='confirmed' if confirmed else None,
                           user_metadata={'email_verified': True, 'provider': 'google', 'providers': ['google']},
                           app_metadata={'provider': provider, 'providers': providers})

    async def get_user(_token):
        return SimpleNamespace(user=user)

    async def sdk_factory(*_args):
        return SimpleNamespace(auth=SimpleNamespace(get_user=get_user))

    gateway = SupabaseAuthGateway(project_url=PROJECT_URL, publishable_key=PUBLISHABLE_KEY,
                                  origin=ORIGIN, sdk_factory=sdk_factory)
    identity = await gateway.get_identity('synthetic-access')
    assert identity.email_verified is confirmed
    assert identity.has_google_identity is google


@pytest.mark.asyncio
async def test_code_exchange_and_refresh_return_rotated_request_scoped_sessions() -> None:
    factory = FakeSdkFactory()
    gateway = SupabaseAuthGateway(
        project_url=PROJECT_URL,
        publishable_key=PUBLISHABLE_KEY,
        origin=ORIGIN,
        sdk_factory=factory,
    )

    exchanged = await gateway.exchange_code(
        auth_code="auth-code",
        code_verifier="v" * 64,
        redirect_uri="https://events.example.com/auth/supabase/callback?state=opaque-state",
    )
    refreshed = await gateway.refresh_session("refresh-1")

    assert exchanged.identity.subject == "supabase-sub-1"
    assert exchanged.identity.email_verified is True
    assert exchanged.identity.display_name == "Friend"
    assert factory.auth_clients[0].exchange_params == {
        "auth_code": "auth-code",
        "code_verifier": "v" * 64,
        "redirect_to": "https://events.example.com/auth/supabase/callback?state=opaque-state",
    }
    assert refreshed.refresh_token == "rotated-refresh-token"
    assert factory.auth_clients[1].refresh_token == "refresh-1"


@pytest.mark.asyncio
async def test_logout_uses_sdk_local_session_revocation() -> None:
    factory = FakeSdkFactory()
    gateway = SupabaseAuthGateway(
        project_url=PROJECT_URL,
        publishable_key=PUBLISHABLE_KEY,
        origin=ORIGIN,
        sdk_factory=factory,
    )

    await gateway.logout("access-token")

    assert factory.auth_clients[0].logged_out == ("access-token", "local")


@pytest.mark.asyncio
async def test_consent_and_grant_rest_calls_use_user_token_key_and_origin() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer access-token"
        assert request.headers["apikey"] == PUBLISHABLE_KEY
        assert request.headers["origin"] == ORIGIN
        if request.method == "GET" and "/oauth/authorizations/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "authorization_id": "authorization-1",
                    "redirect_uri": "http://127.0.0.1:1455/callback",
                    "client": {"id": "client-1", "name": "Codex", "uri": "https://openai.com"},
                    "user": {"id": "supabase-sub-1", "email": "friend@example.com"},
                    "scope": "openid email",
                },
            )
        if request.method == "POST":
            assert request.read() == b'{"action":"approve"}'
            return httpx.Response(
                200, json={"redirect_url": "http://127.0.0.1:1455/callback?code=x"}
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "client": {"id": "client-1", "name": "Codex", "uri": "https://openai.com"},
                        "scopes": ["openid", "email"],
                        "granted_at": "2030-01-01T00:00:00Z",
                    }
                ],
            )
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        gateway = SupabaseAuthGateway(
            project_url=PROJECT_URL,
            publishable_key=PUBLISHABLE_KEY,
            origin=ORIGIN,
            http_client=http_client,
        )
        details = await gateway.get_authorization_details("authorization-1", "access-token")
        redirect = await gateway.submit_consent("authorization-1", "approve", "access-token")
        grants = await gateway.list_grants("access-token")
        await gateway.revoke_grant("client-1", "access-token")

    assert details.client_name == "Codex"
    assert details.scopes == ("openid", "email")
    assert redirect.startswith("http://127.0.0.1:1455/callback")
    assert grants[0].client_id == "client-1"
    assert [request.method for request in requests] == ["GET", "POST", "GET", "DELETE"]


@pytest.mark.asyncio
async def test_rest_failures_are_bounded_and_do_not_echo_sensitive_values() -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("authorization-secret access-token", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(failing_handler)) as http_client:
        gateway = SupabaseAuthGateway(
            project_url=PROJECT_URL,
            publishable_key=PUBLISHABLE_KEY,
            origin=ORIGIN,
            http_client=http_client,
        )
        with pytest.raises(SupabaseAuthError) as caught:
            await gateway.get_authorization_details("authorization-secret", "access-token")

    message = str(caught.value)
    assert "authorization-secret" not in message
    assert "access-token" not in message
    assert "request_id=" in message
