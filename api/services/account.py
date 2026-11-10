"""Small display helpers for the Account and AI integration screens."""

from __future__ import annotations

from email.errors import HeaderParseError
from email.headerregistry import Address
from urllib.parse import quote, urlencode


def feedback_link(recipient: str) -> str | None:
    """Open a draft to a configured recipient; never invent a contact address."""
    if not recipient:
        return None
    try:
        address = Address(addr_spec=recipient)
    except (ValueError, HeaderParseError, IndexError):
        # Python 3.11's address parser raises IndexError for an empty domain.
        return None
    if not address.domain:
        return None
    return f"mailto:{quote(address.addr_spec, safe='@')}?{urlencode({'subject': 'Event Hunt feedback'})}"


def account_initials(display_name: str | None, email: str) -> str:
    """Use the actual name, or email local part, for the fallback avatar."""
    words = (display_name or email.partition("@")[0]).split()
    return "".join(word[0] for word in words[:2]).upper()


def mcp_setup_prompt(resource_url: str) -> str:
    """Describe the configured remote MCP connection without a shared credential."""
    return (
        f"Help me connect Event Hunt via remote MCP at {resource_url}. "
        "Use OAuth and guide me through sign-in."
    )


def oauth_permissions(scopes: tuple[str, ...]) -> list[str]:
    """Explain event read access and the provider's requested identity scopes."""
    permissions = ["Read event recommendations"]
    for scope in scopes:
        if scope == "openid":
            continue  # The signed-in identity is already displayed beside the permissions.
        permissions.append({
            "email": "View your email address",
            "profile": "View your name and profile photo",
            "phone": "View your phone number",
            "offline_access": "Keep this app connected",
        }.get(scope, f"Requested scope: {scope}"))
    return permissions
