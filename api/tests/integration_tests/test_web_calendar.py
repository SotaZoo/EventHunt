from __future__ import annotations

from starlette.testclient import TestClient

from api.app import create_app
from api.tests.fixtures.web_helpers import add_scored_event, add_user, migrated_db


def _client(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db, invite_code="yang-secret")
    app = create_app(db_path=db, session_secret="test-secret")
    client = TestClient(app)
    client.post("/login", data={"invite_code": "yang-secret"})
    return client, db


def test_calendar_redirects_to_timeline(tmp_path) -> None:
    client, db = _client(tmp_path)
    add_scored_event(db, title="Tomorrow", url="https://lu.ma/a", days_from_now=1)

    response = client.get("/calendar?source=luma", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/events?source=luma"
