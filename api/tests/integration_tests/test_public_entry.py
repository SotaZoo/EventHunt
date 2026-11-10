import asyncio
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from starlette.testclient import TestClient

from api.app import create_app
from api.repositories.events import connect
from api.tests.fixtures.web_helpers import add_scored_event, add_user, migrated_db


@pytest.fixture
def public_app(tmp_path):
    db = migrated_db(tmp_path)
    return db, TestClient(create_app(db_path=db, session_secret="preview-test-secret"))


def test_preview_is_server_bounded_and_does_not_include_started_or_unavailable_events(public_app):
    db, client = public_app
    now = datetime.now(UTC)
    add_scored_event(db, title="Already started", starts_at=now - timedelta(minutes=1))
    for index, status in enumerate(["full", "closed", "canceled", "open", "waitlist", "unknown", "open"]):
        event_id = add_scored_event(db, title=f"Future {index}", starts_at=now + timedelta(hours=index + 1))
        with connect(db) as conn:
            conn.execute("UPDATE events SET registration_availability=? WHERE id=?", (status, event_id))
    expected = ["Future 3", "Future 4", "Future 5"]
    for query in ["", "?limit=100&offset=3", "?date=2099-01-01&score=0&format=online"]:
        response = client.get("/api/preview" + query)
        assert response.status_code == 200
        assert [event["title"] for event in response.json()["events"]] == expected
        assert set(response.json()) == {"today", "timezone", "events"}
        assert "no-store" in response.headers["cache-control"]
    assert client.get("/api/discovery", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_preview_renders_actual_inventory_without_filler(public_app, count):
    db, client = public_app
    for index in range(count):
        add_scored_event(db, title=f"Event {index}", days_from_now=index + 1)
    assert len(client.get("/api/preview").json()["events"]) == count
    page = client.get("/events", follow_redirects=False)
    assert page.status_code == 200
    assert 'data-public-preview="true"' in page.text
    assert "Early Access" in page.text
    assert "Request an invite" in page.text
    assert "Google account email" not in page.text


def test_existing_invited_user_keeps_full_catalog_and_layout(public_app):
    db, client = public_app
    add_user(db)
    for index in range(5):
        add_scored_event(db, title=f"Event {index}")
    client.post("/login", data={"invite_code": "yang-secret"})
    assert len(client.get("/api/discovery").json()["events"]) == 5
    page = client.get("/events")
    assert 'data-public-preview="false"' in page.text
    assert "Request an invite" not in page.text


def test_public_landing_has_only_usable_public_controls_and_footer(public_app):
    _, client = public_app
    page = client.get('/events')
    assert 'The best events. Picked for you.' in page.text
    assert 'Instant alerts. Never miss out.' in page.text
    for private_control in ['id="calendarPages"', 'id="openFilters"', 'href="/settings"', 'id="monthPicker"']:
        assert private_control not in page.text
    for path in ['/about', '/privacy', '/terms']:
        assert f'href="{path}"' in page.text
        destination = client.get(path, follow_redirects=False)
        assert destination.status_code == 200
        assert 'href="/events"' in destination.text
        assert 'href="/settings"' not in destination.text
    assert 'info@sotazoo.com' in page.text
    assert 'method="post" action="/api/invitation-requests"' in page.text
    assert 'class="request-button" disabled' in page.text


def test_policy_publication_date_is_explicit(public_app, monkeypatch):
    _, client = public_app
    assert 'Draft — not yet published' in client.get('/privacy').text
    monkeypatch.setenv('EVENT_HUNT_POLICY_EFFECTIVE_DATE', '2026-10-22')
    assert 'Effective date: 2026-10-22' in client.get('/terms').text


def test_database_outage_keeps_public_access_without_destroying_valid_session(public_app):
    db, client = public_app
    add_user(db)
    client.post("/login", data={"invite_code": "yang-secret"})
    cookie = client.cookies.get("nyc_ai_events_session")
    with connect(db) as conn:
        conn.execute("ALTER TABLE users RENAME TO unavailable_users")
    page = client.get("/events")
    assert page.status_code == 200
    assert "Early Access" in page.text
    assert client.cookies.get("nyc_ai_events_session") == cookie
    with connect(db) as conn:
        conn.execute("ALTER TABLE unavailable_users RENAME TO users")
    assert 'data-public-preview="false"' in client.get("/events").text


@pytest.mark.asyncio
async def test_preview_refresh_does_not_block_the_event_loop(public_app, monkeypatch):
    from api.controllers import public_entry

    db, client = public_app
    add_scored_event(db, title="Upcoming event", days_from_now=1)
    original = public_entry.catalog
    released = threading.Event()
    responsive = []

    def slow_catalog(*args, **kwargs):
        responsive.append(released.wait(timeout=0.3))
        return original(*args, **kwargs)

    monkeypatch.setattr(public_entry, "catalog", slow_catalog)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as browser:
        task = asyncio.create_task(browser.get("/api/preview"))
        await asyncio.sleep(0.01)
        released.set()
        response = await task
    assert response.status_code == 200
    assert responsive == [True]


@pytest.mark.asyncio
async def test_concurrent_visitors_share_one_bounded_preview_refresh(public_app, monkeypatch):
    from api.controllers import public_entry

    db, client = public_app
    for index in range(4):
        add_scored_event(db, title=f"Upcoming {index}", days_from_now=index + 1)
    original = public_entry.catalog
    reads = []

    def counted_catalog(*args, **kwargs):
        reads.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(public_entry, "catalog", counted_catalog)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as browser:
        responses = await asyncio.gather(*(browser.get(f"/api/preview?offset={index}") for index in range(8)))
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    assert len(responses[0].json()["events"]) == 3
    assert len(reads) == 1


@pytest.mark.parametrize("boundary", ["event start", "midnight", "thirty seconds"])
def test_cached_preview_refreshes_at_time_boundaries(public_app, monkeypatch, boundary):
    from api.services import discovery, public_preview

    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    if boundary == "midnight":
        now = datetime(2026, 10, 23, 3, 59, 59, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(discovery, "datetime", Clock)
    monkeypatch.setattr(public_preview, "datetime", Clock)
    db, client = public_app
    first = add_scored_event(db, title="First", starts_at=now + timedelta(seconds=1 if boundary == "event start" else 3600))
    add_scored_event(db, title="Second", starts_at=now + timedelta(days=1))
    initial = client.get("/api/preview").json()
    assert [event["title"] for event in initial["events"]] == ["First", "Second"]
    if boundary == "thirty seconds":
        with connect(db) as conn:
            conn.execute("UPDATE events SET registration_availability='canceled' WHERE id=?", (first,))
        now += timedelta(seconds=31)
    else:
        now += timedelta(seconds=2)
    refreshed = client.get("/api/preview").json()
    if boundary == "midnight":
        assert refreshed["today"] == "2026-10-23"
    else:
        assert [event["title"] for event in refreshed["events"]] == ["Second"]
