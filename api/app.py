from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError
from starlette.applications import Starlette
from starlette.datastructures import FormData
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from api.configs.settings import (
    agent_api_token as configured_agent_api_token,
)
from api.configs.settings import (
    agent_api_token_config_error,
    feedback_email,
    friend_mode_enabled,
    google_oauth_client_id,
    google_oauth_client_secret,
    google_oauth_config_errors,
    google_oauth_configured,
    mcp_auth_mode,
    mcp_resource_url_config_error,
    open_google_signup,
    public_base_url,
    public_base_url_config_error,
    session_secret_config_error,
    supabase_config_errors,
    supabase_configured,
    supabase_issuer_url,
    supabase_jwks_url,
    supabase_publishable_key,
    supabase_url,
)
from api.configs.settings import (
    mcp_resource_url as configured_mcp_resource_url,
)
from api.controllers.api_events import list_events_api
from api.controllers.health import health_response
from api.integrations.google.oauth import GoogleOAuthClient, GoogleOAuthError
from api.integrations.supabase.auth import SupabaseAuthError, SupabaseAuthGateway
from api.mcp.availability import AuthenticationAvailabilityMiddleware
from api.mcp.server import (
    SUPABASE_READ_SCOPE,
    SupabaseMCPTokenVerifier,
    create_event_scout_mcp,
    transport_security_for,
)
from api.repositories import _storage as storage
from api.repositories.auth_transactions import (
    bind_auth_transaction_subject,
    consume_auth_transaction,
    create_auth_transaction,
)
from api.services.discovery import catalog as discovery_catalog
from api.services.sessions import (
    SUPABASE_ACCESS_COOKIE_NAME,
    SUPABASE_REFRESH_COOKIE_NAME,
    clear_oauth_flow_cookie,
    clear_session_cookie,
    clear_supabase_session_cookies,
    load_session,
    make_serializer,
    new_session,
    oauth_flow_cookie_name,
    set_oauth_flow_cookie,
    set_session_cookie,
    set_supabase_session_cookies,
)
from api.tasks.invitation_delivery import POLL_SECONDS, invitation_delivery

LOGGER = logging.getLogger(__name__)
NY_TZ = ZoneInfo("America/New_York")
SOURCE_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
SAFE_RETURN_PATHS = {"/events", "/calendar", "/mine", "/hidden"}
OAUTH_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,255}$")
OAUTH_START_MAX_REQUESTS = 10
OAUTH_START_WINDOW_SECONDS = 300
MINE_STATUSES = {"saved", "interested", "registering", "applied", "pending_approval", "confirmed"}
HIDDEN_STATUSES = {"dismissed", "rejected"}
STATUS_MENU = (
    ("saved", "Saved"),
    ("registering", "Registering"),
    ("applied", "Applied"),
    ("pending_approval", "Pending"),
    ("confirmed", "Approved"),
    ("dismissed", "Hidden"),
    ("rejected", "Rejected"),
)
STATUS_LABELS = {
    "new": "New",
    "saved": "Saved",
    "interested": "Saved",
    "registering": "Registering",
    "applied": "Applied",
    "pending_approval": "Pending",
    "confirmed": "Approved",
    "dismissed": "Hidden",
    "rejected": "Rejected",
}
LUMA_HOSTS = {"lu.ma", "luma.com", "www.lu.ma", "www.luma.com"}


class LoginRateLimiter:
    def __init__(self, *, max_failures: int = 5, window_seconds: int = 300) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.failures: defaultdict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> None:
        failures = self.failures[key]
        cutoff = now - self.window_seconds
        while failures and failures[0] < cutoff:
            failures.popleft()

    def is_limited(self, key: str) -> bool:
        now = time.monotonic()
        self._prune(key, now)
        return len(self.failures[key]) >= self.max_failures

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        self._prune(key, now)
        self.failures[key].append(now)

    def reset(self, key: str) -> None:
        self.failures.pop(key, None)


class RequestRateLimiter(LoginRateLimiter):
    def __init__(self, *, max_requests: int, window_seconds: int) -> None:
        super().__init__(max_failures=max_requests, window_seconds=window_seconds)

    def record_request(self, key: str) -> None:
        self.record_failure(key)


@dataclass(frozen=True)
class EventFilters:
    status: str | None
    min_score: float
    source: str | None


def _template_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def _web_asset_dir() -> Path:
    packaged = Path(__file__).parent / "_runtime" / "web"
    return packaged if packaged.exists() else Path(__file__).parent.parent / "web" / "dist" / "discovery"


def _templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(_template_dir()))
    templates.env.filters["status_label"] = _status_label
    asset_urls = {}
    for name in ("main.css", "main.js", "account.css", "account.js"):
        path = _web_asset_dir() / name
        url = f"/assets/discovery/{name}"
        if path.is_file():
            url += f"?v={hashlib.sha256(path.read_bytes()).hexdigest()[:16]}"
        asset_urls[name] = url
    templates.env.globals["asset_urls"] = asset_urls
    return templates


def _status_label(status: str | None) -> str:
    return STATUS_LABELS.get(status or "new", (status or "new").replace("_", " ").title())


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(NY_TZ)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(NY_TZ)


def _format_datetime(value: str | None) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return "Time TBD"
    minute = f":{parsed.minute:02d}" if parsed.minute else ""
    return f"{parsed.strftime('%a')}, {parsed.strftime('%b')} {parsed.day}, {parsed.strftime('%-I')}{minute} {parsed.strftime('%p')}"


def _format_event_time(value: str | None) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return "TBD"
    minute = f":{parsed.minute:02d}" if parsed.minute else ""
    return f"{parsed.strftime('%-I')}{minute} {parsed.strftime('%p')}"


