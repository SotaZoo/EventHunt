"""Anonymous discovery: explicitly bounded facts, never an authenticated catalog."""
import hashlib
import hmac
import json
import os
import sqlite3
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from api.configs.settings import open_google_signup
from api.repositories.invitations import RequestLimited
from api.services.discovery import catalog
from api.services.invitations import request_email


async def public_document(request: Request) -> Response:
    """Serve public product and policy information without requiring a session."""
    from api import app as web

    context = web._base_context(request)
    context.update(document=request.url.path.strip('/'), current_year=datetime.now(UTC).year,
                   policy_effective_date=os.environ.get('EVENT_HUNT_POLICY_EFFECTIVE_DATE', ''))
    return request.app.state.templates.TemplateResponse(
        request, "public_document.html", context, headers={"Cache-Control": "private, no-store"},
    )


def preview_page(request: Request, *, email: str = "", denied: bool = False, clear_credentials: bool = True) -> Response:
    """Render the public shell, discarding product credentials for denied visitors."""
    from api import app as web

    context = web._base_context(request)
    context.update(public_preview=True, request_email=email, denied_account=denied, current_year=datetime.now(UTC).year,
                   sign_in_url="/auth/supabase/start" if request.app.state.supabase_auth_gateway else "/login")
    response = request.app.state.templates.TemplateResponse(
        request, "public_entry.html", context,
        status_code=403 if denied else 200,
        headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"},
    )
    if clear_credentials:
        web.clear_session_cookie(response)
        web.clear_supabase_session_cookies(response)
    return response


async def preview(request: Request) -> Response:
    """Return only the globally selected three-event preview or a safe error."""
    from api import app as web

    def load():
        conn = web._connect(request)
        try:
            return catalog(conn, public_preview=True)
        finally:
            conn.close()

    try:
        value = await request.app.state.public_preview.get(load)
        return JSONResponse(value, headers={"Cache-Control": "no-store"})
    except (sqlite3.OperationalError, SQLAlchemyError):
        return JSONResponse({"error": "Events are temporarily unavailable"}, status_code=503,
                            headers={"Cache-Control": "no-store"})


async def invitation_request(request: Request) -> Response:
    """Persist before acknowledging. Do not expose account or invite membership."""
    headers = {"Cache-Control": "no-store"}
    if open_google_signup():
        return JSONResponse({"error": "Invitations are paused. Sign in with Google."}, status_code=409, headers=headers)
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        return JSONResponse({"error": "Open Event Hunt and try again."}, status_code=403, headers=headers)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4096:
            return JSONResponse({"error": "Request too large."}, status_code=413, headers=headers)
    try:
        data = json.loads(body)
        email = request_email(data.get("email") if isinstance(data, dict) else None)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "Enter a valid email address."}, status_code=422, headers=headers)
    source = request.client.host if request.client else "unknown"
    source_key = hmac.new(request.app.state.invitation_source_secret, source.encode(), hashlib.sha256).hexdigest()
    workflow = request.app.state.invitations
    try:
        mail_id = await run_in_threadpool(workflow.repository.request, email, source_key)
    except RequestLimited:
        return JSONResponse({"error": "Too many requests. Please try again later."}, status_code=429,
                            headers={**headers, "Retry-After": "600"})
    except (sqlite3.DatabaseError, SQLAlchemyError):
        return JSONResponse({"error": "Your request wasn't saved. Please try again."}, status_code=503, headers=headers)
    return JSONResponse({"message": "Request received. We'll email you if you're invited."},
                        status_code=202, headers=headers, background=BackgroundTask(workflow.deliver, mail_id))
