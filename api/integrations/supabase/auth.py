"""Narrow request-scoped adapter around Supabase Auth.

The official Python SDK owns social sign-in, PKCE exchange, refresh, identity
lookup, and logout. OAuth-server methods that the Python SDK does not
yet expose use Supabase Auth's official REST API here and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from supabase import AsyncClientOptions, acreate_client
from supabase_auth import AsyncMemoryStorage

ConsentAction = Literal["approve", "deny"]
GOOGLE_SCOPES = "openid email profile"


class SupabaseAuthError(RuntimeError):
    """Safe, redacted failure at the Supabase Auth boundary."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SupabaseAuthUnavailable(SupabaseAuthError):
    """The provider could not establish whether a session is valid."""


@dataclass(frozen=True)
class SupabaseIdentity:
    subject: str
    email: str
    email_verified: bool
    display_name: str | None
    has_google_identity: bool = False


@dataclass(frozen=True)
class SupabaseSession:
    access_token: str
    refresh_token: str
    expires_at: datetime | None
    identity: SupabaseIdentity


@dataclass(frozen=True)
class SupabaseOAuthStart:
    authorization_url: str
    code_verifier: str


@dataclass(frozen=True)
class AuthorizationDetails:
    authorization_id: str | None
    redirect_uri: str | None
    client_id: str | None
    client_name: str | None
    client_uri: str | None
    user_subject: str | None
    user_email: str | None
    scopes: tuple[str, ...]
    redirect_url: str | None = None


@dataclass(frozen=True)
class OAuthGrant:
    client_id: str
    client_name: str | None
    client_uri: str | None
    scopes: tuple[str, ...]
    granted_at: str | None


