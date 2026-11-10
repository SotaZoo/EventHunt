from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from api.integrations.supabase.auth import SupabaseAuthGateway
from api.mcp.availability import AuthenticationUnavailable
from api.mcp.server import SupabaseMCPTokenVerifier
from api.repositories.events import connect
from api.repositories.identities import create_or_accept_supabase_user
from api.repositories.invites import create_alpha_invite
from api.tests.fixtures.web_helpers import migrated_db

ISSUER = "https://project-ref.supabase.co/auth/v1"
RESOURCE = "https://events.example.com/mcp"


def _key(kid: str):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    public_jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private, public_jwk


def _token(private, kid: str, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": RESOURCE,
        "sub": "supabase-sub-1",
        "client_id": str(uuid4()),
        "scope": "openid email",
        "session_id": "session-1",
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": kid})


def _db(tmp_path: Path) -> Path:
    db = migrated_db(tmp_path)
    conn = connect(db)
    create_alpha_invite(conn, email="friend@example.com")
    user = create_or_accept_supabase_user(
        conn,
        subject="supabase-sub-1",
        email="friend@example.com",
        email_verified=True,
        display_name="Friend",
    )
    assert user is not None
    conn.close()
    return db


def _verifier(tmp_path: Path, handler, *, userinfo_handler=None, jwks_ttl_seconds=600):
    def provider(request):
        if request.url.path.endswith("/oauth/userinfo"):
            if userinfo_handler is not None:
                return userinfo_handler(request)
            return httpx.Response(200, json={"sub": "supabase-sub-1"})
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    gateway = SupabaseAuthGateway(
        project_url="https://project-ref.supabase.co",
        publishable_key="sb_publishable_test", origin="https://events.example.com",
        http_client=client,
    )
    return SupabaseMCPTokenVerifier(
        issuer_url=ISSUER,
        jwks_url=f"{ISSUER}/.well-known/jwks.json",
        resource_url=RESOURCE,
        auth_gateway=gateway,
        db_path=_db(tmp_path),
        http_client=client,
        jwks_ttl_seconds=jwks_ttl_seconds,
    ), client


@pytest.mark.asyncio
async def test_rechecks_provider_session_and_rejects_revoked_unexpired_token(tmp_path):
    private, public_jwk = _key("kid-1")
    session_active = True
    userinfo_calls = 0

    def userinfo(request):
        nonlocal userinfo_calls
        userinfo_calls += 1
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(
            200 if session_active else 403,
            json={"sub": "supabase-sub-1"} if session_active else {"code": "session_not_found"},
        )

    verifier, client = _verifier(
        tmp_path, lambda _: httpx.Response(200, json={"keys": [public_jwk]}),
        userinfo_handler=userinfo,
    )
    token = _token(private, "kid-1")
    try:
        assert await verifier.verify_token(token) is not None
        session_active = False
        assert await verifier.verify_token(token) is None
    finally:
        await client.aclose()
    assert userinfo_calls == 2


@pytest.mark.asyncio
async def test_accepts_asymmetric_oauth_token_for_active_invited_user(tmp_path: Path) -> None:
    private, public_jwk = _key("kid-1")
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"keys": [public_jwk]})

    verifier, client = _verifier(tmp_path, handler)
    try:
        access = await verifier.verify_token(_token(private, "kid-1"))
        again = await verifier.verify_token(_token(private, "kid-1"))
    finally:
        await client.aclose()

    assert access is not None
    assert access.subject == "supabase-sub-1"
    assert access.resource == RESOURCE
    assert access.scopes == ["openid", "email"]
    assert access.client_id
    assert again is not None
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://evil.example/auth/v1"},
        {"aud": "authenticated"},
        {"scope": "openid profile"},
        {"client_id": None},
        {"exp": int(time.time()) - 1},
        {"sub": None},
        {"session_id": None},
        {"session_id": ""},
    ],
)
async def test_rejects_wrong_or_ordinary_web_token(tmp_path: Path, overrides: dict) -> None:
    private, public_jwk = _key("kid-1")
    verifier, client = _verifier(
        tmp_path,
        lambda _request: httpx.Response(200, json={"keys": [public_jwk]}),
    )
    try:
        assert await verifier.verify_token(_token(private, "kid-1", **overrides)) is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_rejects_inactive_or_unlinked_subject(tmp_path: Path) -> None:
    private, public_jwk = _key("kid-1")
    verifier, client = _verifier(
        tmp_path,
        lambda _request: httpx.Response(200, json={"keys": [public_jwk]}),
    )
    conn = connect(verifier._db_path)
    conn.execute("UPDATE users SET is_active = 0")
    conn.close()
    try:
        assert await verifier.verify_token(_token(private, "kid-1")) is None
        assert await verifier.verify_token(_token(private, "kid-1", sub="unknown-subject")) is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unknown_kid_refreshes_jwks_once_then_fails_closed(tmp_path: Path) -> None:
    private_1, public_1 = _key("kid-1")
    private_2, public_2 = _key("kid-2")
    responses = [public_1, public_2]
    calls = 0

    def handler(_request):
        nonlocal calls
        key = responses[min(calls, 1)]
        calls += 1
        return httpx.Response(200, json={"keys": [key]})

    verifier, client = _verifier(tmp_path, handler)
    try:
        assert await verifier.verify_token(_token(private_1, "kid-1")) is not None
        assert await verifier.verify_token(_token(private_2, "kid-2")) is not None
        assert await verifier.verify_token(_token(private_2, "never-known", sub="x")) is None
    finally:
        await client.aclose()

    assert calls == 3