def _format_day(value: str | None) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return "Date TBD"
    return f"{parsed.strftime('%A')}, {parsed.strftime('%b')} {parsed.day}"


def _format_today_header(now: datetime | None = None) -> str:
    local_now = now.astimezone(NY_TZ) if now else datetime.now(NY_TZ)
    return f"Today is {local_now.strftime('%A')}, {local_now.strftime('%B')} {local_now.day}, {local_now.year}"


def _local_day_start_utc(now: datetime | None = None) -> datetime:
    local_now = now.astimezone(NY_TZ) if now else datetime.now(NY_TZ)
    return datetime.combine(local_now.date(), datetime.min.time(), tzinfo=NY_TZ).astimezone(UTC)


def _timeline_bucket(value: str | None, *, today: date | None = None) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return "Later"
    current = today or datetime.now(NY_TZ).date()
    event_day = parsed.date()
    if event_day == current:
        return "Today"
    if event_day == current + timedelta(days=1):
        return "Tomorrow"
    if event_day < current + timedelta(days=7):
        return "This Week"
    return "Later"


def _safe_event_url(value: str | None) -> str:
    if not value:
        return "#"
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return "#"
    return value


def _decorate_event(row: Any) -> dict[str, Any]:
    event = row.as_dict() if hasattr(row, "as_dict") else dict(row)
    event["score_label"] = f"{round(float(event['fit_score']) * 100):.0f}%"
    event["time_label"] = _format_datetime(event.get("start_at"))
    event["event_time_label"] = _format_event_time(event.get("start_at"))
    event["day_label"] = _format_day(event.get("start_at"))
    parsed = _parse_datetime(event.get("start_at"))
    event["weekday_short"] = parsed.strftime("%a") if parsed else "TBD"
    event["day_number"] = str(parsed.day) if parsed else "--"
    event["timeline_bucket"] = _timeline_bucket(event.get("start_at"))
    event["status_label"] = _status_label(event.get("user_status"))
    event["venue_label"] = event.get("venue") or "Venue TBD"
    event["safe_url"] = _safe_event_url(event.get("url"))
    action_kind = _primary_action_kind(event)
    event["primary_action_kind"] = action_kind
    event["primary_action_label"] = "Register" if action_kind == "register" else "Open"
    return event


def _primary_action_kind(event: Mapping[str, Any]) -> str:
    if event.get("safe_url") == "#":
        return "view"
    if str(event.get("source") or "").lower() == "luma" and _is_luma_url(
        str(event.get("safe_url") or "")
    ):
        return "register"
    return "open"


def _is_luma_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in {"http", "https"} and parts.hostname in LUMA_HOSTS


def _group_by_timeline(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    order = ("Today", "Tomorrow", "This Week", "Later")
    buckets = {name: {"day": name, "events": []} for name in order}
    for event in events:
        buckets.get(event["timeline_bucket"], buckets["Later"])["events"].append(event)
    return [buckets[name] for name in order if buckets[name]["events"]]


def _group_by_day(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for event in events:
        day = event["day_label"]
        if day not in seen:
            seen[day] = {"day": day, "events": []}
            groups.append(seen[day])
        seen[day]["events"].append(event)
    return groups


def _login_key(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _safe_return_to(value: str | None, *, default: str = "/events") -> str:
    if not value:
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path not in SAFE_RETURN_PATHS:
        return default
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.path}{query}"


def _with_query_params(value: str, **params: object) -> str:
    parts = urlsplit(value)
    existing = dict(
        item.split("=", 1) if "=" in item else (item, "")
        for item in parts.query.split("&")
        if item
    )
    existing.update({k: str(v) for k, v in params.items() if v is not None})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(existing), parts.fragment))


def _parse_filters(request: Request) -> EventFilters:
    raw_status = request.query_params.get("status")
    status = raw_status if raw_status in (*storage.USER_EVENT_STATUSES, "all") else None
    raw_score = request.query_params.get("min_score", "0.7")
    try:
        min_score = float(raw_score)
    except ValueError:
        min_score = 0.7
    min_score = max(0.0, min(1.0, min_score))
    source = request.query_params.get("source")
    if source and not SOURCE_RE.fullmatch(source):
        source = None
    return EventFilters(status=status, min_score=min_score, source=source or None)


def _csrf_from_session(request: Request) -> str:
    session = getattr(request.state, "session", None)
    if isinstance(session, dict):
        token = session.get("csrf_token")
        if isinstance(token, str):
            return token
    return ""


def _base_context(
    request: Request,
    user: Any | None = None,
    *,
    filters: EventFilters | None = None,
) -> dict[str, Any]:
    current_return_to = request.url.path
    if request.url.query:
        current_return_to = f"{current_return_to}?{request.url.query}"
    undo_event = request.query_params.get("undo_event")
    undo_status = request.query_params.get("undo_status")
    undo = None
    if (
        user is not None
        and undo_event
        and undo_event.isdigit()
        and undo_status in storage.USER_EVENT_STATUSES
    ):
        undo = {"event_id": int(undo_event), "status": undo_status}
    is_authenticated = user is not None
    return {
        "request": request,
        "user": user,
        "is_authenticated": is_authenticated,
        "show_app_header": True,
        "can_personalize": is_authenticated,
        "csrf_token": _csrf_from_session(request),
        "current_return_to": current_return_to,
        "filters": filters,
        "statuses": STATUS_MENU,
        "undo": undo,
        "open_google_signup": open_google_signup() and request.app.state.supabase_auth_gateway is not None,
    }


def _connect(request: Request) -> Any:
    if request.app.state.database is not None:
        return request.app.state.database.open_compat()
    return storage.connect(request.app.state.db_path)


