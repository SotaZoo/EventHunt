"""Keep verification outages distinct from invalid MCP credentials."""

from __future__ import annotations

import logging
from typing import Literal

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOGGER = logging.getLogger(__name__)


class AuthenticationUnavailable(RuntimeError):
    """Deny access when a dependency cannot establish credential validity."""

    def __init__(
        self, dependency: Literal["signing_keys", "identity_store", "provider_session"],
    ) -> None:
        super().__init__("Authentication verification is unavailable")
        self.dependency = dependency


class AuthenticationAvailabilityMiddleware:
    """Return a temporary failure without asking clients to replace credentials."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        except AuthenticationUnavailable as exc:
            LOGGER.warning("MCP authentication verification unavailable: %s", exc.dependency)
            response = JSONResponse(
                {
                    "error": "temporarily_unavailable",
                    "error_description": "Authentication verification is temporarily unavailable.",
                },
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
