"""Browser-test-only app factory. Never used by deployment entry points."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

from api.app import create_app as production_app
from api.integrations.supabase.auth import SupabaseIdentity
from api.mcp.server import SupabaseMCPTokenVerifier
from api.tests.fixtures.oauth_provider import ISSUER, PROJECT, OAuthProviderFixture


def create_app():
    base_url = os.environ["E2E_BASE_URL"]
    resource_url = os.environ["E2E_MCP_RESOURCE_URL"]
    db = Path(os.environ["DATABASE_PATH"])
    os.environ["NYC_AI_EVENTS_PUBLIC_BASE_URL"] = base_url
    os.environ["NYC_AI_EVENTS_SUPABASE_URL"] = PROJECT
    os.environ["NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY"] = "sb_publishable_synthetic"
    os.environ["NYC_AI_EVENTS_FEEDBACK_EMAIL"] = "feedback@example.test"
    provider = OAuthProviderFixture(resource_url=resource_url, browser_origin=base_url)
    provider.issue_token()
    verifier = SupabaseMCPTokenVerifier(
        issuer_url=ISSUER, jwks_url=f"{ISSUER}/.well-known/jwks.json",
        resource_url=resource_url, auth_gateway=provider,
        db_path=db, http_client=provider.provider_http,
    )
    app = production_app(
        db_path=db, supabase_auth_gateway=provider, supabase_mcp_token_verifier=verifier,
        mcp_resource_url=resource_url,
    )

    async def google(request):
        email = "uninvited@example.test" if request.cookies.get("synthetic-uninvited-signin") else "discovery@example.test"
        provider.identity = SupabaseIdentity("synthetic-subject", email, True, "Test Friend", has_google_identity=True)
        callback = urlsplit(request.query_params["callback"])
        params = dict(parse_qsl(callback.query))
        if request.cookies.get("synthetic-cancel-signin"):
            params["error"] = "access_denied"
        else:
            params["code"] = "synthetic-auth-code"
        response = RedirectResponse(urlunsplit(callback._replace(query=urlencode(params))), status_code=303)
        response.delete_cookie("synthetic-cancel-signin")
        response.delete_cookie("synthetic-uninvited-signin")
        return response

    async def ai_callback(request):
        return HTMLResponse("<h1>Your AI app</h1>")

    # These simulate external web destinations, not an application login bypass.
    # Keeping fixture destinations on loopback lets redirects run natively.
    app.router.routes[0:0] = [
        Route("/_test/provider/google", google), Route("/_test/ai/callback", ai_callback),
    ]
    return app
