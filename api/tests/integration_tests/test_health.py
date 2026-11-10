from pathlib import Path

from starlette.testclient import TestClient

from api.app import create_app
from api.tests.fixtures.web_helpers import migrated_db


def test_healthz_returns_ok(tmp_path: Path) -> None:
    app = create_app(db_path=migrated_db(tmp_path), session_secret="test-secret")
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
