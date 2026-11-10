"""Presentation rules shared by Discord and web discovery."""
from datetime import UTC, datetime, timedelta
from typing import Any

from api.models.event_annotations import EventHighlight, EventUrgencyNote, grounded_annotation
from api.models.occurrences import utc_instant

# A conservative observation window for the existing twice-daily catalog worker.
URGENCY_OBSERVATION_MAX_AGE = timedelta(days=1)


def fit_score_icon(score: float | None) -> str:
    """Return the shared Discord and web fit indicator, omitting unscored events."""
    if score is None:
        return ""
    if score >= 0.90:
        return "🔥"
    if score >= 0.80:
        return "⭐"
    if score >= 0.70:
        return "✨"
    return "🔅"


def event_annotations(row: Any, *, now: datetime | None = None) -> tuple[str | None, str | None, datetime | None]:
    """Present only scored, grounded facts, with actionable and current urgency."""
    source = dict(row)
    if source.get("scoring_status") != "scored":
        return None, None, None
    positive = grounded_annotation(source.get("highlight"), EventHighlight, source)
    action = grounded_annotation(source.get("urgency_note"), EventUrgencyNote, source)
    now = now or datetime.now(UTC)
    observed = utc_instant(source.get("source_content_checked_at"))
    note = None
    expires_at = None
    if action and observed and timedelta(0) <= now - observed < URGENCY_OBSERVATION_MAX_AGE:
        observation_expiry = observed + URGENCY_OBSERVATION_MAX_AGE
        if action.kind == "deadline":
            if action.deadline_at and action.deadline_at > now:
                note = action.text
                expires_at = min(action.deadline_at, observation_expiry)
        else:
            # This is a source observation, not a guarantee of live remaining seats.
            note = f"At last check: {action.text}"
            expires_at = observation_expiry
    return positive.text if positive else None, note, expires_at