def _redirect_login(clear: bool = False) -> Response:
    response = RedirectResponse("/login", status_code=303)
    if clear:
        clear_session_cookie(response)
    return response


def _load_user(request: Request, conn: Any) -> Any | Response:
    session = load_session(request, request.app.state.session_serializer)
    if session is None:
        return _redirect_login()
    user = storage.get_active_user(conn, session["user_id"])
    if user is None:
        return _redirect_login(clear=True)
    request.state.session = session
    return user


def _require_csrf(request: Request, form: FormData) -> bool:
    session = getattr(request.state, "session", None)
    if not isinstance(session, dict):
        return False
    expected = session.get("csrf_token")
    actual = form.get("_csrf_token")
    return isinstance(expected, str) and isinstance(actual, str) and secrets.compare_digest(
        expected, actual
    )


def _event_exists(conn: Any, event_id: int) -> bool:
    return storage.event_exists(conn, event_id)


def _event_counts(conn: Any) -> tuple[int, int]:
    return storage.event_counts(conn)


def _sources(conn: Any) -> list[str]:
    return storage.list_sources(conn)


def _empty_state(conn: sqlite3.Connection) -> str:
    total, scored = _event_counts(conn)
    if total == 0:
        return "No scraped events yet"
    if scored == 0:
        return "No scored events yet"
    return "No events match these filters"


def _previous_user_status(
    conn: Any,
    user_id: int,
    event_id: int,
) -> str:
    return storage.get_previous_user_status(conn, user_id, event_id)


def _event_action_details(
    conn: Any,
    event_id: int,
) -> tuple[str, str] | None:
    row = storage.get_event_source_url(conn, event_id)
    if row is None:
        return None
    safe_url = _safe_event_url(row["url"])
    action_kind = _primary_action_kind({"source": row["source"], "safe_url": safe_url})
    return action_kind, safe_url


def _login_context(
    request: Request, error: str | None, authorization_id: str | None = None,
) -> dict[str, Any]:
    return {
        "request": request,
        "error": error,
        "google_oauth_enabled": request.app.state.google_oauth_client is not None,
        "supabase_oauth_enabled": request.app.state.supabase_auth_gateway is not None,
        "invite_login_enabled": request.app.state.invite_login_enabled,
        "open_google_signup": open_google_signup() and request.app.state.supabase_auth_gateway is not None,
        "supabase_start_url": _supabase_start_location(
            authorization_id or _valid_oauth_id(request.query_params.get("authorization_id")),
        ),
    }


def _login_response(
    request: Request,
    *,
    error: str | None,
    status_code: int = 200,
    authorization_id: str | None = None,
) -> Response:
    return request.app.state.templates.TemplateResponse(
        request,
        "login.html",
        _login_context(request, error, authorization_id),
        status_code=status_code,
    )


def _google_redirect_uri(request: Request) -> str:
    base_url = public_base_url()
    if base_url:
        return f"{base_url}/auth/google/callback"
    return str(request.url_for("google_callback"))


def _supabase_callback_uri(request: Request, state: str) -> str:
    base_url = public_base_url()
    callback = (
        f"{base_url}/auth/supabase/callback"
        if base_url
        else str(request.url_for("supabase_callback"))
    )
    return f"{callback}?{urlencode({'state': state})}"


def _supabase_start_location(authorization_id: str | None = None) -> str:
    if authorization_id:
        return f"/auth/supabase/start?{urlencode({'authorization_id': authorization_id})}"
    return "/auth/supabase/start"


def _valid_oauth_id(value: str | None) -> str | None:
    if value and OAUTH_ID_RE.fullmatch(value):
        return value
    return None


async def _supabase_access_for_user(
    request: Request,
    user: Any,
) -> tuple[str, Any | None, Any | None] | None:
    """Return validated access, identity, and an optional rotated session."""
    gateway = request.app.state.supabase_auth_gateway
    if gateway is None:
        return None
    access_token = request.cookies.get(SUPABASE_ACCESS_COOKIE_NAME)
    rotated_session = None
    try:
        if access_token:
            try:
                identity = await gateway.get_identity(access_token)
            except SupabaseAuthError:
                access_token = None
        if not access_token:
            refresh_token = request.cookies.get(SUPABASE_REFRESH_COOKIE_NAME)
            if not refresh_token:
                return None
            rotated_session = await gateway.refresh_session(refresh_token)
            access_token = rotated_session.access_token
            identity = rotated_session.identity
    except SupabaseAuthError:
        return None
    conn = _connect(request)
    try:
        linked = storage.get_user_by_identity(
            conn,
            provider="supabase",
            subject=identity.subject,
        )
    finally:
        conn.close()
    if linked is None or linked["id"] != user["id"]:
        return None
    return access_token, identity, rotated_session


def _with_rotated_supabase_session(
    response: Response,
    request: Request,
    rotated_session: Any | None,
) -> Response:
    if rotated_session is not None:
        set_supabase_session_cookies(response, request, rotated_session)
    return response


async def home(request: Request) -> Response:
    return RedirectResponse("/events", status_code=303)


async def favicon(request: Request) -> Response:
    return Response(status_code=204)


async def login_form(request: Request) -> Response:
    return _login_response(request, error=None)


