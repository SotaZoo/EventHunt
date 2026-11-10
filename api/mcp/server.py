"""Authenticated MCP server for Event Hunt's ranked event catalog."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl, Field, JsonValue, WithJsonSchema

from api.integrations.supabase.auth import (
    SupabaseAuthError,
    SupabaseAuthGateway,
    SupabaseAuthUnavailable,
)
from api.mcp.availability import AuthenticationUnavailable
from api.repositories import _storage as storage
from api.services.event_catalog import EventCatalogResult, InvalidEventQuery
from api.services.event_catalog import find_events as query_events

LOGGER = logging.getLogger(__name__)
READ_SCOPE = "events:read"
SUPABASE_READ_SCOPE = "email"
ASYMMETRIC_JWT_ALGORITHMS = {"RS256", "ES256"}


class StaticAgentTokenVerifier(TokenVerifier):
    """Verify the private bootstrap credential used by Yang's agent."""

    def __init__(self, *, token: str, resource_url: str) -> None:
        self._token = token
        self._resource_url = resource_url

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the fixed read-only identity when the bearer token matches."""
        if not secrets.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id="event-scout-agent",
            scopes=[READ_SCOPE],
            resource=self._resource_url,
            subject="yang",
        )


class SupabaseMCPTokenVerifier(TokenVerifier):
    """Verify Supabase OAuth-server JWTs and the linked Event Hunt user."""

    def __init__(
        self,
        *,
        issuer_url: str,
        jwks_url: str,
        resource_url: str,
        auth_gateway: SupabaseAuthGateway,
        db_path: Path | None = None,
        database: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        jwks_ttl_seconds: int = 600,
    ) -> None:
        self._issuer_url = issuer_url.rstrip("/")
        self._jwks_url = jwks_url
        self._resource_url = resource_url
        self._auth_gateway = auth_gateway
        self._db_path = db_path
        self._database = database
        self._http_client = http_client
        self._jwks_ttl_seconds = jwks_ttl_seconds
        self._keys: dict[str, jwt.PyJWK] = {}
        self._jwks_loaded_at = 0.0
        self._jwks_lock = asyncio.Lock()

    async def _fetch_jwks(self, *, force: bool = False) -> None:
        async with self._jwks_lock:
            if (
                not force
                and self._keys
                and time.monotonic() - self._jwks_loaded_at < self._jwks_ttl_seconds
            ):
                return
            try:
                if self._http_client is not None:
                    response = await self._http_client.get(
                        self._jwks_url,
                        timeout=httpx.Timeout(8.0, connect=3.0),
                    )
                else:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
                        response = await client.get(self._jwks_url)
                response.raise_for_status()
                payload = response.json()
                keys = payload.get("keys") if isinstance(payload, dict) else None
                if not isinstance(keys, list):
                    raise ValueError("JWKS keys are missing")
                parsed = {
                    str(key["kid"]): jwt.PyJWK.from_dict(key)
                    for key in keys
                    if isinstance(key, dict)
                    and isinstance(key.get("kid"), str)
                    and key.get("alg") in ASYMMETRIC_JWT_ALGORITHMS
                }
                if not parsed:
                    raise ValueError("JWKS contains no supported signing keys")
            except (httpx.HTTPError, jwt.PyJWTError, ValueError, TypeError):
                raise AuthenticationUnavailable("signing_keys") from None
            self._keys = parsed
            self._jwks_loaded_at = time.monotonic()

    async def _key_for(self, kid: str) -> jwt.PyJWK | None:
        await self._fetch_jwks()
        key = self._keys.get(kid)
        if key is not None:
            return key
        await self._fetch_jwks(force=True)
        return self._keys.get(kid)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            algorithm = header.get("alg")
            if not isinstance(kid, str) or algorithm not in ASYMMETRIC_JWT_ALGORITHMS:
                return None
            key = await self._key_for(kid)
            if key is None:
                return None
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],
                issuer=self._issuer_url,
                options={
                    "verify_aud": False,
                    "require": ["iss", "aud", "sub", "exp", "client_id", "scope", "session_id"],
                },
            )
        except (jwt.PyJWTError, ValueError, TypeError):
            return None

        if claims.get("aud") != self._resource_url:
            return None
        subject = claims.get("sub")
        client_id = claims.get("client_id")
        raw_scope = claims.get("scope")
        session_id = claims.get("session_id")
        if not isinstance(subject, str) or not subject:
            return None
        if not isinstance(client_id, str) or not client_id:
            return None
        if not isinstance(raw_scope, str):
            return None
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        scopes = [scope for scope in raw_scope.split() if scope]
        if SUPABASE_READ_SCOPE not in scopes:
            return None

        connection = None
        try:
            connection = (
                self._database.open_compat()
                if self._database is not None
                else storage.connect(self._db_path)
            )
            user = storage.get_user_by_identity(
                connection,
                provider="supabase",
                subject=subject,
            )
        except Exception:
            raise AuthenticationUnavailable("identity_store") from None
        finally:
            if connection is not None:
                connection.close()
        if user is None:
            return None
        # JWT validity alone survives grant removal until exp. UserInfo also
        # validates Supabase's current session row, deleted by client revocation.
        # Never cache success here: removal must block the next HTTP request.
        try:
            if await self._auth_gateway.get_oauth_subject(token) != subject:
                return None
        except SupabaseAuthUnavailable:
            raise AuthenticationUnavailable("provider_session") from None
        except SupabaseAuthError:
            LOGGER.warning("MCP provider session validation failed")
            return None
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self._resource_url,
            subject=subject,
            claims=claims,
        )


def transport_security_for(resource_url: str) -> TransportSecuritySettings:
    """Allow only the configured Event Hunt host and origin."""
    parsed = urlsplit(resource_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[parsed.netloc],
        allowed_origins=[origin],
    )


def create_event_scout_mcp(
    *,
    token: str | None = None,
    token_verifier: TokenVerifier | None = None,
    issuer_url: str | None = None,
    required_scopes: list[str] | None = None,
    resource_url: str,
    db_path: Path | None = None,
    database: Any | None = None,
) -> MCPServer:
    """Build the one-tool Event Hunt MCP server from the SDK bearer-auth pattern."""
    parsed = urlsplit(resource_url)
    resolved_issuer_url = issuer_url or f"{parsed.scheme}://{parsed.netloc}/"
    if token_verifier is None:
        if token is None:
            raise ValueError("token or token_verifier is required")
        token_verifier = StaticAgentTokenVerifier(token=token, resource_url=resource_url)
    mcp = MCPServer(
        "event-scout",
        title="Event Hunt",
        description="Read-only access to Event Hunt's ranked event catalog.",
        instructions=(
            "Use find_events for a bounded ranked shortlist. Rankings use the shared "
            "internal profile and do not reflect registration, approval, inbox, or calendar state."
            " Source registration availability is the last verified provider fact, not your registration."
            " Disclose any time_warning and confirm the date/time with the organizer."
        ),
        version="0.1.0",
        token_verifier=token_verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(resolved_issuer_url),
            resource_server_url=AnyHttpUrl(resource_url),
            required_scopes=required_scopes or [READ_SCOPE],
            validate_token_resource=True,
        ),
    )

    @mcp.tool(
        title="Find ranked events",
        description=(
            "Return Event Hunt's shared internal ranking for a bounded time window. "
            "Start and end must be timezone-aware ISO-8601 timestamps, start must be "
            "before end, and limit must be from 1 through 25. "
            "The tool does not know registration, approval, inbox, or calendar state."
            " It excludes confirmed unavailable occurrences and includes joinable waitlists."
            " Unknown source availability is not proof that registration is open."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            open_world_hint=False,
        ),
    )
    def find_events(
        start: Annotated[
            str,
            Field(description="Inclusive timezone-aware ISO-8601 start timestamp."),
        ],
        end: Annotated[
            str,
            Field(description="Inclusive timezone-aware ISO-8601 end timestamp; must follow start."),
        ],
        limit: Annotated[
            JsonValue,
            WithJsonSchema({"type": "integer"}),
            Field(description="Maximum ranked events to return, from 1 through 25."),
        ] = 5,
    ) -> EventCatalogResult:
        """Read an already-curated, bounded event shortlist without external calls."""
        connection = None
        try:
            connection = database.open_compat() if database is not None else storage.connect(db_path)
            return query_events(connection, start=start, end=end, limit=limit)
        except InvalidEventQuery as exc:
            raise ToolError(f"invalid_query: {exc}") from exc
        except Exception as exc:
            LOGGER.error("Event catalog query failed: %s", type(exc).__name__)
            raise ToolError("catalog_unavailable: Event Hunt could not read the catalog") from exc
        finally:
            if connection is not None:
                connection.close()

    return mcp
