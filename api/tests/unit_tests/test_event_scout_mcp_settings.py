import logging

import pytest
from mcp.client import Client

from api.configs.settings import (
    agent_api_token_config_error,
    mcp_resource_url,
    mcp_resource_url_config_error,
)
from api.mcp.server import create_event_scout_mcp, transport_security_for


def test_mcp_resource_url_uses_the_existing_public_host(monkeypatch) -> None:
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.test")

    assert mcp_resource_url() == "https://events.example.test/mcp"


def test_agent_token_rejects_short_and_placeholder_values() -> None:
    assert "at least 32" in (agent_api_token_config_error("short") or "")
    assert "private random value" in (
        agent_api_token_config_error("replace-with-a-private-random-agent-token") or ""
    )


def test_agent_token_requires_a_value(monkeypatch) -> None:
    monkeypatch.delenv("NYC_AI_EVENTS_AGENT_API_TOKEN", raising=False)

    assert agent_api_token_config_error() == "NYC_AI_EVENTS_AGENT_API_TOKEN is required"


def test_mcp_resource_url_requires_exact_https_mcp_path() -> None:
    assert mcp_resource_url_config_error("") is not None
    assert mcp_resource_url_config_error("http://events.example.test/mcp") is not None
    assert mcp_resource_url_config_error("https://events.example.test/not-mcp") is not None
    assert mcp_resource_url_config_error("https://events.example.test/mcp?token=no") is not None
    assert mcp_resource_url_config_error("https://events.example.test/mcp#fragment") is not None
    assert mcp_resource_url_config_error("https://replace-with-event-host.example/mcp") is not None
    assert mcp_resource_url_config_error("https://events.example.test/mcp") is None


def test_transport_security_allows_only_the_resource_host_and_origin() -> None:
    settings = transport_security_for("https://events.example.test/mcp")

    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["events.example.test"]
    assert settings.allowed_origins == ["https://events.example.test"]


@pytest.mark.asyncio
async def test_catalog_connection_failure_is_sanitized(caplog) -> None:
    class BrokenDatabase:
        def open_compat(self):
            raise RuntimeError("private database hostname")

    mcp = create_event_scout_mcp(
        database=BrokenDatabase(),
        token="a" * 32,
        resource_url="https://testserver/mcp",
    )

    with caplog.at_level(logging.ERROR):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "find_events",
                {
                    "start": "2026-09-21T00:00:00Z",
                    "end": "2026-09-28T00:00:00Z",
                    "limit": 5,
                },
            )

    assert result.is_error
    assert "catalog_unavailable: Event Hunt could not read the catalog" in result.content[0].text
    assert "private database hostname" not in result.content[0].text
    assert "private database hostname" not in caplog.text
