"""Returning browsers receive updated entrypoints without clearing their cache."""

import hashlib
import re

import pytest
from starlette.testclient import TestClient

import api.app as web_app
from api.tests.fixtures.web_helpers import add_user, migrated_db


@pytest.mark.parametrize(("page", "filename"), [
    ("/events", "main.css"),
    ("/events", "main.js"),
    ("/settings", "account.css"),
    ("/settings", "account.js"),
])
def test_entrypoint_url_tracks_the_actual_served_bytes(tmp_path, monkeypatch, page, filename):
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ("main.css", "main.js", "account.css", "account.js"):
        (assets / name).write_bytes(b"/* first release */")
    monkeypatch.setattr(web_app, "_web_asset_dir", lambda: assets, raising=False)
    db = migrated_db(tmp_path)
    add_user(db)

    def load_url():
        with TestClient(web_app.create_app(db_path=db, session_secret="test-secret")) as client:
            client.post("/login", data={"invite_code": "yang-secret"})
            response = client.get(page)
            assert response.status_code == 200
            url = re.search(rf'["\'](/assets/discovery/{re.escape(filename)}[^"\']*)',
                            response.text).group(1)
            body = (assets / filename).read_bytes()
            assert url == f"/assets/discovery/{filename}?v={hashlib.sha256(body).hexdigest()[:16]}"
            asset_response = client.get(url)
            assert asset_response.status_code == 200
            assert asset_response.content == body
            return url

    first_url = load_url()
    assert load_url() == first_url  # Unchanged bytes do not churn the browser cache.
    (assets / filename).write_bytes(b"/* updated release */")
    assert load_url() != first_url


def test_missing_build_assets_do_not_prevent_app_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "_web_asset_dir", lambda: tmp_path / "not-built", raising=False)
    db = migrated_db(tmp_path)
    add_user(db)
    with TestClient(web_app.create_app(db_path=db, session_secret="test-secret")) as client:
        client.post("/login", data={"invite_code": "yang-secret"})
        response = client.get("/events")
        assert response.status_code == 200
        assert 'href="/assets/discovery/main.css"' in response.text
