from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest
from mcp import ClientSession
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from api.app import create_app
from api.mcp.server import create_event_scout_mcp
from api.repositories.events import connect
from api.tests.fixtures.web_helpers import add_scored_event, migrated_db


class AcceptGate0SupabaseToken:
    async def verify_token(self, token: str) -> AccessToken | None:
        if token != "signed-supabase-oauth-token":
            return None
        return AccessToken(
            token=token,
            client_id="codex-client",
            scopes=["openid", "email"],
            expires_at=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            resource="https://events.example.com/mcp",
            subject="supabase-sub-1",
        )


@pytest.mark.asyncio
async def test_find_events_returns_the_ranked_catalog_through_mcp(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    first_id = add_scored_event(
        db,
        title="Highest-fit event",
        score=0.96,
        starts_at=now + timedelta(days=2),
        reasoning="Best match for the internal AI founder profile",
    )
    add_scored_event(
        db,
        title="Second event",
        score=0.82,
        starts_at=now + timedelta(days=1),
        reasoning="Useful technical audience",
    )
    add_scored_event(
        db,
        title="Outside the requested week",
        score=0.99,
        starts_at=now + timedelta(days=20),
    )

    mcp = create_event_scout_mcp(
        db_path=db,
        token="a" * 32,
        resource_url="http://testserver/mcp",
    )

    async with Client(mcp) as client:
        listed = await client.list_tools()
        result = await client.call_tool(
            "find_events",
            {
                "start": now.isoformat(),
                "end": (now + timedelta(days=7)).isoformat(),
                "limit": 5,
            },
        )
        limited = await client.call_tool(
            "find_events",
            {
                "start": now.isoformat(),
                "end": (now + timedelta(days=7)).isoformat(),
                "limit": 1,
            },
        )

    assert [tool.name for tool in listed.tools] == ["find_events"]
    assert listed.tools[0].annotations is not None
    assert listed.tools[0].annotations.read_only_hint is True
    assert not result.is_error
    assert result.structured_content == {
        "query": {
            "start": now.isoformat().replace("+00:00", "Z"),
            "end": (now + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
            "limit": 5,
        },
        "ranking": {"basis": "shared_internal", "personalized": False},
        "catalog_updated_at": result.structured_content["catalog_updated_at"],
        "count": 2,
        "events": [
            {
                "id": first_id,
                "source": "luma",
                "url": "https://example.test/luma/highest-fit-event-3-96",
                "title": "Highest-fit event",
                "start_at": (now + timedelta(days=2)).isoformat().replace("+00:00", "Z"),
                "ends_at": None,
                "venue": "Cerebral Valley",
                "neighborhood": None,
                "host": "AI Builders",
                "fit_score": 0.96,
                "fit_reasoning": "Best match for the internal AI founder profile",
                "urgency": "medium",
                "urgency_reason": None,
                "highlight": None,
                "urgency_note": None,
                "urgency_note_expires_at": None,
                "annotations_scored_at": None,
                "time_warning": None,
                "ranking_version": "taste",
                "last_seen": result.structured_content["events"][0]["last_seen"],
                "registration_availability": "unknown",
                "availability_checked_at": None,
            },
            {
                "id": result.structured_content["events"][1]["id"],
                "source": "luma",
                "url": "https://example.test/luma/second-event-3-82",
                "title": "Second event",
                "start_at": (now + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                "ends_at": None,
                "venue": "Cerebral Valley",
                "neighborhood": None,
                "host": "AI Builders",
                "fit_score": 0.82,
                "fit_reasoning": "Useful technical audience",
                "urgency": "medium",
                "urgency_reason": None,
                "highlight": None,
                "urgency_note": None,
                "urgency_note_expires_at": None,
                "annotations_scored_at": None,
                "time_warning": None,
                "ranking_version": "taste",
                "last_seen": result.structured_content["events"][1]["last_seen"],
                "registration_availability": "unknown",
                "availability_checked_at": None,
            },
        ],
    }
    assert result.structured_content["catalog_updated_at"] is not None
    assert limited.structured_content["count"] == 1
    assert limited.structured_content["events"][0]["id"] == first_id


def test_mcp_is_not_mounted_without_an_agent_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NYC_AI_EVENTS_AGENT_API_TOKEN", raising=False)
    app = create_app(db_path=migrated_db(tmp_path), session_secret="test-secret")

    with TestClient(app) as client:
        response = client.get("/mcp")

    assert response.status_code == 404


def test_mcp_configuration_fails_closed_when_enabled_with_a_weak_token(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="NYC_AI_EVENTS_AGENT_API_TOKEN"):
        create_app(
            db_path=migrated_db(tmp_path),
            session_secret="test-secret",
            agent_api_token="short",
            mcp_resource_url="https://testserver/mcp",
        )


def test_mcp_configuration_fails_closed_without_a_public_https_resource(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="HTTPS URL"):
        create_app(
            db_path=migrated_db(tmp_path),
            session_secret="test-secret",
            agent_api_token="a" * 32,
            mcp_resource_url="http://event-scout.test/mcp",
        )


def test_mcp_rejects_missing_malformed_wrong_and_browser_session_credentials(
    tmp_path: Path,
) -> None:
    app = create_app(
        db_path=migrated_db(tmp_path),
        session_secret="test-secret",
        agent_api_token="a" * 32,
        mcp_resource_url="https://testserver/mcp",
    )

    with TestClient(app, base_url="https://testserver") as client:
        health = client.get("/healthz")
        events_page = client.get("/events", follow_redirects=False)
        missing = client.get("/mcp")
        malformed = client.get("/mcp", headers={"Authorization": "Basic bm90LWEtYmVhcmVy"})
        empty_bearer = client.get("/mcp", headers={"Authorization": "Bearer"})
        wrong = client.get("/mcp", headers={"Authorization": "Bearer " + "b" * 32})
        client.cookies.set(
            "nyc_ai_events_session",
            "browser-session-does-not-authorize-agents",
        )
        cookie_only = client.get("/mcp")

    assert health.status_code == 200
    assert events_page.status_code == 200
    assert 'data-public-preview="true"' in events_page.text
    assert missing.status_code == 401
    assert malformed.status_code == 401
    assert empty_bearer.status_code == 401
    assert wrong.status_code == 401
    assert cookie_only.status_code == 401


def test_mcp_rejects_unconfigured_hosts_and_origins(tmp_path: Path) -> None:
    app = create_app(
        db_path=migrated_db(tmp_path),
        session_secret="test-secret",
        agent_api_token="a" * 32,
        mcp_resource_url="https://testserver/mcp",
    )
    headers = {"Authorization": "Bearer " + "a" * 32}

    with TestClient(app, base_url="https://testserver") as client:
        bad_host = client.get("/mcp", headers={**headers, "Host": "evil.example"})
        bad_origin = client.get(
            "/mcp",
            headers={**headers, "Origin": "https://evil.example"},
        )

    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403


@pytest.mark.asyncio
async def test_mounted_mcp_initializes_lists_and_calls_with_bearer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = migrated_db(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    add_scored_event(
        db,
        title="Daily journey event",
        score=0.91,
        starts_at=now + timedelta(days=2),
        reasoning="Strong fit for the real weekly discovery request",
    )
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    app = create_app(
        session_secret="test-secret",
        agent_api_token="a" * 32,
        mcp_resource_url="https://testserver/mcp",
    )
    http_transport = httpx2.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=http_transport,
            base_url="https://testserver",
            headers={"Authorization": "Bearer " + "a" * 32},
        ) as http_client,
        streamable_http_client(
            "https://testserver/mcp",
            http_client=http_client,
        ) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        listed = await session.list_tools()
        result = await session.call_tool(
            "find_events",
            {
                "start": now.isoformat(),
                "end": (now + timedelta(days=7)).isoformat(),
                "limit": 5,
            },
        )

    assert initialized.server_info.name == "event-scout"
    assert initialized.server_info.title == "Event Hunt"
    assert app.state.database is not None
    assert [tool.name for tool in listed.tools] == ["find_events"]
    assert not result.is_error
    assert result.structured_content["events"][0]["title"] == "Daily journey event"


@pytest.mark.asyncio
async def test_mounted_mcp_uses_supabase_oauth_discovery_and_email_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = migrated_db(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    add_scored_event(
        db,
        title="OAuth journey event",
        score=0.93,
        starts_at=now + timedelta(days=1),
    )
    monkeypatch.delenv("NYC_AI_EVENTS_AGENT_API_TOKEN", raising=False)
    monkeypatch.setenv("NYC_AI_EVENTS_PUBLIC_BASE_URL", "https://events.example.com")
    monkeypatch.setenv("NYC_AI_EVENTS_SUPABASE_URL", "https://project-ref.supabase.co")
    monkeypatch.setenv("NYC_AI_EVENTS_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_gate0")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    app = create_app(
        session_secret="test-secret",
        supabase_mcp_token_verifier=AcceptGate0SupabaseToken(),
    )
    transport = httpx2.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=transport,
            base_url="https://events.example.com",
            headers={"Authorization": "Bearer signed-supabase-oauth-token"},
        ) as http_client,
        streamable_http_client(
            "https://events.example.com/mcp",
            http_client=http_client,
        ) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "find_events",
            {
                "start": now.isoformat(),
                "end": (now + timedelta(days=7)).isoformat(),
                "limit": 5,
            },
        )

    assert not result.is_error
    assert result.structured_content["events"][0]["title"] == "OAuth journey event"


@pytest.mark.asyncio
async def test_find_events_defaults_to_five_and_preserves_missing_facts(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    add_scored_event(
        db,
        title="Sparse source event",
        score=0.88,
        starts_at=now + timedelta(days=1),
        venue=None,
    )
    mcp = create_event_scout_mcp(
        db_path=db,
        token="a" * 32,
        resource_url="https://testserver/mcp",
    )

    async with Client(mcp) as client:
        listed = await client.list_tools()
        result = await client.call_tool(
            "find_events",
            {"start": now.isoformat(), "end": (now + timedelta(days=7)).isoformat()},
        )

    schema = listed.tools[0].input_schema
    assert schema["required"] == ["start", "end"]
    assert "timezone-aware" in schema["properties"]["start"]["description"]
    assert "timezone-aware" in schema["properties"]["end"]["description"]
    assert "1 through 25" in schema["properties"]["limit"]["description"]
    assert schema["properties"]["limit"]["type"] == "integer"
    assert schema["properties"]["limit"]["default"] == 5
    assert listed.tools[0].output_schema is not None
    assert result.structured_content["query"]["limit"] == 5
    assert result.structured_content["events"][0]["venue"] is None
    assert result.structured_content["events"][0]["ends_at"] is None
    assert result.structured_content["events"][0]["neighborhood"] is None


@pytest.mark.asyncio
async def test_find_events_returns_explicit_empty_catalog_result(tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    mcp = create_event_scout_mcp(
        db_path=migrated_db(tmp_path),
        token="a" * 32,
        resource_url="https://testserver/mcp",
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "find_events",
            {
                "start": now.isoformat(),
                "end": (now + timedelta(days=7)).isoformat(),
                "limit": 25,
            },
        )

    assert result.structured_content["count"] == 0
    assert result.structured_content["events"] == []
    assert result.structured_content["catalog_updated_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end", "limit", "message"),
    [
        ("2026-09-21T00:00:00", "2026-09-28T00:00:00Z", 5, "timezone"),
        ("not-a-date", "2026-09-28T00:00:00Z", 5, "timestamp"),
        ("2026-09-28T00:00:00Z", "2026-09-21T00:00:00Z", 5, "before"),
        ("2026-09-21T00:00:00Z", "2026-09-28T00:00:00Z", 0, "1 and 25"),
        ("2026-09-21T00:00:00Z", "2026-09-28T00:00:00Z", 26, "1 and 25"),
    ],
)
async def test_find_events_rejects_unsafe_queries(
    tmp_path: Path,
    start: str,
    end: str,
    limit: int,
    message: str,
) -> None:
    mcp = create_event_scout_mcp(
        db_path=migrated_db(tmp_path),
        token="a" * 32,
        resource_url="https://testserver/mcp",
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "find_events",
            {"start": start, "end": end, "limit": limit},
        )

    assert result.is_error
    assert "invalid_query" in result.content[0].text
    assert message in result.content[0].text


@pytest.mark.asyncio
async def test_find_events_rejects_a_non_integer_limit_with_a_stable_error(tmp_path: Path) -> None:
    mcp = create_event_scout_mcp(
        db_path=migrated_db(tmp_path),
        token="a" * 32,
        resource_url="https://testserver/mcp",
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "find_events",
            {
                "start": "2026-09-21T00:00:00Z",
                "end": "2026-09-28T00:00:00Z",
                "limit": "many",
            },
        )

    assert result.is_error
    assert "invalid_query" in result.content[0].text
    assert "limit" in result.content[0].text


@pytest.mark.asyncio
async def test_find_events_does_not_run_scrapers_or_llm_scoring(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = migrated_db(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    add_scored_event(
        db,
        title="Already curated",
        score=0.89,
        starts_at=now + timedelta(days=1),
    )

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("discovery reads must not scrape or score")

    monkeypatch.setattr("api.integrations.llm.agent.score_batch", unexpected_call)
    monkeypatch.setattr(
        "api.integrations.scrapers.luma.fetch_capacity_for_event",
        unexpected_call,
    )
    mcp = create_event_scout_mcp(
        db_path=db,
        token="a" * 32,
        resource_url="https://testserver/mcp",
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "find_events",
            {
                "start": now.isoformat(),
                "end": (now + timedelta(days=7)).isoformat(),
                "limit": 5,
            },
        )

    assert not result.is_error
    assert result.structured_content["events"][0]["title"] == "Already curated"


@pytest.mark.asyncio
async def test_find_events_excludes_unscored_cancelled_and_out_of_window_rows(
    tmp_path: Path,
) -> None:
    db = migrated_db(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    included_id = add_scored_event(
        db,
        title="Included",
        score=0.80,
        starts_at=now + timedelta(days=2),
    )
    cancelled_id = add_scored_event(
        db,
        title="Cancelled",
        score=0.99,
        starts_at=now + timedelta(days=1),
    )
    unscored_id = add_scored_event(
        db,
        title="Unscored",
        score=0.98,
        starts_at=now + timedelta(days=1),
    )
    add_scored_event(
        db,
        title="Too late",
        score=1.0,
        starts_at=now + timedelta(days=20),
    )
    conn = connect(db)
    try:
        conn.execute(
            "UPDATE events SET cancelled_at = ? WHERE id = ?",
            (now.isoformat(), cancelled_id),
        )
        conn.execute(
            "UPDATE events SET scoring_status = 'pending', fit_score = NULL WHERE id = ?",
            (unscored_id,),
        )
        conn.execute(
            "UPDATE events SET last_seen = '2030-01-02T03:04:05+00:00' WHERE title = 'Too late'"
        )
        conn.commit()
    finally:
        conn.close()
    mcp = create_event_scout_mcp(
        db_path=db,
        token="a" * 32,
        resource_url="https://testserver/mcp",
    )

    async with Client(mcp) as client:
        result = await client.call_tool(
            "find_events",
            {
                "start": now.isoformat(),
                "end": (now + timedelta(days=7)).isoformat(),
                "limit": 25,
            },
        )

    assert [event["id"] for event in result.structured_content["events"]] == [included_id]
    assert result.structured_content["catalog_updated_at"] == "2030-01-02T03:04:05Z"


@pytest.mark.asyncio
async def test_find_events_reports_catalog_unavailable_without_internal_details(
    tmp_path: Path,
) -> None:
    missing_schema = tmp_path / "missing-schema.db"
    mcp = create_event_scout_mcp(
        db_path=missing_schema,
        token="a" * 32,
        resource_url="https://testserver/mcp",
    )

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
    assert str(missing_schema) not in result.content[0].text
