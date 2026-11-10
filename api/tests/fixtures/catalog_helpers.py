"""Synthetic catalog facts and public query seams shared by integration tests."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from api.app import create_app
from api.models.availability import RegistrationAvailability
from api.models.events import RawEvent
from api.repositories.events import connect, update_fit, upsert_raw
from api.services.event_catalog import CatalogEvent, find_events

START = "2026-10-07T18:00:00-04:00"


def web_events(db: Path) -> list[dict[str, Any]]:
    """Read discovery as an invited synthetic web user."""
    with TestClient(create_app(db_path=db, session_secret="test-secret")) as client:
        client.post("/login", data={"invite_code": "yang-secret"})
        response = client.get("/api/discovery")
        assert response.status_code == 200
        result = response.json()["events"]
    return result


def agent_events(db: Path, *, limit: int = 25) -> list[CatalogEvent]:
    """Query the synthetic catalog through the MCP service's public seam."""
    with closing(connect(db)) as conn:
        return find_events(conn, start="2026-10-07T21:00:00Z",
                           end="2026-10-09T23:00:00Z", limit=limit).events


def observed_listing(db: Path, url: str, state: RegistrationAvailability, *, score: float = .8) -> int:
    """Persist a synthetic source fact with the normal ingestion and scoring seams."""
    with closing(connect(db)) as conn:
        event_id = upsert_raw(conn, RawEvent(
            source="luma", url=url, title="AI Builders", starts_at=datetime.fromisoformat(START),
            host="AI Builders", venue="Builders Hall",
            registration_availability=state, availability_evidence=f"source: {state}",
            availability_checked_at=datetime.fromisoformat("2026-10-06T12:00:00+00:00"),
        ))
        update_fit(conn, event_id, fit_score=score, reasoning="Shared fit", urgency="medium",
                   urgency_reason=None, prompt_version="v1", taste_sha="taste")
    return event_id