@pytest.mark.asyncio
async def test_jwks_failure_is_unavailable_not_an_invalid_credential(tmp_path: Path) -> None:
    private, _public = _key("kid-1")

    def handler(request):
        raise httpx.ReadTimeout("private token detail", request=request)

    verifier, client = _verifier(tmp_path, handler)
    try:
        with pytest.raises(AuthenticationUnavailable) as caught:
            await verifier.verify_token(_token(private, "kid-1"))
        assert caught.value.dependency == "signing_keys"
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(401, json={"code": "session_not_found"}),
    httpx.Response(403, json={"code": "session_not_found"}),
    httpx.Response(200, json={"sub": "another-user"}),
])
async def test_provider_session_failure_or_subject_mismatch_fails_closed(tmp_path, response):
    private, public_jwk = _key("kid-1")
    verifier, client = _verifier(
        tmp_path, lambda _: httpx.Response(200, json={"keys": [public_jwk]}),
        userinfo_handler=lambda _: response,
    )
    try:
        assert await verifier.verify_token(_token(private, "kid-1")) is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_provider_session_timeout_is_unavailable_without_logging_token(tmp_path, caplog):
    private, public_jwk = _key("kid-1")

    def unavailable(request):
        raise httpx.ReadTimeout("private-token-marker", request=request)

    verifier, client = _verifier(
        tmp_path, lambda _: httpx.Response(200, json={"keys": [public_jwk]}),
        userinfo_handler=unavailable,
    )
    try:
        with pytest.raises(AuthenticationUnavailable) as caught:
            await verifier.verify_token(_token(private, "kid-1"))
        assert caught.value.dependency == "provider_session"
    finally:
        await client.aclose()
    assert "private-token-marker" not in caplog.text


@pytest.mark.asyncio
async def test_unknown_kid_refresh_failure_keeps_cached_valid_keys(tmp_path: Path) -> None:
    private, public_jwk = _key("kid-1")
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"keys": [public_jwk]})
        raise httpx.ReadTimeout("temporary JWKS failure", request=request)

    verifier, client = _verifier(tmp_path, handler)
    try:
        known_token = _token(private, "kid-1")
        assert await verifier.verify_token(known_token) is not None
        with pytest.raises(AuthenticationUnavailable) as caught:
            await verifier.verify_token(_token(private, "unknown-kid"))
        assert caught.value.dependency == "signing_keys"
        assert await verifier.verify_token(known_token) is not None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_expired_jwks_cache_recovers_without_replacing_credentials(tmp_path: Path) -> None:
    private, public_jwk = _key("kid-1")
    unavailable = False

    def handler(_request):
        if unavailable:
            return httpx.Response(503)
        return httpx.Response(200, json={"keys": [public_jwk]})

    verifier, client = _verifier(tmp_path, handler, jwks_ttl_seconds=0)
    token = _token(private, "kid-1")
    try:
        assert await verifier.verify_token(token) is not None
        unavailable = True
        with pytest.raises(AuthenticationUnavailable) as caught:
            await verifier.verify_token(token)
        assert caught.value.dependency == "signing_keys"
        unavailable = False
        assert await verifier.verify_token(token) is not None
    finally:
        await client.aclose()
