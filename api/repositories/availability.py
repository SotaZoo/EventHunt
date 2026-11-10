"""Persist verified availability and bounded refresh attempts."""
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from api.models.availability import TERMINAL_AVAILABILITY, AvailabilityObservation
from api.models.events import RawEvent
from api.models.occurrences import (
    occurrence_groups,
    provider_identity,
    supported_registration_url,
    utc_instant,
)
from api.repositories.catalog import catalog_rows, excluded_source_identities


def record_availability(conn: Any, event_id: int, observation: AvailabilityObservation,
                        *, expected_start: datetime | None = None) -> bool:
    """Record a current source fact without erasing unknown or terminal evidence."""
    row = conn.execute(
        "SELECT registration_availability, availability_checked_at, start_at FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if row is None or row["registration_availability"] in TERMINAL_AVAILABILITY:
        return False
    if expected_start is not None and utc_instant(row["start_at"]) != expected_start:
        return False
    checked = observation.checked_at.astimezone(UTC)
    previous = utc_instant(row["availability_checked_at"])
    if previous is not None and checked < previous:
        return False
    timestamp = checked.isoformat(timespec="seconds")
    if observation.state == "unknown":
        conn.execute("UPDATE events SET availability_attempted_at = ? WHERE id = ?", (timestamp, event_id))
        return False
    if not observation.evidence:
        raise ValueError("confirmed availability requires source evidence")
    conn.execute(
        """UPDATE events SET registration_availability = ?, availability_evidence = ?,
            availability_checked_at = ?, availability_attempted_at = ?,
            cancelled_at = CASE WHEN ? = 'canceled' THEN COALESCE(cancelled_at, ?) ELSE cancelled_at END
            WHERE id = ?""",
        (observation.state, observation.evidence[:240], timestamp, timestamp,
         observation.state, timestamp, event_id),
    )
    return True


def record_ingested_availability(conn: Any, event_id: int, raw: RawEvent) -> None:
    """Apply source observations while preserving closure for the same occurrence."""
    # A time correction/reschedule is not evidence of a new occurrence.
    # Newly identified listings are inserted with the migration's unknown default.
    if raw.registration_availability != "unknown":
        record_availability(conn, event_id, AvailabilityObservation(
            state=raw.registration_availability, evidence=raw.availability_evidence,
            checked_at=raw.availability_checked_at or datetime.now(UTC),
        ))


@dataclass(frozen=True)
class AvailabilityCandidate:
    url: str
    event_ids: tuple[int, ...]
    starts_at: datetime
    attempted_at: datetime | None
    waitlist: bool


def availability_candidates(conn: Any, since: datetime, *, limit: int = 50) -> list[AvailabilityCandidate]:
    """Choose stale nonterminal source listings, collapsing provider aliases first."""
    candidates = []
    rows = catalog_rows(conn)
    excluded = excluded_source_identities(rows)
    for group in occurrence_groups(rows):
        if any(row["cancelled_at"] or row["registration_availability"] == "canceled" for row in group):
            continue
        sources: dict[tuple[str, str], list[dict]] = {}
        for row in group:
            sources.setdefault(provider_identity(row["url"]), []).append(row)
        for identity, aliases in sources.items():
            if identity in excluded:
                continue
            fetchable = [row for row in aliases if supported_registration_url(row["url"])]
            if not fetchable:
                continue
            preferred = max(fetchable, key=lambda row: (
                row["registration_availability"] == "waitlist", str(row["last_seen"]), row["id"],
            ))
            starts_at = utc_instant(preferred["start_at"])
            if starts_at is None or starts_at < since:
                continue
            attempts = [utc_instant(row["availability_attempted_at"]) for row in aliases]
            candidates.append(AvailabilityCandidate(
                url=preferred["url"], event_ids=tuple(row["id"] for row in aliases),
                starts_at=starts_at,
                attempted_at=min(attempts) if all(attempt is not None for attempt in attempts) else None,
                waitlist=any(row["registration_availability"] == "waitlist" for row in aliases),
            ))
    oldest = datetime.min.replace(tzinfo=UTC)
    return sorted(candidates, key=lambda item: (item.attempted_at or oldest, not item.waitlist, item.event_ids))[:max(0, limit)]


def terminal_source_identities(conn: Any) -> frozenset[tuple[str, str]]:
    """Avoid detail-page revisits, including aliases of a canceled occurrence."""
    return excluded_source_identities(catalog_rows(conn))
