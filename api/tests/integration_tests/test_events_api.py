from pathlib import Path

from starlette.testclient import TestClient

from api.app import create_app
from api.tests.fixtures.web_helpers import migrated_db


def test_events_api_requires_authenticated_user(tmp_path: Path) -> None:
    app = create_app(db_path=migrated_db(tmp_path), session_secret="test-secret")
    client = TestClient(app)
    response = client.get("/api/events")
    assert response.status_code in {401, 403}
