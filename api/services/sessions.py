from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.requests import Request
from starlette.responses import Response

from api.configs.settings import friend_mode_enabled, public_base_url

COOKIE_NAME = "nyc_ai_events_session"
SUPABASE_ACCESS_COOKIE_NAME = "nyc_ai_events_sb_access"
SUPABASE_REFRESH_COOKIE_NAME = "nyc_ai_events_sb_refresh"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
SUPABASE_REFRESH_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
OAUTH_FLOW_MAX_AGE_SECONDS = 10 * 60


def make_serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="nyc-ai-events-session-v1")


def new_session(user_id: int) -> dict[str, Any]:
    return {
        "user_id": int(user_id),
        "csrf_token": secrets.token_urlsafe(32),
    }


def load_session(
    request: Request,
    serializer: URLSafeTimedSerializer,
) -> dict[str, Any] | None:
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    try:
        data = serializer.loads(cookie, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("user_id"), int):
        return None
    if not isinstance(data.get("csrf_token"), str):
        return None
    return data


def secure_cookie_for_request(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return (
        friend_mode_enabled()
        or public_base_url().startswith("https://")
        or request.url.scheme == "https"
        or forwarded_proto.split(",", 1)[0] == "https"
    )


def set_session_cookie(
    response: Response,
    request: Request,
    serializer: URLSafeTimedSerializer,
    session: dict[str, Any],
) -> None:
    response.set_cookie(
        COOKIE_NAME,
        serializer.dumps(session),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=secure_cookie_for_request(request),
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        httponly=True,
        samesite="lax",
    )


def oauth_flow_cookie_name(state: str) -> str:
    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()[:24]
    return f"nyc_ai_events_flow_{digest}"


def set_oauth_flow_cookie(
    response: Response,
    request: Request,
    *,
    state: str,
    value: str,
) -> None:
    response.set_cookie(
        oauth_flow_cookie_name(state),
        value,
        max_age=OAUTH_FLOW_MAX_AGE_SECONDS,
        httponly=True,
        secure=secure_cookie_for_request(request),
        samesite="lax",
        path="/auth/supabase/callback",
    )


def clear_oauth_flow_cookie(response: Response, *, state: str) -> None:
    response.delete_cookie(
        oauth_flow_cookie_name(state),
        httponly=True,
        samesite="lax",
        path="/auth/supabase/callback",
    )


def set_supabase_session_cookies(
    response: Response,
    request: Request,
    session: Any,
) -> None:
    expires_at = getattr(session, "expires_at", None)
    access_max_age = 60 * 60
    if isinstance(expires_at, datetime):
        normalized = (
            expires_at.astimezone(UTC) if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        )
        access_max_age = max(1, int((normalized - datetime.now(UTC)).total_seconds()))
    common = {
        "httponly": True,
        "secure": secure_cookie_for_request(request),
        "samesite": "lax",
        "path": "/",
    }
    response.set_cookie(
        SUPABASE_ACCESS_COOKIE_NAME,
        session.access_token,
        max_age=access_max_age,
        **common,
    )
    response.set_cookie(
        SUPABASE_REFRESH_COOKIE_NAME,
        session.refresh_token,
        max_age=SUPABASE_REFRESH_MAX_AGE_SECONDS,
        **common,
    )


def clear_supabase_session_cookies(response: Response) -> None:
    for name in (SUPABASE_ACCESS_COOKIE_NAME, SUPABASE_REFRESH_COOKIE_NAME):
        response.delete_cookie(
            name,
            httponly=True,
            samesite="lax",
            path="/",
        )
