"""Event catalog service shared by machine-readable discovery interfaces."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, JsonValue

from api.models.availability import RegistrationAvailability
from api.repositories.events import fetch_catalog_updated_at, fetch_events_for_time_window
from api.services.event_presentation import event_annotations


class InvalidEventQuery(ValueError):
    """Raised when an event discovery window is unsafe or ambiguous."""


class CatalogQuery(BaseModel):
    start: str
    end: str
    limit: int


class RankingBasis(BaseModel):
    basis: Literal["shared_internal"] = "shared_internal"
    personalized: bool = False


class CatalogEvent(BaseModel):
    id: int
    source: str
    url: str
    title: str
    start_at: str
    ends_at: str | None
    venue: str | None
    neighborhood: str | None
    host: str | None
    fit_score: float
    fit_reasoning: str | None
    urgency: str | None
    urgency_reason: str | None
    ranking_version: str | None
    last_seen: str
    registration_availability: RegistrationAvailability = "unknown"
    availability_checked_at: str | None = None
    highlight: str | None = None
    urgency_note: str | None = None
    urgency_note_expires_at: str | None = None
    annotations_scored_at: str | None = None
    time_warning: str | None = None


class EventCatalogResult(BaseModel):
    query: CatalogQuery
    ranking: RankingBasis
    catalog_updated_at: str | None
    count: int
    events: list[CatalogEvent]


def _utc_iso(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _query_datetime(value: str | datetime, *, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidEventQuery(
                f"{name} must be a valid timezone-aware ISO-8601 timestamp"
            ) from exc
    else:
        raise InvalidEventQuery(f"{name} must be a valid timezone-aware ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidEventQuery(f"{name} must include a timezone offset")
    return parsed.astimezone(UTC)


def find_events(
    conn: Any,
    *,
    start: str | datetime,
    end: str | datetime,
    limit: JsonValue = 5,
) -> EventCatalogResult:
    """Return a bounded ranked shortlist from the already-scored catalog."""
    parsed_start = _query_datetime(start, name="start")
    parsed_end = _query_datetime(end, name="end")
    if parsed_start >= parsed_end:
        raise InvalidEventQuery("start must be before end")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise InvalidEventQuery("limit must be an integer between 1 and 25")
    if not 1 <= limit <= 25:
        raise InvalidEventQuery("limit must be between 1 and 25")

    rows = fetch_events_for_time_window(
        conn,
        start=parsed_start,
        end=parsed_end,
        limit=limit,
    )
    events = []
    for row in rows:
        highlight, urgency_note, urgency_expiry = event_annotations(row)
        events.append(CatalogEvent(
            id=int(row["id"]),
            source=str(row["source"]),
            url=str(row["url"]),
            title=str(row["title"]),
            start_at=_utc_iso(row["start_at"]) or "",
            ends_at=_utc_iso(row["ends_at"]),
            venue=row["venue"],
            neighborhood=row["neighborhood"],
            host=row["host"],
            fit_score=float(row["fit_score"]),
            fit_reasoning=row["fit_reasoning"],
            urgency=row["urgency"],
            urgency_reason=row["urgency_reason"],
            ranking_version=row["taste_sha"],
            last_seen=_utc_iso(row["last_seen"]) or "",
            registration_availability=row["registration_availability"],
            availability_checked_at=_utc_iso(row["availability_checked_at"]),
            highlight=highlight,
            urgency_note=urgency_note,
            urgency_note_expires_at=urgency_expiry.astimezone(UTC).isoformat() if urgency_expiry else None,
            annotations_scored_at=_utc_iso(row["scored_at"]) if highlight or urgency_note else None,
            time_warning=row.get("time_warning"),
        ))
    return EventCatalogResult(
        query=CatalogQuery(
            start=_utc_iso(parsed_start) or "",
            end=_utc_iso(parsed_end) or "",
            limit=limit,
        ),
        ranking=RankingBasis(),
        catalog_updated_at=_utc_iso(fetch_catalog_updated_at(conn)),
        count=len(events),
        events=events,
    )


def serialize_event(row: Any) -> dict[str, Any]:
    """Project a SQLite/Postgres event row into the frontend JSON shape."""
    try:
        starts_at = row["starts_at"]
    except (KeyError, IndexError):
        starts_at = None
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "startsAt": starts_at,
        "url": row["url"],
    }
