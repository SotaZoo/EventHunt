from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request
from starlette.responses import Response

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_OAUTH_SCOPE = "openid email profile"


class GoogleOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class GoogleUserInfo:
    subject: str
    email: str
    email_verified: bool
    display_name: str | None = None


class GoogleOAuthClient:
    def __init__(self, *, client_id: str, client_secret: str) -> None:
        self.oauth = OAuth()
        self.oauth.register(
            "google",
            client_id=client_id,
            client_secret=client_secret,
            server_metadata_url=GOOGLE_DISCOVERY_URL,
            client_kwargs={"scope": GOOGLE_OAUTH_SCOPE},
        )

    async def authorize_redirect(self, request: Request, redirect_uri: str) -> Response:
        google = self.oauth.create_client("google")
        if google is None:
            raise GoogleOAuthError("Google OAuth client is not configured")
        return await google.authorize_redirect(request, redirect_uri)

    async def authorize_userinfo(self, request: Request) -> GoogleUserInfo:
        google = self.oauth.create_client("google")
        if google is None:
            raise GoogleOAuthError("Google OAuth client is not configured")
        try:
            token = await google.authorize_access_token(request)
        except OAuthError as exc:
            raise GoogleOAuthError("Google OAuth callback could not be verified") from exc
        except Exception as exc:
            raise GoogleOAuthError("Google OAuth callback failed") from exc

        userinfo = token.get("userinfo")
        if not isinstance(userinfo, dict):
            raise GoogleOAuthError("Google OAuth response did not include user info")
        return _parse_userinfo(userinfo)


def _parse_userinfo(userinfo: dict[str, Any]) -> GoogleUserInfo:
    subject = str(userinfo.get("sub") or "").strip()
    email = str(userinfo.get("email") or "").strip()
    email_verified = bool(userinfo.get("email_verified"))
    name = str(userinfo.get("name") or "").strip() or None
    if not subject or not email:
        raise GoogleOAuthError("Google OAuth response was missing identity fields")
    return GoogleUserInfo(
        subject=subject,
        email=email,
        email_verified=email_verified,
        display_name=name,
    )


__all__ = [
    "GOOGLE_DISCOVERY_URL",
    "GOOGLE_OAUTH_SCOPE",
    "GoogleOAuthClient",
    "GoogleOAuthError",
    "GoogleUserInfo",
]
