from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from starlette.testclient import TestClient

from api.app import create_app
from api.integrations.scrapers.luma import _jsonld_to_raw
from api.models.events import RawEvent
from api.repositories.event_states import set_user_event_status
from api.repositories.events import connect, upsert_raw
from api.tests.fixtures.web_helpers import add_scored_event, add_user, migrated_db


@freeze_time("2026-09-30T20:00:00Z")
def test_discovery_has_all_eligible_events_without_legacy_limits_or_status_filters(tmp_path):
    db = migrated_db(tmp_path)
    user = add_user(db)
    conn = connect(db)
    try:
        for index in range(105):
            add_scored_event(db, title=f"Event {index}", score=0.6)
        event = add_scored_event(db, title="Hidden personally", score=0.91)
        set_user_event_status(conn, user["id"], event, "dismissed")
        upsert_raw(conn, RawEvent(source="luma", url="https://example.test/pending",
                                 title="Not scored yet", starts_at=datetime(2026, 9, 30, 8,
                                 tzinfo=ZoneInfo("America/New_York"))))
        for status in ("failed", "skipped"):
            event = add_scored_event(db, title=status)
            conn.execute("UPDATE events SET scoring_status = ? WHERE id = ?", (status, event))
        event = add_scored_event(db, title="Cancelled")
        conn.execute("UPDATE events SET cancelled_at = CURRENT_TIMESTAMP WHERE id = ?", (event,))
        add_scored_event(db, title="Yesterday", days_from_now=-1)
    finally:
        conn.close()
    client = TestClient(create_app(db_path=db, session_secret="test-secret"))
    assert client.get("/api/discovery", follow_redirects=False).status_code == 303
    client.post("/login", data={"invite_code": "yang-secret"})
    response = client.get("/api/discovery?status=new&source=meetup&min_score=0.7")
    assert response.status_code == 200
    result = response.json()
    assert result["today"] == "2026-09-30"
    assert len(result["events"]) == 107
    pending = result["events"][0]
    assert pending["title"] == "Not scored yet"
    assert pending["score"] is None
    assert pending["day"] == "2026-09-30"
    assert pending["hour"] == 8
    assert pending["format"] is None
    assert {"failed", "skipped", "Cancelled", "Yesterday"}.isdisjoint(
        e["title"] for e in result["events"])
    assert "no-store" in response.headers["cache-control"]


@freeze_time("2026-09-30T20:00:00Z")
def test_source_backed_attendance_round_trips_and_unknown_does_not_erase_evidence(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    conn = connect(db)
    for mode, expected in [("Offline", "in-person"), ("Online", "online"), ("Mixed", "mixed")]:
        raw = _jsonld_to_raw(f"https://example.test/{mode}", {
            "name": mode, "startDate": "2026-10-01T18:00:00Z",
            "eventAttendanceMode": f"https://schema.org/{mode}EventAttendanceMode",
        })
        assert raw.attendance_mode == expected
        upsert_raw(conn, raw)
        # A legacy/partial source refresh must not erase explicit evidence.
        upsert_raw(conn, raw.model_copy(update={"attendance_mode": None}))
    upsert_raw(conn, RawEvent(source="luma", title="Unknown", url="https://example.test/unknown",
                             starts_at=datetime(2026, 10, 1), venue="Online-ish cafe"))
    conn.close()
    client = TestClient(create_app(db_path=db, session_secret="test-secret"))
    client.post("/login", data={"invite_code": "yang-secret"})
    events = {e["title"]: e for e in client.get("/api/discovery").json()["events"]}
    assert events["Offline"]["format"] == "in-person"
    assert events["Offline"]["place"] == "In person"
    assert events["Online"]["format"] == "online"
    assert events["Online"]["place"] == "Online"
    assert events["Mixed"]["format"] == "mixed"
    assert events["Unknown"]["format"] is None


@pytest.mark.parametrize(("score", "icon"), [
    (0, "🔅"), (.6999, "🔅"), (.7, "✨"), (.7999, "✨"),
    (.8, "⭐"), (.8999, "⭐"), (.9, "🔥"), (1, "🔥"),
])
def test_discovery_matches_discord_fit_boundaries(tmp_path, score, icon):
    db = migrated_db(tmp_path)
    add_user(db)
    add_scored_event(db, score=score)
    client = TestClient(create_app(db_path=db, session_secret="test-secret"))
    client.post("/login", data={"invite_code": "yang-secret"})
    assert client.get("/api/discovery").json()["events"][0]["fitIcon"] == icon


@pytest.mark.parametrize(("instant", "start", "day", "hour"), [
    ("2026-11-01T04:30:00Z", "2026-11-01T05:30:00Z", "2026-11-01", 1.5),
    ("2026-11-01T04:30:00Z", "2026-11-01T06:30:00Z", "2026-11-01", 1.5),
    ("2027-03-14T05:30:00Z", "2027-03-14T07:00:00Z", "2027-03-14", 3),
    ("2026-10-01T01:00:00Z", "2026-10-01T02:00:00Z", "2026-09-30", 22),
    ("2027-01-01T04:00:00Z", "2027-01-01T05:00:00Z", "2027-01-01", 0),
])
def test_discovery_uses_new_york_dates_across_dst_and_year_boundaries(tmp_path, instant, start, day, hour):
    with freeze_time(instant):
        db = migrated_db(tmp_path)
        add_user(db)
        add_scored_event(db, starts_at=datetime.fromisoformat(start))
        client = TestClient(create_app(db_path=db, session_secret="test-secret"))
        client.post("/login", data={"invite_code": "yang-secret"})
        event = client.get("/api/discovery").json()["events"][0]
        assert (event["day"], event["hour"]) == (day, hour)


def test_discovery_denies_inactive_user(tmp_path):
    db = migrated_db(tmp_path)
    user = add_user(db)
    client = TestClient(create_app(db_path=db, session_secret="test-secret"))
    client.post("/login", data={"invite_code": "yang-secret"})
    conn = connect(db)
    conn.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user["id"],))
    conn.close()
    assert client.get("/api/discovery", follow_redirects=False).status_code == 303
