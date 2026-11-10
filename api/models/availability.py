"""Source registration facts, never a user's personal registration state."""
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

RegistrationAvailability = Literal["unknown", "open", "waitlist", "full", "closed", "canceled"]
TERMINAL_AVAILABILITY = frozenset({"full", "closed", "canceled"})


@dataclass(frozen=True)
class AvailabilityObservation:
    state: RegistrationAvailability = "unknown"
    # A bounded parser-generated fact, not page HTML or user data.
    evidence: str | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