async def login(request: Request) -> Response:
    if not request.app.state.invite_login_enabled:
        return _login_response(
            request,
            error="Invite-code login is disabled for the shared friends alpha",
            status_code=403,
        )
    form = await request.form()
    key = _login_key(request)
    limiter: LoginRateLimiter = request.app.state.login_rate_limiter
    if limiter.is_limited(key):
        return _login_response(
            request,
            error="Too many attempts. Wait a few minutes and try again.",
            status_code=429,
        )
    invite_code = str(form.get("invite_code") or "")
    if not invite_code.strip():
        limiter.record_failure(key)
        return _login_response(
            request,
            error="Invite code not recognized",
            status_code=401,
        )

    conn = _connect(request)
    try:
        user = storage.get_user_by_invite_code(conn, invite_code)
    finally:
        conn.close()
    if user is None:
        limiter.record_failure(key)
        return _login_response(
            request,
            error="Invite code not recognized",
            status_code=401,
        )

    limiter.reset(key)
    response = RedirectResponse("/events", status_code=303)
    set_session_cookie(
        response,
        request,
        request.app.state.session_serializer,
        new_session(user["id"]),
    )
    return response


async def google_start(request: Request) -> Response:
    client = request.app.state.google_oauth_client
    if client is None:
        return _login_response(
            request,
            error="Google sign-in is not configured yet",
            status_code=503,
        )
    try:
        return await client.authorize_redirect(request, _google_redirect_uri(request))
    except GoogleOAuthError:
        LOGGER.exception("google oauth start failed")
        return _login_response(
            request,
            error="Google sign-in could not start",
            status_code=503,
        )


async def google_callback(request: Request) -> Response:
    client = request.app.state.google_oauth_client
    if client is None:
        return _login_response(
            request,
            error="Google sign-in is not configured yet",
            status_code=503,
        )
    if request.query_params.get("error"):
        request.session.clear()
        return _login_response(
            request,
            error="Google sign-in was cancelled",
            status_code=401,
        )
    if not request.query_params.get("code"):
        request.session.clear()
        return _login_response(
            request,
            error="Google sign-in response was incomplete",
            status_code=400,
        )

    try:
        userinfo = await client.authorize_userinfo(request)
    except GoogleOAuthError:
        LOGGER.warning("google oauth callback rejected", exc_info=True)
        request.session.clear()
        return _login_response(
            request,
            error="Google sign-in could not be verified",
            status_code=401,
        )
    if not userinfo.email_verified:
        request.session.clear()
        return _login_response(
            request,
            error="Google account email must be verified",
            status_code=403,
        )

    conn = _connect(request)
    try:
        user = storage.create_or_accept_google_user(
            conn,
            subject=userinfo.subject,
            email=userinfo.email,
            email_verified=userinfo.email_verified,
            display_name=userinfo.display_name,
        )
    finally:
        conn.close()
    if user is None:
        request.session.clear()
        return _login_response(
            request,
            error="This Google account cannot sign in right now",
            status_code=403,
        )

    request.session.clear()
    response = RedirectResponse("/events", status_code=303)
    set_session_cookie(
        response,
        request,
        request.app.state.session_serializer,
        new_session(user["id"]),
    )
    return response


