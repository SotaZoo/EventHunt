"""One short-lived, public-only preview per application process."""
import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.concurrency import run_in_threadpool

from api.services.discovery import EVENT_TIMEZONE


class PublicPreview:
    """Share refresh work without running synchronous catalog queries on the event loop."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._value: dict[str, Any] | None = None
        self._expires_at = 0.0

    async def get(self, load: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        async with self._lock:
            now = datetime.now(UTC).timestamp()
            if self._value is not None and now < self._expires_at:
                return self._value
            value = await run_in_threadpool(load)
            self._value = value
            midnight = datetime.fromisoformat(value["today"]).replace(tzinfo=EVENT_TIMEZONE) + timedelta(days=1)
            boundaries = [now + 30, midnight.timestamp()]
            for event in value["events"]:
                boundaries.append(datetime.fromisoformat(event["startAt"]).timestamp())
                if event["urgencyNoteExpiresAt"]:
                    boundaries.append(datetime.fromisoformat(event["urgencyNoteExpiresAt"]).timestamp())
            self._expires_at = min(boundaries)
            return value
