"""Presentation-safe public event facts for the signed-in discovery home."""
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from api.repositories.discovery import upcoming_events
from api.services.event_presentation import event_annotations, fit_score_icon

EVENT_TIMEZONE = ZoneInfo("America/New_York")


def catalog(conn: Any, *, public_preview: bool = False) -> dict[str, Any]:
    """Present the complete eligible catalog using New York dates and source-backed facts."""
    now = datetime.now(EVENT_TIMEZONE)
    start = now if public_preview else now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = upcoming_events(conn, start.astimezone(UTC))
    if public_preview:
        rows = rows[:3]
    events = []
    for row in rows:
        value = row["start_at"]
        when = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        when = when.replace(tzinfo=UTC) if when.tzinfo is None else when
        when = when.astimezone(EVENT_TIMEZONE)
        score = row["fit_score"] if row["scoring_status"] == "scored" else None
        highlight, urgency_note, urgency_expiry = event_annotations(row, now=now)
        place = " · ".join(filter(None, [row["venue"], row["neighborhood"]]))
        if row["attendance_mode"] == "online":
            place = "Online"
        elif row["attendance_mode"] == "mixed":
            place = f"{place} · Online" if place else "Online · In person"
        elif not place and row["attendance_mode"] == "in-person":
            place = "In person"
        url = row["url"]
        try:
            parsed = urlsplit(url)
            safe = parsed.scheme in {"https", "http"} and bool(parsed.netloc) and not parsed.username
        except ValueError:
            safe = False
        events.append({
            "id": row["id"], "title": row["title"], "url": url if safe else None,
            "day": when.date().isoformat(), "hour": when.hour + when.minute / 60 + when.second / 3600,
            "time": when.strftime("%I:%M").lstrip("0"), "period": when.strftime("%p"),
            "startAt": when.isoformat(), "score": score, "format": row["attendance_mode"],
            "fitIcon": fit_score_icon(score),
            "place": place,
            "highlight": highlight,
            "urgencyNote": urgency_note,
            "urgencyNoteExpiresAt": urgency_expiry.astimezone(UTC).isoformat() if urgency_expiry else None,
            "food": "Food provided" if row["food_provided"] == "yes" else None,
            "registrationAvailability": row["registration_availability"],
            "timeWarning": row.get("time_warning"),
        })
    return {"today": now.date().isoformat(), "timezone": "America/New_York", "events": events}
