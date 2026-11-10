from __future__ import annotations

from starlette.testclient import TestClient

from api.app import create_app
from api.repositories.events import connect
from api.tests.fixtures.web_helpers import add_scored_event, add_user, migrated_db


def _client(tmp_path):
    db = migrated_db(tmp_path)
    user = add_user(db, invite_code="yang-secret")
    other = add_user(
        db,
        email="friend@example.com",
        display_name="Friend",
        invite_code="friend-secret",
    )
    app = create_app(db_path=db, session_secret="test-secret")
    client = TestClient(app)
    client.post("/login", data={"invite_code": "yang-secret"})
    return client, db, user, other


def _csrf(client: TestClient) -> str:
    # Discovery has no mutating forms; account forms still issue the session token.
    html = client.get("/settings").text
    return html.split('name="_csrf_token" value="', 1)[1].split('"', 1)[0]


def test_status_update_scopes_to_current_user(tmp_path) -> None:
    client, db, user, other = _client(tmp_path)
    event_id = add_scored_event(db)

    response = client.post(
        f"/events/{event_id}/status",
        data={"status": "saved", "_csrf_token": _csrf(client)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    conn = connect(db)
    rows = conn.execute("SELECT * FROM user_event_states").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["user_id"] == user["id"]
    assert rows[0]["user_id"] != other["id"]
    assert rows[0]["status"] == "saved"


def test_register_action_marks_event_registering_before_redirect(tmp_path) -> None:
    client, db, user, _ = _client(tmp_path)
    event_id = add_scored_event(db, source="luma", url="https://lu.ma/register-me")

    response = client.post(
        f"/events/{event_id}/action",
        data={"action_kind": "register", "_csrf_token": _csrf(client)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "https://lu.ma/register-me"
    conn = connect(db)
    row = conn.execute(
        "SELECT status FROM user_event_states WHERE user_id = ? AND event_id = ?",
        (user["id"], event_id),
    ).fetchone()
    conn.close()
    assert row["status"] == "registering"


def test_open_action_redirects_without_mutating_state(tmp_path) -> None:
    client, db, user, _ = _client(tmp_path)
    event_id = add_scored_event(db, source="meetup", url="https://meetup.com/open-me")

    response = client.post(
        f"/events/{event_id}/action",
        data={"action_kind": "open", "_csrf_token": _csrf(client)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "https://meetup.com/open-me"
    conn = connect(db)
    row = conn.execute(
        "SELECT status FROM user_event_states WHERE user_id = ? AND event_id = ?",
        (user["id"], event_id),
    ).fetchone()
    conn.close()
    assert row is None


def test_forged_register_action_on_non_luma_event_does_not_mutate_state(tmp_path) -> None:
    client, db, user, _ = _client(tmp_path)
    event_id = add_scored_event(db, source="meetup", url="https://meetup.com/open-me")

    response = client.post(
        f"/events/{event_id}/action",
        data={"action_kind": "register", "_csrf_token": _csrf(client)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "https://meetup.com/open-me"
    conn = connect(db)
    row = conn.execute(
        "SELECT status FROM user_event_states WHERE user_id = ? AND event_id = ?",
        (user["id"], event_id),
    ).fetchone()
    conn.close()
    assert row is None


def test_luma_source_with_non_luma_url_does_not_mark_registering(tmp_path) -> None:
    client, db, user, _ = _client(tmp_path)
    event_id = add_scored_event(db, source="luma", url="https://evil.example/register")

    response = client.post(
        f"/events/{event_id}/action",
        data={"_csrf_token": _csrf(client)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "https://evil.example/register"
    conn = connect(db)
    row = conn.execute(
        "SELECT status FROM user_event_states WHERE user_id = ? AND event_id = ?",
        (user["id"], event_id),
    ).fetchone()
    conn.close()
    assert row is None


def test_legacy_status_api_can_reset_after_retired_hidden_redirect(tmp_path) -> None:
    client, db, user, _ = _client(tmp_path)
    event_id = add_scored_event(db)
    token = _csrf(client)

    response = client.post(
        f"/events/{event_id}/status",
        data={"status": "dismissed", "return_to": "/hidden", "_csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "undo_event=" in response.headers["location"]
    page = client.get(response.headers["location"])
    assert page.url.path == "/events"
    assert "Undo" not in page.text
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT status FROM user_event_states WHERE user_id = ? AND event_id = ?",
            (user["id"], event_id),
        ).fetchone()
        assert row["status"] == "dismissed"
    finally:
        conn.close()

    reset = client.post(
        f"/events/{event_id}/status",
        data={"status": "new", "_csrf_token": token},
        follow_redirects=False,
    )

    assert reset.status_code == 303
    conn = connect(db)
    row = conn.execute(
        "SELECT status FROM user_event_states WHERE user_id = ? AND event_id = ?",
        (user["id"], event_id),
    ).fetchone()
    conn.close()
    assert row is None


def test_manual_applied_pending_and_approved_statuses_persist(tmp_path) -> None:
    client, db, user, _ = _client(tmp_path)
    event_id = add_scored_event(db)
    token = _csrf(client)

    for status in ("applied", "pending_approval", "confirmed"):
        response = client.post(
            f"/events/{event_id}/status",
            data={"status": status, "_csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        conn = connect(db)
        row = conn.execute(
            "SELECT status FROM user_event_states WHERE user_id = ? AND event_id = ?",
            (user["id"], event_id),
        ).fetchone()
        conn.close()
        assert row["status"] == status


def test_rating_update_persists(tmp_path) -> None:
    client, db, user, _ = _client(tmp_path)
    event_id = add_scored_event(db)

    response = client.post(
        f"/events/{event_id}/rating",
        data={"rating": "1", "_csrf_token": _csrf(client)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    conn = connect(db)
    row = conn.execute(
        "SELECT rating FROM user_event_states WHERE user_id = ? AND event_id = ?",
        (user["id"], event_id),
    ).fetchone()
    conn.close()
    assert row["rating"] == 1


def test_invalid_rating_is_rejected(tmp_path) -> None:
    client, db, _, _ = _client(tmp_path)
    event_id = add_scored_event(db)

    response = client.post(
        f"/events/{event_id}/rating",
        data={"rating": "strong", "_csrf_token": _csrf(client)},
        follow_redirects=False,
    )

    assert response.status_code == 400


def test_invalid_status_is_rejected(tmp_path) -> None:
    client, db, _, _ = _client(tmp_path)
    event_id = add_scored_event(db)

    response = client.post(
        f"/events/{event_id}/status",
        data={"status": "maybe", "_csrf_token": _csrf(client)},
        follow_redirects=False,
    )

    assert response.status_code == 400


def test_unknown_event_id_returns_404(tmp_path) -> None:
    client, _, _, _ = _client(tmp_path)

    response = client.post(
        "/events/999/status",
        data={"status": "saved", "_csrf_token": _csrf(client)},
        follow_redirects=False,
    )

    assert response.status_code == 404


def test_double_submit_is_idempotent(tmp_path) -> None:
    client, db, user, _ = _client(tmp_path)
    event_id = add_scored_event(db)
    token = _csrf(client)

    for _ in range(2):
        response = client.post(
            f"/events/{event_id}/status",
            data={"status": "saved", "_csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303

    conn = connect(db)
    count = conn.execute(
        "SELECT count(*) FROM user_event_states WHERE user_id = ? AND event_id = ?",
        (user["id"], event_id),
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_status_redirect_preserves_safe_return_to(tmp_path) -> None:
    client, db, _, _ = _client(tmp_path)
    event_id = add_scored_event(db)

    response = client.post(
        f"/events/{event_id}/status",
        data={
            "status": "saved",
            "return_to": "/calendar?source=luma",
            "_csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )

    assert response.headers["location"] == "/calendar?source=luma"


def test_external_return_to_is_ignored(tmp_path) -> None:
    client, db, _, _ = _client(tmp_path)
    event_id = add_scored_event(db)

    response = client.post(
        f"/events/{event_id}/status",
        data={
            "status": "saved",
            "return_to": "https://evil.example",
            "_csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )

    assert response.headers["location"] == "/events"
