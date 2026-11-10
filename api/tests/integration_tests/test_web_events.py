from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from api.app import create_app
from api.repositories.event_states import set_user_event_status
from api.repositories.events import connect
from api.tests.fixtures.web_helpers import add_scored_event, add_user, migrated_db


def _client(tmp_path):
    db = migrated_db(tmp_path)
    user = add_user(db, invite_code="yang-secret")
    app = create_app(db_path=db, session_secret="test-secret")
    client = TestClient(app)
    client.post("/login", data={"invite_code": "yang-secret"})
    return client, db, user


def _anonymous_client(tmp_path):
    db = migrated_db(tmp_path)
    app = create_app(db_path=db, session_secret="test-secret")
    return TestClient(app), db


def test_anonymous_events_renders_only_the_public_shell(tmp_path) -> None:
    client, db = _anonymous_client(tmp_path)
    add_scored_event(
        db,
        title="Open Alpha Night",
        url="https://lu.ma/open-alpha",
        score=0.91,
    )

    response = client.get("/events", follow_redirects=False)

    assert response.status_code == 200
    assert 'data-public-preview="true"' in response.text
    assert client.get("/api/discovery", follow_redirects=False).status_code == 303
    assert "Open Alpha Night" not in response.text


def test_events_serves_discovery_shell_and_safe_catalog(tmp_path) -> None:
    client, db, _ = _client(tmp_path)
    add_scored_event(db, title="AI Infra Night", score=0.91, reasoning="Infra-heavy crowd")

    response = client.get("/events")

    assert response.status_code == 200
    assert "<title>Events - Event Hunt</title>" in response.text
    assert "Open profile" in response.text
    assert "Register" not in response.text
    assert "Saved" not in response.text
    assert "Approved" not in response.text
    event = client.get("/api/discovery").json()["events"][0]
    assert event["title"] == "AI Infra Night"
    assert event["score"] == 0.91
    assert event["fitIcon"] == "🔥"
    assert event["highlight"] is None  # Never substitute fit reasoning.
    assert "source" not in event


def test_events_include_earlier_today_and_hide_prior_days_by_default(tmp_path) -> None:
    client, db, _ = _client(tmp_path)
    earlier_today = datetime.now(ZoneInfo("America/New_York")).replace(hour=1, minute=0, second=0, microsecond=0)
    add_scored_event(db, title="Earlier Today", score=0.91, starts_at=earlier_today)
    add_scored_event(db, title="Future strong", score=0.91)
    add_scored_event(db, title="Past strong", score=0.95, days_from_now=-3)
    add_scored_event(db, title="Future weak", score=0.60)

    response = client.get("/api/discovery")

    assert "Earlier Today" in response.text
    assert "Future strong" in response.text
    assert "Past strong" not in response.text
    assert "Future weak" in response.text


def test_discovery_catalog_includes_lower_scores_for_interactive_filtering(tmp_path) -> None:
    client, db, _ = _client(tmp_path)
    add_scored_event(db, title="Future weak", score=0.60)

    response = client.get("/api/discovery")

    assert "Future weak" in response.text


def test_discovery_ignores_legacy_personal_status_filters(
    tmp_path,
) -> None:
    client, db, user = _client(tmp_path)
    add_scored_event(db, title="No State", url="https://lu.ma/new")
    dismissed_event = add_scored_event(db, title="Dismissed", url="https://lu.ma/no")
    conn = connect(db)
    set_user_event_status(conn, user["id"], dismissed_event, "dismissed")
    conn.close()

    response = client.get("/api/discovery?status=new")
    assert "No State" in response.text
    assert "Dismissed" in response.text

    response = client.get("/api/discovery")
    assert "No State" in response.text
    assert "Dismissed" in response.text


@pytest.mark.parametrize("path", ["/mine", "/hidden", "/mine?status=saved&min_score=0.9"])
def test_retired_personal_pages_redirect_without_changing_history(tmp_path, path) -> None:
    client, db, user = _client(tmp_path)
    event_id = add_scored_event(db, title="Historical event")
    conn = connect(db)
    try:
        set_user_event_status(conn, user["id"], event_id, "dismissed")
        before = tuple(conn.execute("SELECT * FROM user_event_states").fetchone())
    finally:
        conn.close()

    response = client.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/events"
    assert "no-store" in response.headers["cache-control"]
    page = client.get(path)
    assert "<title>Events - Event Hunt</title>" in page.text
    assert 'href="/mine"' not in page.text
    assert 'href="/hidden"' not in page.text
    conn = connect(db)
    try:
        assert tuple(conn.execute("SELECT * FROM user_event_states").fetchone()) == before
    finally:
        conn.close()


def test_discovery_ignores_legacy_source_filters(tmp_path) -> None:
    client, db, _ = _client(tmp_path)
    add_scored_event(db, title="Luma Event", source="luma", url="https://lu.ma/a")
    add_scored_event(
        db,
        title="Meetup Event",
        source="meetup",
        url="https://meetup.com/a",
    )

    response = client.get("/api/discovery?source=meetup")

    assert "Meetup Event" in response.text
    assert "Luma Event" in response.text


def test_discovery_transports_source_text_as_json_not_html(tmp_path) -> None:
    client, db, _ = _client(tmp_path)
    raw_title = "<script>alert(1)</script>"
    raw_reasoning = "<b>bold reason</b>"
    add_scored_event(db, title=raw_title, reasoning=raw_reasoning)

    response = client.get("/api/discovery")

    assert response.headers["content-type"] == "application/json"
    assert response.json()["events"][0]["title"] == raw_title
    assert raw_reasoning not in response.text
    assert response.json()["events"][0]["highlight"] is None


def test_events_reject_unsafe_event_link_href(tmp_path) -> None:
    client, db, _ = _client(tmp_path)
    add_scored_event(db, title="Sketchy Link", url="javascript:alert(1)")

    response = client.get("/api/discovery")

    assert "Sketchy Link" in response.text
    assert 'href="javascript:alert(1)"' not in response.text
    assert response.json()["events"][0]["url"] is None


def test_discovery_returns_an_empty_catalog(tmp_path) -> None:
    client, _db, _ = _client(tmp_path)
    response = client.get("/api/discovery")

    assert response.status_code == 200
    assert response.json()["events"] == []