async def supabase_start(request: Request) -> Response:
    gateway = request.app.state.supabase_auth_gateway
    if gateway is None:
        return _login_response(
            request,
            error="Google sign-in is not configured yet",
            status_code=503,
        )
    raw_authorization_id = request.query_params.get("authorization_id")
    authorization_id = _valid_oauth_id(raw_authorization_id)
    if raw_authorization_id and authorization_id is None:
        return PlainTextResponse("Invalid authorization request", status_code=400)

    key = _login_key(request)
    limiter: RequestRateLimiter = request.app.state.oauth_start_rate_limiter
    if limiter.is_limited(key):
        return PlainTextResponse(
            "Too many sign-in attempts. Wait a few minutes and try again.",
            status_code=429,
        )
    limiter.record_request(key)

    state = secrets.token_urlsafe(32)
    flow_cookie = secrets.token_urlsafe(32)
    redirect_uri = _supabase_callback_uri(request, state)
    try:
        started = await gateway.start_google_sign_in(redirect_uri)
    except SupabaseAuthError as exc:
        LOGGER.warning("supabase sign-in start failed (%s)", exc)
        return _login_response(
            request,
            error="Google sign-in could not start",
            status_code=503,
        )

    conn = _connect(request)
    try:
        create_auth_transaction(
            conn,
            state=state,
            flow_cookie=flow_cookie,
            pkce_verifier=started.code_verifier,
            purpose="consent" if authorization_id else "login",
            authorization_id=authorization_id,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    finally:
        conn.close()

    response = RedirectResponse(started.authorization_url, status_code=303)
    set_oauth_flow_cookie(
        response,
        request,
        state=state,
        value=flow_cookie,
    )
    return response


async def supabase_callback(request: Request) -> Response:
    gateway = request.app.state.supabase_auth_gateway
    if gateway is None:
        return _login_response(
            request,
            error="Google sign-in is not configured yet",
            status_code=503,
        )
    state = request.query_params.get("state") or ""
    if not _valid_oauth_id(state):
        return _login_response(
            request,
            error="Google sign-in could not be verified",
            status_code=401,
        )
    flow_cookie = request.cookies.get(oauth_flow_cookie_name(state)) or ""
    code = request.query_params.get("code") or ""
    if not flow_cookie:
        return _login_response(
            request, error="Google sign-in could not be verified", status_code=401,
        )
    conn = _connect(request)
    try:
        transaction = consume_auth_transaction(
            conn,
            state=state,
            flow_cookie=flow_cookie,
        )
    finally:
        conn.close()
    if transaction is None:
        response = _login_response(
            request,
            error="Google sign-in could not be verified",
            status_code=401,
        )
        clear_oauth_flow_cookie(response, state=state)
        return response

    authorization_id = transaction["authorization_id"]
    if request.query_params.get("error") or not code:
        response = _login_response(
            request, error="Google sign-in was cancelled or incomplete", status_code=401,
            authorization_id=authorization_id,
        )
        clear_oauth_flow_cookie(response, state=state)
        return response

    redirect_uri = _supabase_callback_uri(request, state)
    try:
        session = await gateway.exchange_code(
            auth_code=code,
            code_verifier=transaction["pkce_verifier"],
            redirect_uri=redirect_uri,
        )
    except SupabaseAuthError as exc:
        LOGGER.warning("supabase callback rejected (%s)", exc)
        response = _login_response(
            request,
            error="Google sign-in could not be verified",
            status_code=401,
            authorization_id=authorization_id,
        )
        clear_oauth_flow_cookie(response, state=state)
        return response

    conn = _connect(request)
    try:
        user = storage.create_or_accept_supabase_user(
            conn,
            subject=session.identity.subject,
            email=session.identity.email,
            email_verified=session.identity.email_verified,
            display_name=session.identity.display_name,
            allow_signup=open_google_signup() and session.identity.has_google_identity,
        )
        if user is not None:
            bind_auth_transaction_subject(
                conn,
                state=state,
                subject=session.identity.subject,
            )
    finally:
        conn.close()
    if user is None:
        try:
            await gateway.logout(session.access_token)
        except SupabaseAuthError as exc:
            LOGGER.warning("supabase rejected-user logout failed (%s)", exc)
        if not authorization_id and session.identity.email_verified:
            from api.controllers.public_entry import preview_page
            response = preview_page(request, email=session.identity.email, denied=True)
        else:
            response = _login_response(
                request,
                error="This Google account cannot sign in right now",
                status_code=403,
                authorization_id=authorization_id,
            )
        clear_oauth_flow_cookie(response, state=state)
        return response

    authorization_id = transaction["authorization_id"]
    target = (
        f"/oauth/consent?{urlencode({'authorization_id': authorization_id})}"
        if authorization_id
        else "/events"
    )
    response = RedirectResponse(target, status_code=303)
    clear_oauth_flow_cookie(response, state=state)
    set_session_cookie(
        response,
        request,
        request.app.state.session_serializer,
        new_session(user["id"]),
    )
    set_supabase_session_cookies(response, request, session)
    return response


async def oauth_consent(request: Request) -> Response:
    from api.controllers.account import authorization_unavailable
    from api.services.account import oauth_permissions

    authorization_id = _valid_oauth_id(request.query_params.get("authorization_id"))
    if authorization_id is None:
        return authorization_unavailable(request)
    conn = _connect(request)
    try:
        user = _load_user(request, conn)
    finally:
        conn.close()
    if isinstance(user, Response):
        return RedirectResponse(
            _supabase_start_location(authorization_id),
            status_code=303,
        )
    access = await _supabase_access_for_user(request, user)
    if access is None:
        response = RedirectResponse(
            _supabase_start_location(authorization_id),
            status_code=303,
        )
        clear_supabase_session_cookies(response)
        return response
    access_token, identity, rotated_session = access
    try:
        details = await request.app.state.supabase_auth_gateway.get_authorization_details(
            authorization_id,
            access_token,
        )
    except SupabaseAuthError:
        return _with_rotated_supabase_session(
            authorization_unavailable(request),
            request,
            rotated_session,
        )
    if details.user_subject and details.user_subject != identity.subject:
        return _with_rotated_supabase_session(
            authorization_unavailable(request, status_code=404),
            request,
            rotated_session,
        )
    if details.redirect_url:
        response = RedirectResponse(details.redirect_url, status_code=303)
    else:
        context = {
            **_base_context(request, user),
            "authorization": details,
            "permissions": oauth_permissions(details.scopes),
        }
        response = request.app.state.templates.TemplateResponse(
            request,
            "oauth_consent.html",
            context,
        )
    return _with_rotated_supabase_session(response, request, rotated_session)


async def oauth_consent_decision(request: Request) -> Response:
    from api.controllers.account import authorization_unavailable

    form = await request.form()
    authorization_id = _valid_oauth_id(str(form.get("authorization_id") or ""))
    decision = str(form.get("decision") or "")
    if authorization_id is None or decision not in {"approve", "deny"}:
        return PlainTextResponse("Invalid authorization decision", status_code=400)
    conn = _connect(request)
    try:
        user = _load_user(request, conn)
    finally:
        conn.close()
    if isinstance(user, Response):
        return RedirectResponse(_supabase_start_location(authorization_id), status_code=303)
    if not _require_csrf(request, form):
        return PlainTextResponse("Invalid CSRF token", status_code=403)
    access = await _supabase_access_for_user(request, user)
    if access is None:
        response = RedirectResponse(_supabase_start_location(authorization_id), status_code=303)
        clear_session_cookie(response)
        clear_supabase_session_cookies(response)
        return response
    access_token, identity, rotated_session = access
    try:
        details = await request.app.state.supabase_auth_gateway.get_authorization_details(
            authorization_id,
            access_token,
        )
        if details.user_subject and details.user_subject != identity.subject:
            return _with_rotated_supabase_session(
                authorization_unavailable(request, status_code=404),
                request,
                rotated_session,
            )
        target = await request.app.state.supabase_auth_gateway.submit_consent(
            authorization_id,
            decision,
            access_token,
        )
    except SupabaseAuthError:
        return _with_rotated_supabase_session(
            authorization_unavailable(request),
            request,
            rotated_session,
        )
    response = RedirectResponse(target, status_code=303)
    return _with_rotated_supabase_session(response, request, rotated_session)


async def logout(request: Request) -> Response:
    form = await request.form()
    conn = _connect(request)
    access_token = None
    try:
        user = _load_user(request, conn)
        if isinstance(user, Response):
            return user
        if not _require_csrf(request, form):
            return PlainTextResponse("Invalid CSRF token", status_code=403)
    finally:
        conn.close()
    if request.app.state.supabase_auth_gateway is not None:
        access = await _supabase_access_for_user(request, user)
        if access is not None:
            access_token = access[0]
    gateway = request.app.state.supabase_auth_gateway
    if gateway is not None and access_token:
        try:
            await gateway.logout(access_token)
        except SupabaseAuthError as exc:
            LOGGER.warning("supabase logout failed (%s)", exc)
    request.session.clear()
    response = RedirectResponse("/events", status_code=303)
    clear_session_cookie(response)
    clear_supabase_session_cookies(response)
    return response


def _fetch_events(
    conn: Any,
    user_id: int,
    filters: EventFilters,
) -> list[dict[str, Any]]:
    rows = storage.fetch_events_for_user(
        conn,
        user_id,
        status=filters.status,
        min_score=filters.min_score,
        source=filters.source,
        include_past=False,
        start_at_from=_local_day_start_utc(),
    )
    return [_decorate_event(row) for row in rows]


def _filter_personal(
    events: Sequence[dict[str, Any]],
    statuses: set[str],
) -> list[dict[str, Any]]:
    return [event for event in events if event.get("user_status") in statuses]


async def events(request: Request) -> Response:
    from api.controllers.public_entry import preview_page

    conn = None
    try:
        conn = _connect(request)
        user = _load_user(request, conn)
        if isinstance(user, Response):
            return preview_page(request)
        return request.app.state.templates.TemplateResponse(
            request, "discovery.html", _base_context(request, user),
            headers={"Cache-Control": "private, no-store"},
        )
    except (sqlite3.OperationalError, SQLAlchemyError):
        # Access controls remain usable while the catalog is unavailable.
        return preview_page(request, clear_credentials=False)
    finally:
        if conn is not None:
            conn.close()


async def discovery(request: Request) -> Response:
    """Serve the private discovery catalog only to an active invited account."""
    conn = _connect(request)
    try:
        user = _load_user(request, conn)
        if isinstance(user, Response):
            return user
        return JSONResponse(discovery_catalog(conn), headers={"Cache-Control": "private, no-store"})
    except (sqlite3.OperationalError, SQLAlchemyError):
        LOGGER.exception("discovery query failed")
        return JSONResponse({"error": "Events are temporarily unavailable"}, status_code=503)
    finally:
        conn.close()


async def calendar(request: Request) -> Response:
    target = "/events"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(target, status_code=303)


async def mine(request: Request) -> Response:
    """Retire the personal timeline without changing historical event records."""
    conn = _connect(request)
    try:
        user = _load_user(request, conn)
        if isinstance(user, Response):
            return user
        return RedirectResponse(
            "/events", status_code=303, headers={"Cache-Control": "private, no-store"},
        )
    except (sqlite3.OperationalError, SQLAlchemyError):
        LOGGER.exception("web mine query failed")
        return PlainTextResponse("Database temporarily unavailable", status_code=503)
    finally:
        conn.close()


async def hidden(request: Request) -> Response:
    """Redirect old Hidden bookmarks through the same authenticated retirement path."""
    return await mine(request)


async def settings(request: Request) -> Response:
    from api.services.account import account_initials, feedback_link

    conn = _connect(request)
    try:
        user = _load_user(request, conn)
        if isinstance(user, Response):
            return user
    finally:
        conn.close()
    rotated_session = None
    if request.app.state.supabase_auth_gateway is not None:
        access = await _supabase_access_for_user(request, user)
        if access is not None:
            _access_token, _identity, rotated_session = access
    context = {
        **_base_context(request, user),
        "account_initials": account_initials(user["display_name"], user["email"]),
        "feedback_url": feedback_link(feedback_email()),
    }
    response = request.app.state.templates.TemplateResponse(request, "settings.html", context)
    return _with_rotated_supabase_session(response, request, rotated_session)


async def revoke_oauth_grant(request: Request) -> Response:
    from api.controllers.account import ai_response

    client_id = _valid_oauth_id(request.path_params.get("client_id"))
    if client_id is None:
        return PlainTextResponse("Invalid OAuth client", status_code=400)
    form = await request.form()
    conn = _connect(request)
    try:
        user = _load_user(request, conn)
    finally:
        conn.close()
    if isinstance(user, Response):
        return user
    if not _require_csrf(request, form):
        return PlainTextResponse("Invalid CSRF token", status_code=403)
    access = await _supabase_access_for_user(request, user)
    if access is None:
        return await ai_response(
            request, user, None, error="Sign in again to manage access.", status_code=401,
        )
    access_token, _identity, rotated_session = access
    try:
        if form.get("confirmed") != "yes":
            grants = await request.app.state.supabase_auth_gateway.list_grants(access_token)
            grant = next((item for item in grants if item.client_id == client_id), None)
            if grant is None:
                return await ai_response(
                    request, user, access,
                    error="This client no longer has access.", status_code=404,
                )
            response = request.app.state.templates.TemplateResponse(
                request, "remove_access.html",
                {**_base_context(request, user), "grant": grant},
            )
            return _with_rotated_supabase_session(response, request, rotated_session)
        await request.app.state.supabase_auth_gateway.revoke_grant(
            client_id,
            access_token,
        )
    except SupabaseAuthError:
        return await ai_response(
            request, user, access,
            error="Could not remove access. Try again.", status_code=400,
        )
    response = RedirectResponse("/settings/ai", status_code=303)
    return _with_rotated_supabase_session(response, request, rotated_session)


async def update_status(request: Request) -> Response:
    event_id = int(request.path_params["event_id"])
    form = await request.form()
    conn = _connect(request)
    try:
        user = _load_user(request, conn)
        if isinstance(user, Response):
            return user
        if not _require_csrf(request, form):
            return PlainTextResponse("Invalid CSRF token", status_code=403)
        if not _event_exists(conn, event_id):
            return PlainTextResponse("Event not found", status_code=404)
        previous_status = _previous_user_status(conn, user["id"], event_id)
        status = str(form.get("status") or "")
        try:
            if status == "new":
                storage.clear_user_event_state(conn, user["id"], event_id)
            else:
                storage.set_user_event_status(conn, user["id"], event_id, status)
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        target = _safe_return_to(str(form.get("return_to") or ""))
        if status in HIDDEN_STATUSES:
            target = _with_query_params(
                target,
                undo_event=event_id,
                undo_status=previous_status,
            )
        return RedirectResponse(
            target,
            status_code=303,
        )
    finally:
        conn.close()


async def event_action(request: Request) -> Response:
    event_id = int(request.path_params["event_id"])
    form = await request.form()
    conn = _connect(request)
    try:
        user = _load_user(request, conn)
        if isinstance(user, Response):
            return user
        if not _require_csrf(request, form):
            return PlainTextResponse("Invalid CSRF token", status_code=403)
        action_details = _event_action_details(conn, event_id)
        if action_details is None:
            return PlainTextResponse("Event not found", status_code=404)
        action_kind, target = action_details
        if action_kind == "register":
            storage.set_user_event_status(conn, user["id"], event_id, "registering")
        if target == "#":
            target = _safe_return_to(str(form.get("return_to") or ""))
        return RedirectResponse(target, status_code=303)
    finally:
        conn.close()


async def update_rating(request: Request) -> Response:
    event_id = int(request.path_params["event_id"])
    form = await request.form()
    conn = _connect(request)
    try:
        user = _load_user(request, conn)
        if isinstance(user, Response):
            return user
        if not _require_csrf(request, form):
            return PlainTextResponse("Invalid CSRF token", status_code=403)
        if not _event_exists(conn, event_id):
            return PlainTextResponse("Event not found", status_code=404)
        raw_rating = str(form.get("rating") or "")
        try:
            rating = None if raw_rating == "" else int(raw_rating)
        except ValueError:
            return PlainTextResponse("rating must be -1, 0, 1, or None", status_code=400)
        try:
            storage.set_user_event_rating(conn, user["id"], event_id, rating)
        except ValueError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        return RedirectResponse(
            _safe_return_to(str(form.get("return_to") or "")),
            status_code=303,
        )
    finally:
        conn.close()


def create_app(
    *,
    db_path: Path | None = None,
    session_secret: str | None = None,
    google_oauth_client: Any | None = None,
    supabase_auth_gateway: Any | None = None,
    supabase_mcp_token_verifier: Any | None = None,
    agent_api_token: str | None = None,
    mcp_resource_url: str | None = None,
    invitation_mailer: Any | None = None,
    invitation_poll_seconds: float = POLL_SECONDS,
) -> Starlette:
    from api.controllers.account import about, ai_integration
    from api.controllers.public_entry import invitation_request, preview, public_document
    from api.integrations.invitation_mail import BrevoMailer
    from api.repositories.invitations import InvitationRepository
    from api.services.invitations import InvitationService
    from api.services.public_preview import PublicPreview

    secret = session_secret or os.environ.get("NYC_AI_EVENTS_SESSION_SECRET")
    _validate_friend_mode_config(
        secret=secret,
        google_oauth_client=google_oauth_client,
        supabase_auth_gateway=supabase_auth_gateway,
    )
    if not secret:
        secret = secrets.token_urlsafe(32)
        LOGGER.warning(
            "NYC_AI_EVENTS_SESSION_SECRET is not set; using an ephemeral local-dev secret"
        )
    token = configured_agent_api_token() if agent_api_token is None else agent_api_token.strip()
    if agent_api_token is None:
        auth_mode = mcp_auth_mode()
    elif token:
        if supabase_url() or supabase_publishable_key():
            raise RuntimeError("configure exactly one MCP auth provider")
        auth_mode = "static"
    else:
        auth_mode = "disabled"
    resource_url = (
        configured_mcp_resource_url()
        if mcp_resource_url is None
        else mcp_resource_url.strip().rstrip("/")
    )
    _validate_agent_mcp_config(
        auth_mode=auth_mode,
        token=token,
        resource_url=resource_url,
    )
    database = None if db_path is not None else storage.open_database()
    if supabase_auth_gateway is None and supabase_configured():
        supabase_auth_gateway = SupabaseAuthGateway(
            project_url=supabase_url(),
            publishable_key=supabase_publishable_key(),
            origin=public_base_url(),
        )
    if open_google_signup() and supabase_auth_gateway is None:
        raise RuntimeError("Open Google signup requires Supabase Auth")
    mcp_server = None
    mcp_app = None
    if auth_mode != "disabled":
        if auth_mode == "supabase":
            verifier = supabase_mcp_token_verifier or SupabaseMCPTokenVerifier(
                issuer_url=supabase_issuer_url(),
                jwks_url=supabase_jwks_url(),
                resource_url=resource_url,
                auth_gateway=supabase_auth_gateway,
                db_path=db_path,
                database=database,
            )
            mcp_server = create_event_scout_mcp(
                token_verifier=verifier,
                issuer_url=supabase_issuer_url(),
                required_scopes=[SUPABASE_READ_SCOPE],
                resource_url=resource_url,
                db_path=db_path,
                database=database,
            )
        else:
            mcp_server = create_event_scout_mcp(
                token=token,
                resource_url=resource_url,
                db_path=db_path,
                database=database,
            )
        mcp_app = mcp_server.streamable_http_app(
            json_response=True,
            transport_security=transport_security_for(resource_url),
        )
        mcp_app.add_middleware(AuthenticationAvailabilityMiddleware)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with invitation_delivery(
            _app.state.invitations,
            enabled=not open_google_signup() and (
                invitation_mailer is not None or bool(os.environ.get("EVENT_HUNT_BREVO_API_KEY"))
            ),
            poll_seconds=invitation_poll_seconds,
        ):
            if mcp_server is None:
                yield
                return
            async with mcp_server.session_manager.run():
                yield

    app = Starlette(
        debug=False,
        lifespan=lifespan,
        routes=[
            Route("/", home, methods=["GET"]),
            Route("/favicon.ico", favicon, methods=["GET"]),
            Route("/healthz", health_response, methods=["GET"]),
            Route("/login", login_form, methods=["GET"]),
            Route("/login", login, methods=["POST"]),
            Route("/auth/google/start", google_start, methods=["GET"]),
            Route("/auth/google/callback", google_callback, methods=["GET"]),
            Route("/auth/supabase/start", supabase_start, methods=["GET"]),
            Route(
                "/auth/supabase/callback",
                supabase_callback,
                methods=["GET"],
                name="supabase_callback",
            ),
            Route("/oauth/consent", oauth_consent, methods=["GET"]),
            Route("/oauth/consent", oauth_consent_decision, methods=["POST"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/events", events, methods=["GET"]),
            Mount("/assets/discovery", app=StaticFiles(
                directory=_web_asset_dir(),
                check_dir=False,
            )),
            Route("/api/discovery", discovery, methods=["GET"]),
            Route("/api/preview", preview, methods=["GET"]),
            Route("/api/invitation-requests", invitation_request, methods=["POST"]),
            Route("/calendar", calendar, methods=["GET"]),
            Route("/mine", mine, methods=["GET"]),
            Route("/hidden", hidden, methods=["GET"]),
            Route("/settings", settings, methods=["GET"]),
            Route("/settings/ai", ai_integration, methods=["GET"]),
            Route("/about", about, methods=["GET"]),
            Route("/privacy", public_document, methods=["GET"]),
            Route("/terms", public_document, methods=["GET"]),
            Route(
                "/settings/oauth-grants/{client_id:str}/revoke",
                revoke_oauth_grant,
                methods=["POST"],
            ),
            Route("/events/{event_id:int}/action", event_action, methods=["POST"]),
            Route("/events/{event_id:int}/status", update_status, methods=["POST"]),
            Route("/events/{event_id:int}/rating", update_rating, methods=["POST"]),
        ],
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=f"{secret}:oauth",
        session_cookie="nyc_ai_events_oauth",
        max_age=600,
        same_site="lax",
        https_only=friend_mode_enabled() or public_base_url().startswith("https://"),
    )
    app.state.db_path = db_path
    app.state.mcp_resource_url = resource_url
    app.state.database = database
    app.state.public_preview = PublicPreview()
    app.state.invitation_source_secret = secret.encode()
    app.state.invitations = InvitationService(
        InvitationRepository(db_path=db_path, database=database),
        invitation_mailer if invitation_mailer is not None else BrevoMailer(), public_base_url(),
    )
    app.state.session_serializer = make_serializer(secret)
    app.state.login_rate_limiter = LoginRateLimiter()
    app.state.oauth_start_rate_limiter = RequestRateLimiter(
        max_requests=OAUTH_START_MAX_REQUESTS,
        window_seconds=OAUTH_START_WINDOW_SECONDS,
    )
    app.state.invite_login_enabled = not (
        friend_mode_enabled() or public_base_url().startswith("https://")
    )
    if google_oauth_client is None and google_oauth_configured():
        google_oauth_client = GoogleOAuthClient(
            client_id=google_oauth_client_id(),
            client_secret=google_oauth_client_secret(),
        )
    app.state.google_oauth_client = google_oauth_client
    app.state.supabase_auth_gateway = supabase_auth_gateway
    app.state.templates = _templates()
    app.router.routes.append(Route("/api/events", list_events_api, methods=["GET"]))
    if mcp_app is not None:
        app.router.routes.append(Mount("/", app=mcp_app))
    return app


def _validate_agent_mcp_config(*, auth_mode: str, token: str, resource_url: str) -> None:
    if auth_mode == "disabled":
        return
    errors = []
    if auth_mode == "static":
        error = agent_api_token_config_error(token)
        if error is not None:
            errors.append(error)
    resource_error = mcp_resource_url_config_error(resource_url)
    if resource_error is not None:
        errors.append(resource_error)
    if errors:
        raise RuntimeError("agent MCP requires: " + ", ".join(errors))


def _validate_friend_mode_config(
    *,
    secret: str | None,
    google_oauth_client: Any | None,
    supabase_auth_gateway: Any | None,
) -> None:
    if not friend_mode_enabled():
        return
    missing: list[str] = []
    base_url_error = public_base_url_config_error()
    if base_url_error is not None:
        missing.append(base_url_error)
    secret_error = session_secret_config_error(secret)
    if secret_error is not None:
        missing.append(secret_error)
    if google_oauth_client is None and supabase_auth_gateway is None:
        if supabase_configured():
            pass
        elif supabase_url() or supabase_publishable_key():
            missing.extend(supabase_config_errors())
        else:
            missing.extend(google_oauth_config_errors())
    if missing:
        raise RuntimeError(
            "friend mode requires: " + ", ".join(missing)
        )


__all__ = ["create_app"]
