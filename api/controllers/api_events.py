"""JSON API endpoint for the frontend events list.

Status: stub. The 401 boundary is enforced (matches the private-alpha
HTML contract), but the success path returns an empty list. Wiring the
real catalog read through ``api.services.event_catalog`` lands when the
frontend events page is fleshed out.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse


async def list_events_api(request: Request) -> JSONResponse:
    user = getattr(request.state, "user", None)
    if user is None:
        return JSONResponse(
            {"error": "authentication_required"},
            status_code=401,
        )

    # Stub: returns empty list until api.services.event_catalog is wired in.
    return JSONResponse({"events": []})