class SupabaseAuthGateway:
    """One small Supabase Auth gateway with no process-global user session."""

    def __init__(
        self,
        *,
        project_url: str,
        publishable_key: str,
        origin: str,
        sdk_factory=acreate_client,  # type: ignore[no-untyped-def]
        http_client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._project_url = project_url.rstrip("/")
        self._publishable_key = publishable_key
        self._origin = origin.rstrip("/")
        self._sdk_factory = sdk_factory
        self._http_client = http_client
        self._timeout = timeout or httpx.Timeout(8.0, connect=3.0)

    async def _new_sdk(self, storage: AsyncMemoryStorage, sdk_http: httpx.AsyncClient):
        options = AsyncClientOptions(
            auto_refresh_token=False,
            persist_session=True,
            flow_type="pkce",
            storage=storage,
            httpx_client=sdk_http,
        )
        return await self._sdk_factory(
            self._project_url,
            self._publishable_key,
            options,
        )

    @staticmethod
    def _safe_error(label: str, exc: Exception) -> SupabaseAuthError:
        request_id = uuid4().hex
        return SupabaseAuthError(f"{label} failed (request_id={request_id})")

    async def start_google_sign_in(self, redirect_uri: str) -> SupabaseOAuthStart:
        storage = AsyncMemoryStorage()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as sdk_http:
                sdk = await self._new_sdk(storage, sdk_http)
                result = await sdk.auth.sign_in_with_oauth(
                    {
                        "provider": "google",
                        "options": {
                            "redirect_to": redirect_uri,
                            "scopes": GOOGLE_SCOPES,
                            "query_params": {"prompt": "select_account"},
                        },
                    }
                )
        except Exception as exc:
            raise self._safe_error("Supabase Google sign-in", exc) from exc

        verifier = next(
            (value for key, value in storage.storage.items() if key.endswith("code-verifier")),
            None,
        )
        authorization_url = str(getattr(result, "url", "") or "").strip()
        if not verifier or not authorization_url:
            raise SupabaseAuthError(
                f"Supabase Google sign-in returned an incomplete response "
                f"(request_id={uuid4().hex})"
            )
        return SupabaseOAuthStart(
            authorization_url=authorization_url,
            code_verifier=verifier,
        )

    async def exchange_code(
        self,
        *,
        auth_code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> SupabaseSession:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as sdk_http:
                sdk = await self._new_sdk(AsyncMemoryStorage(), sdk_http)
                response = await sdk.auth.exchange_code_for_session(
                    {
                        "auth_code": auth_code,
                        "code_verifier": code_verifier,
                        "redirect_to": redirect_uri,
                    }
                )
        except Exception as exc:
            raise self._safe_error("Supabase code exchange", exc) from exc
        return self._parse_session(response)

    async def refresh_session(self, refresh_token: str) -> SupabaseSession:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as sdk_http:
                sdk = await self._new_sdk(AsyncMemoryStorage(), sdk_http)
                response = await sdk.auth.refresh_session(refresh_token)
        except Exception as exc:
            raise self._safe_error("Supabase session refresh", exc) from exc
        return self._parse_session(response)

    async def get_identity(self, access_token: str) -> SupabaseIdentity:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as sdk_http:
                sdk = await self._new_sdk(AsyncMemoryStorage(), sdk_http)
                response = await sdk.auth.get_user(access_token)
        except Exception as exc:
            raise self._safe_error("Supabase identity lookup", exc) from exc
        user = getattr(response, "user", None)
        return self._parse_identity(user)

    async def logout(self, access_token: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as sdk_http:
                sdk = await self._new_sdk(AsyncMemoryStorage(), sdk_http)
                await sdk.auth.admin.sign_out(access_token, "local")
        except Exception as exc:
            raise self._safe_error("Supabase logout", exc) from exc

    @classmethod
    def _parse_session(cls, response: Any) -> SupabaseSession:
        session = getattr(response, "session", None)
        user = getattr(response, "user", None) or getattr(session, "user", None)
        access_token = str(getattr(session, "access_token", "") or "").strip()
        refresh_token = str(getattr(session, "refresh_token", "") or "").strip()
        if session is None or not access_token or not refresh_token:
            raise SupabaseAuthError(
                f"Supabase Auth returned an incomplete session (request_id={uuid4().hex})"
            )
        raw_expires_at = getattr(session, "expires_at", None)
        expires_at = None
        if isinstance(raw_expires_at, (float, int)):
            expires_at = datetime.fromtimestamp(raw_expires_at, tz=UTC)
        return SupabaseSession(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            identity=cls._parse_identity(user),
        )

    @staticmethod
    def _parse_identity(user: Any) -> SupabaseIdentity:
        subject = str(getattr(user, "id", "") or "").strip()
        email = str(getattr(user, "email", "") or "").strip()
        metadata = getattr(user, "user_metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        display_name = str(metadata.get("full_name") or metadata.get("name") or "").strip() or None
        # User metadata is editable; authorization uses only server-owned facts.
        email_verified = bool(getattr(user, "email_confirmed_at", None))
        app_metadata = getattr(user, "app_metadata", None)
        app_metadata = app_metadata if isinstance(app_metadata, dict) else {}
        providers = app_metadata.get("providers", [])
        has_google_identity = (app_metadata.get("provider") == "google"
                               or (isinstance(providers, list) and "google" in providers))
        if not subject or not email:
            raise SupabaseAuthError(
                f"Supabase Auth returned an incomplete identity (request_id={uuid4().hex})"
            )
        return SupabaseIdentity(
            subject=subject,
            email=email,
            email_verified=email_verified,
            display_name=display_name,
            has_google_identity=has_google_identity,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        access_token: str,
        json: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        expected_statuses: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "apikey": self._publishable_key,
            "Origin": self._origin,
            "Accept": "application/json",
        }
        url = f"{self._project_url}/auth/v1/{path.lstrip('/')}"
        try:
            if self._http_client is not None:
                response = await self._http_client.request(
                    method,
                    url,
                    headers=headers,
                    json=json,
                    params=params,
                    timeout=self._timeout,
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=headers,
                        json=json,
                        params=params,
                    )
        except httpx.HTTPError as exc:
            raise self._safe_error("Supabase Auth request", exc) from exc
        if response.status_code not in expected_statuses:
            raise SupabaseAuthError(
                f"Supabase Auth request failed with status {response.status_code} "
                f"(request_id={uuid4().hex})",
                status_code=response.status_code,
            )
        return response

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise SupabaseAuthError(
                f"Supabase Auth returned invalid JSON (request_id={uuid4().hex})"
            ) from exc

    async def get_authorization_details(
        self,
        authorization_id: str,
        access_token: str,
    ) -> AuthorizationDetails:
        response = await self._request(
            "GET",
            f"oauth/authorizations/{quote(authorization_id, safe='')}",
            access_token=access_token,
        )
        data = self._json(response)
        if not isinstance(data, dict):
            raise SupabaseAuthError(
                f"Supabase Auth returned invalid authorization details (request_id={uuid4().hex})"
            )
        client = data.get("client") if isinstance(data.get("client"), dict) else {}
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        scope = str(data.get("scope") or "")
        return AuthorizationDetails(
            authorization_id=str(data.get("authorization_id") or "") or None,
            redirect_uri=str(data.get("redirect_uri") or "") or None,
            client_id=str(client.get("id") or "") or None,
            client_name=str(client.get("name") or "") or None,
            client_uri=str(client.get("uri") or "") or None,
            user_subject=str(user.get("id") or "") or None,
            user_email=str(user.get("email") or "") or None,
            scopes=tuple(item for item in scope.split() if item),
            redirect_url=str(data.get("redirect_url") or "") or None,
        )

    async def submit_consent(
        self,
        authorization_id: str,
        action: ConsentAction,
        access_token: str,
    ) -> str:
        if action not in ("approve", "deny"):
            raise ValueError("action must be approve or deny")
        response = await self._request(
            "POST",
            f"oauth/authorizations/{quote(authorization_id, safe='')}/consent",
            access_token=access_token,
            json={"action": action},
        )
        data = self._json(response)
        redirect_url = str(data.get("redirect_url") or "") if isinstance(data, dict) else ""
        parsed = urlsplit(redirect_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username:
            raise SupabaseAuthError(
                f"Supabase Auth returned an invalid redirect (request_id={uuid4().hex})"
            )
        return redirect_url

    async def list_grants(self, access_token: str) -> list[OAuthGrant]:
        response = await self._request(
            "GET",
            "user/oauth/grants",
            access_token=access_token,
        )
        data = self._json(response)
        if not isinstance(data, list):
            raise SupabaseAuthError(
                f"Supabase Auth returned invalid grants (request_id={uuid4().hex})"
            )
        grants: list[OAuthGrant] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            client = item.get("client") if isinstance(item.get("client"), dict) else {}
            client_id = str(client.get("id") or "").strip()
            scopes = item.get("scopes") if isinstance(item.get("scopes"), list) else []
            if not client_id:
                continue
            grants.append(
                OAuthGrant(
                    client_id=client_id,
                    client_name=str(client.get("name") or "") or None,
                    client_uri=str(client.get("uri") or "") or None,
                    scopes=tuple(str(scope) for scope in scopes if str(scope)),
                    granted_at=str(item.get("granted_at") or "") or None,
                )
            )
        return grants

    async def revoke_grant(self, client_id: str, access_token: str) -> None:
        await self._request(
            "DELETE",
            "user/oauth/grants",
            access_token=access_token,
            params={"client_id": client_id},
            expected_statuses=(204,),
        )

    async def get_oauth_subject(self, access_token: str) -> str:
        """Validate the current provider session, including resource-audience tokens."""
        try:
            response = await self._request("GET", "oauth/userinfo", access_token=access_token)
            data = self._json(response)
        except SupabaseAuthError as exc:
            if exc.status_code in (401, 403):
                raise
            raise SupabaseAuthUnavailable("Supabase UserInfo is unavailable") from None
        subject = data.get("sub") if isinstance(data, dict) else None
        if not isinstance(subject, str) or not subject.strip():
            raise SupabaseAuthUnavailable(
                f"Supabase Auth returned invalid UserInfo (request_id={uuid4().hex})"
            )
        return subject
