"""Bounded source refresh on the existing worker; no network-held DB transaction."""
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from api.integrations.registration import (
    fetch_registration_availability,
    supported_registration_url,
)
from api.repositories.availability import availability_candidates, record_availability


@dataclass
class AvailabilityRefresh:
    attempted: int = 0
    confirmed: int = 0


def refresh_availability(connection: Callable[[], AbstractContextManager[Any]], *, limit: int = 50,
                         client: httpx.Client | None = None) -> AvailabilityRefresh:
    """Refresh eligible source facts without holding a transaction during requests."""
    today = datetime.now(ZoneInfo("America/New_York")).replace(hour=0, minute=0, second=0, microsecond=0)
    with connection() as conn:
        candidates = availability_candidates(conn, today.astimezone(UTC), limit=limit)
    result = AvailabilityRefresh()
    # Reuse one short-lived client for the batch. It has no user credentials.
    context = nullcontext(client) if client is not None else httpx.Client(
        headers={"User-Agent": "EventHunt/0.3 public-event-availability"}, trust_env=False,
    )
    with context as source_client:
        for candidate in candidates:
            if not supported_registration_url(candidate.url):
                continue
            observation = fetch_registration_availability(candidate.url, client=source_client)
            result.attempted += 1
            with connection() as conn:
                changed = [record_availability(conn, event_id, observation, expected_start=candidate.starts_at)
                           for event_id in candidate.event_ids]
            result.confirmed += int(any(changed))
    return result
