"""Synthetic external Supabase boundary for browser and full MCP journey tests.

Only test factories use this provider. Production routes and token verification
remain unchanged; storage, cookies, CSRF and the MCP HTTP server are real.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from api.integrations.supabase.auth import (
    SupabaseAuthError,
    SupabaseAuthGateway,
    SupabaseIdentity,
    SupabaseOAuthStart,
    SupabaseSession,
)

PROJECT = "https://synthetic-provider.supabase.co"
ISSUER = f"{PROJECT}/auth/v1"
WEB_ACCESS = "synthetic-web-access"
WEB_REFRESH = "synthetic-web-refresh"
CLIENT_ID = "example-ai-client"


class OAuthProviderFixture(SupabaseAuthGateway):
    def __init__(
        self, *, resource_url: str, email: str = "discovery@example.test", browser_origin: str | None = None,
    ) -> None:
        self.resource_url = resource_url
        self.identity = SupabaseIdentity("synthetic-subject", email, True, "Test Friend", has_google_identity=True)
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_jwk = RSAAlgorithm.to_jwk(self.private_key.public_key(), as_dict=True)
        self.public_jwk.update(kid="synthetic-key", alg="RS256", use="sig")
        self.grants: set[str] = set()
        self.sessions: dict[str, str] = {}
        self.web_session_active = True
        self.google_url = f"{browser_origin}/_test/provider/google" if browser_origin else "https://identity.example.test/google"
        self.client_callback = f"{browser_origin}/_test/ai/callback" if browser_origin else "https://ai.example.test/callback"
        self.provider_http = httpx.AsyncClient(transport=httpx.MockTransport(self._provider))
        super().__init__(
            project_url=PROJECT, publishable_key="sb_publishable_synthetic",
            origin=resource_url.removesuffix("/mcp"), http_client=self.provider_http,
        )

    def issue_token(self, client_id: str = CLIENT_ID) -> str:
        session_id = str(uuid4())
        self.sessions[session_id] = client_id
        self.grants.add(client_id)
        return jwt.encode({
            "iss": ISSUER, "aud": self.resource_url, "sub": self.identity.subject,
            "scope": "openid email", "client_id": client_id, "session_id": session_id,
            "iat": int(time.time()), "exp": int(time.time()) + 3600,
        }, self.private_key, algorithm="RS256", headers={"kid": "synthetic-key"})

    def _provider(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        if path.endswith("/.well-known/jwks.json"):
            return httpx.Response(200, json={"keys": [self.public_jwk]})
        if path.endswith("/oauth/userinfo"):
            try:
                claims = jwt.decode(
                    bearer, self.private_key.public_key(), algorithms=["RS256"],
                    issuer=ISSUER, audience=self.resource_url,
                )
                if self.sessions.get(claims["session_id"]) == claims["client_id"]:
                    return httpx.Response(200, json={"sub": claims["sub"]})
            except (jwt.PyJWTError, KeyError):
                pass
            return httpx.Response(403, json={"code": "session_not_found"})
        if not bearer.startswith(WEB_ACCESS) or not self.web_session_active:
            return httpx.Response(401, json={"code": "session_not_found"})
        if path.endswith("/user/oauth/grants"):
            if bearer.endswith("-grants-error"):
                return httpx.Response(503)
            if request.method == "DELETE":
                if bearer.endswith("-remove-error"):
                    return httpx.Response(503)
                client_id = request.url.params["client_id"]
                self.grants.discard(client_id)
                self.sessions = {key: value for key, value in self.sessions.items() if value != client_id}
                return httpx.Response(204)
            return httpx.Response(200, json=[{
                "client": {"id": client_id, "name": (
                    "Example AI connection with a deliberately long client name for text testing"
                    if bearer.endswith("-long-name")
                    else "Example AI" if client_id == CLIENT_ID else "Other AI"
                )},
                "scopes": ["openid", "email"], "granted_at": "2030-01-01T00:00:00Z",
            } for client_id in sorted(self.grants)])
        if "/oauth/authorizations/" in path:
            if "expired-request" in path:
                return httpx.Response(404)
            if request.method == "POST":
                action = json.loads(request.content)["action"]
                if action == "approve":
                    self.grants.add(CLIENT_ID)
                return httpx.Response(200, json={
                    "redirect_url": f"{self.client_callback}?decision={action}",
                })
            return httpx.Response(200, json={
                "authorization_id": path.rsplit("/", 1)[-1],
                "redirect_uri": self.client_callback,
                "client": {"id": CLIENT_ID, "name": "Example AI"},
                "user": {"id": self.identity.subject, "email": self.identity.email},
                "scope": "openid email",
            })
        return httpx.Response(404)

    async def start_google_sign_in(self, redirect_uri: str) -> SupabaseOAuthStart:
        return SupabaseOAuthStart(
            self.google_url + "?" + urlencode({"callback": redirect_uri}), "v" * 64,
        )

    async def exchange_code(self, *, auth_code: str, code_verifier: str, redirect_uri: str) -> SupabaseSession:
        if auth_code != "synthetic-auth-code" or code_verifier != "v" * 64:
            raise SupabaseAuthError("Synthetic provider rejected code exchange")
        self.web_session_active = True
        return self._web_session()

    def _web_session(self) -> SupabaseSession:
        return SupabaseSession(
            WEB_ACCESS, WEB_REFRESH, datetime.now(UTC) + timedelta(hours=1), self.identity,
        )

    async def get_identity(self, access_token: str) -> SupabaseIdentity:
        if not access_token.startswith(WEB_ACCESS) or not self.web_session_active:
            raise SupabaseAuthError("Synthetic web session is unavailable")
        return self.identity

    async def refresh_session(self, refresh_token: str) -> SupabaseSession:
        if refresh_token != WEB_REFRESH or not self.web_session_active:
            raise SupabaseAuthError("Synthetic web session cannot refresh")
        return self._web_session()

    async def logout(self, access_token: str) -> None:
        self.web_session_active = False
