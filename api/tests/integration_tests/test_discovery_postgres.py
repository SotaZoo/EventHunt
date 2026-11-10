"""Optional real-Postgres parity check; only a disposable Unix-socket cluster.

Set EVENT_HUNT_TEST_PG_SOCKET to the mktemp directory of a local test server
initialized with user eventhunt. No hosted database or credentials are used.
"""
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from starlette.testclient import TestClient

from api.app import create_app
from api.models.availability import AvailabilityObservation
from api.models.events import RawEvent
from api.repositories.availability import record_availability
from api.repositories.database import CompatConnection, create_database
from api.repositories.events import (
    fetch_pending_scoring,
    mark_scoring_failed,
    update_fit,
    upsert_raw,
)
from api.repositories.migrate import apply_pending_sqlalchemy, migration_dir_for_dialect
from api.repositories.users import create_user
from api.services.event_catalog import find_events


def test_postgres_discovery_migrations_worker_permissions_and_old_insert_compatibility(monkeypatch):
    socket = os.environ.get("EVENT_HUNT_TEST_PG_SOCKET")
    if not socket:
        pytest.skip("requires a disposable local Postgres test cluster")
    assert Path(socket).name.startswith("event-hunt-pg-")
    assert str(Path(socket).resolve()).startswith(("/private/tmp/", "/tmp/"))
    with psycopg.connect(host=socket, port=55448, user="eventhunt", dbname="postgres", autocommit=True) as admin:
        for role in ("anon", "authenticated", "supabase_auth_admin"):
            admin.execute(psycopg.sql.SQL("CREATE ROLE {} NOLOGIN").format(psycopg.sql.Identifier(role)))
        admin.execute("CREATE DATABASE discovery_test")
    monkeypatch.setenv("DATABASE_URL", f"postgresql://eventhunt@/discovery_test?host={socket}&port=55448")
    database = create_database()
    try:
        with database.begin() as conn:
            migrations = migration_dir_for_dialect("postgres")
            apply_pending_sqlalchemy(conn, migrations)
            assert apply_pending_sqlalchemy(conn, migrations) == []
            compat = CompatConnection(conn, dialect="postgres")
            create_user(compat, email="test@example.test", display_name="Test", invite_code="pg-invite")
            when = datetime.now(UTC) + timedelta(days=1)
            # This is the old worker's insert shape, with no new column.
            conn.execute(text("""INSERT INTO events (source, title, url, start_at)
                              VALUES ('luma', 'Legacy worker', 'https://example.test/legacy', :start)"""), {"start": when})
            conn.execute(text("SET LOCAL ROLE event_scout_worker"))
            raw = RawEvent(source="luma", title="New worker", url="https://lu.ma/current",
                           starts_at=when, attendance_mode="mixed", registration_availability="waitlist",
                           description="Engineers provide live debugging help. Limited spots available.",
                           registration_url="https://lu.ma/event/evt-Synthetic123",
                           availability_evidence="event registration control: join waitlist")
            annotated_id = upsert_raw(compat, raw)
            upsert_raw(compat, raw.model_copy(update={"title": "Updated worker"}))
            upsert_raw(compat, raw.model_copy(update={"title": "Updated worker", "venue": "Betaworks",
                                                     "registration_url": None}))
            assert conn.execute(text("SELECT registration_url FROM events WHERE title = 'Updated worker'")).scalar_one() == raw.registration_url
            upsert_raw(compat, RawEvent(source="betaworks", title="Updated worker",
                                       url="https://www.betaworks.com/event/current", starts_at=when,
                                       registration_url="https://lu.ma/event/evt-Synthetic123"))
            meetup = RawEvent(source="meetup", title="Updated worker", starts_at=when,
                              url="https://www.meetup.com/builders/events/12345/",
                              description="Register on [luma](https://lu.ma/current).")
            upsert_raw(compat, meetup)
            upsert_raw(compat, meetup.model_copy(update={"venue": "Betaworks", "description": None}))
            upsert_raw(compat, RawEvent(source="luma", title="Provider alias", url="https://luma.com/current?utm_source=friend",
                                       starts_at=when))
            for state in ("full", "closed", "canceled"):
                event_id = upsert_raw(compat, RawEvent(source="luma", title=state, url=f"https://lu.ma/{state}",
                                                      starts_at=when, registration_availability=state,
                                                      availability_evidence=f"schema.org event fact: {state}"))
                assert not record_availability(compat, event_id, AvailabilityObservation(state="open", evidence="later stale control"))
            upsert_raw(compat, RawEvent(source="luma", title="Terminal alias", url="https://luma.com/closed?utm_source=friend",
                                       starts_at=when))
            assert {row["title"] for row in fetch_pending_scoring(compat, limit=20)} == {
                "Legacy worker", "Updated worker", "Provider alias",
            }
            for row in conn.execute(text("SELECT id, title, fit_input_hash FROM events")):
                update_fit(compat, row.id, fit_score=.8 if row.title == "Updated worker" else .6,
                           reasoning="Synthetic shared fit", urgency="medium", urgency_reason=None,
                           prompt_version="v1", taste_sha="synthetic", expected_input_hash=row.fit_input_hash)
            legacy_id = conn.execute(text("SELECT id FROM events WHERE title='Legacy worker'")).scalar_one()
            mark_scoring_failed(compat, legacy_id, "refresh failed", expected_input_hash=None)
            assert update_fit(compat, annotated_id, fit_score=.9, reasoning="Shared fit",
                              urgency="low", urgency_reason=None, prompt_version="v3", taste_sha="synthetic",
                              highlight={"text": "Get debugging help from engineers",
                                         "evidence": "Engineers provide live debugging help."},
                              urgency_note={"kind": "scarcity", "text": "Limited spots",
                                            "evidence": "Limited spots available."})
            assert conn.execute(text("SELECT highlight FROM events WHERE title = 'Legacy worker'")).scalar_one() is None
            assert conn.execute(text("SELECT scored_at FROM events WHERE id = :id"), {"id": annotated_id}).scalar_one() is not None
            assert conn.execute(text("SELECT registration_availability FROM events WHERE title = 'Legacy worker'")).scalar_one() == "unknown"
            assert conn.execute(text("SELECT registration_url FROM events WHERE title = 'Legacy worker'")).scalar_one() is None
            with pytest.raises(ProgrammingError), conn.begin_nested():
                conn.execute(text("SELECT email FROM users"))
            result = find_events(compat, start=when - timedelta(hours=1), end=when + timedelta(hours=1), limit=2)
            assert [event.title for event in result.events] == ["Updated worker", "Legacy worker"]
            assert result.events[0].registration_availability == "waitlist"
            assert result.events[0].availability_checked_at is not None
            assert result.events[0].highlight == "Get debugging help from engineers"
            assert result.events[0].urgency_note == "At last check: Limited spots"
            assert all(row["id"] != annotated_id for row in fetch_pending_scoring(compat, refresh_prompt_version="v3"))
            conn.execute(text("RESET ROLE"))
        client = TestClient(create_app(session_secret="test-secret"))
        client.post("/login", data={"invite_code": "pg-invite"})
        response = client.get("/api/discovery")
        assert response.status_code == 200
        assert {event["title"]: event["format"] for event in response.json()["events"]} == {
            "Legacy worker": None, "Updated worker": "mixed",
        }
        assert {event["title"]: event["registrationAvailability"] for event in response.json()["events"]} == {
            "Legacy worker": "unknown", "Updated worker": "waitlist",
        }
        updated = next(event for event in response.json()["events"] if event["title"] == "Updated worker")
        assert updated["highlight"] == "Get debugging help from engineers"
        assert updated["urgencyNote"] == "At last check: Limited spots"
        assert updated["urgencyNoteExpiresAt"] is not None
        client.app.state.database.engine.dispose()
        with database.begin() as conn:
            conn.execute(text("SET LOCAL ROLE event_scout_worker"))
            compat = CompatConnection(conn, dialect="postgres")
            upsert_raw(compat, RawEvent(source="luma", title="Legacy worker", url="https://example.test/legacy",
                                      starts_at=when, description="New confirmed program."))
            assert not update_fit(compat, legacy_id, fit_score=.99, reasoning="Late old result", urgency="low",
                                  urgency_reason=None, prompt_version="v3", taste_sha="old", expected_input_hash=None)
            assert "Legacy worker" in {row["title"] for row in fetch_pending_scoring(compat)}
        from api.repositories.invitations import InvitationRepository
        repo = InvitationRepository(database=database)
        notification_id = repo.request("newfriend@company.test", "synthetic-source")
        assert repo.request("newfriend@company.test", "synthetic-source-2") == notification_id
        invitation_id = repo.approve(1)
        assert repo.approve(1) == invitation_id
        claim = repo.claim(invitation_id)
        assert claim["email"] == "newfriend@company.test"
        repo.finish(invitation_id, "failed", "provider_rejected", attempt_token=claim["attempt_token"])
        assert repo.retry(invitation_id) == "pending"
        claim = repo.claim(invitation_id)
        repo.finish(invitation_id, "unknown", "connection_lost", attempt_token=claim["attempt_token"])
        with pytest.raises(ValueError, match="reconcile"):
            repo.retry(invitation_id)
        repo.reconcile(invitation_id, "not-sent")
        assert repo.retry(invitation_id) == "pending"
        claim = repo.claim(invitation_id)
        repo.finish(invitation_id, "sent", attempt_token=claim["attempt_token"], provider_message_id="<synthetic-pg-receipt>")
        assert repo.requests()[0]["invitation_status"] == "sent"
        assert repo.requests()[0]["invitation_provider_id"] == "<synthetic-pg-receipt>"
        with database.begin() as conn:
            for role in ("anon", "authenticated", "event_scout_worker"):
                for table in ("invitation_requests", "invitation_mail", "invitation_limits", "invitation_guard"):
                    assert not conn.execute(text("SELECT has_table_privilege(:role,:table,'SELECT')"),
                                            {"role": role, "table": table}).scalar_one()
                    assert conn.execute(text("SELECT relrowsecurity FROM pg_class WHERE relname=:table"),
                                        {"table": table}).scalar_one()
    finally:
        database.engine.dispose()
        with psycopg.connect(host=socket, port=55448, user="eventhunt", dbname="postgres", autocommit=True) as admin:
            admin.execute("DROP DATABASE discovery_test")
            for role in ("event_scout_worker", "anon", "authenticated", "supabase_auth_admin"):
                admin.execute(psycopg.sql.SQL("DROP ROLE {}").format(psycopg.sql.Identifier(role)))
