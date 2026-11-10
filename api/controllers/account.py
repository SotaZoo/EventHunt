"""Account support screens, reusing the web application's admission boundary."""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from api import app as web
from api.integrations.supabase.auth import SupabaseAuthError, SupabaseIdentity, SupabaseSession
from api.services.account import mcp_setup_prompt


def authorization_unavailable(request: Request, *, status_code: int = 400) -> Response:
    """Explain how to restart an unavailable authorization in the AI app."""
    return request.app.state.templates.TemplateResponse(
        request, "oauth_unavailable.html", {"request": request}, status_code=status_code,
    )


async def ai_response(
    request: Request,
    user: Any,
    access: tuple[str, SupabaseIdentity, SupabaseSession | None] | None,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    """Show setup and current provider grants, preserving distinct recovery states."""
    gateway = request.app.state.supabase_auth_gateway
    grants = []
    grants_error = gateway is not None and access is None
    rotated_session = None
    if access is not None:
        access_token, _identity, rotated_session = access
        try:
            grants = await gateway.list_grants(access_token)
        except SupabaseAuthError:
            grants_error = True
    resource_url = request.app.state.mcp_resource_url
    context = {
        **web._base_context(request, user),
        "setup_prompt": mcp_setup_prompt(resource_url) if gateway and resource_url else None,
        "oauth_grants": grants,
        "oauth_grants_enabled": gateway is not None,
        "oauth_grants_error": grants_error,
        "oauth_session_unavailable": gateway is not None and access is None,
        "error": error,
    }
    response = request.app.state.templates.TemplateResponse(
        request, "ai_integration.html", context, status_code=status_code,
    )
    return web._with_rotated_supabase_session(response, request, rotated_session)


async def ai_integration(request: Request) -> Response:
    """Show the admitted user's MCP setup and authorized clients."""
    conn = web._connect(request)
    try:
        user = web._load_user(request, conn)
    finally:
        conn.close()
    if isinstance(user, Response):
        return user
    access = await web._supabase_access_for_user(request, user)
    return await ai_response(request, user, access)


async def about(request: Request) -> Response:
    """Keep the existing About destination usable before and after sign-in."""
    from api.controllers.public_entry import public_document

    return await public_document(request)
