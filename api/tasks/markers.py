"""Marker files in data/ that fence the runner off from re-firing into a fatal state.

When a 404 comes back from Discord (webhook revoked) or 3 consecutive cycles
fail to score any event, write a marker. The runner checks markers at the
top of every cycle and exits early until the human clears the marker.

Exception — the SCORING_DISABLED marker auto-expires after a cooldown window
(default 6h, override via ``SCOUT_BREAKER_COOLDOWN_HOURS``). A transient
5-hour rate-limit window used to trip the breaker permanently; now the cycle
gets a self-healing retry after the cooldown elapses. WEBHOOK_DISABLED stays
manual-only — a revoked webhook is operator-cleared, not time-cleared.
"""
from __future__ import annotations

import enum
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Marker(enum.Enum):
    WEBHOOK_DISABLED = ".webhook_disabled"
    SCORING_DISABLED = ".scoring_disabled"
    # Scout-bot circuit breaker (Task 4). Lets Yang kill the bot service
    # without unloading the launchd plist: the bot checks this marker at
    # startup BEFORE registering slash commands, so setting it and
    # restarting brings the process up in "exit cleanly, no commands"
    # mode. Independent of WEBHOOK_DISABLED — the bot exposes /today and
    # /week (read-only) plus /scout retract (PATCH-only), all of which
    # are distinct from the cycle's post-to-#scout webhook path.
    BOT_DISABLED = ".bot_disabled"


# Markers eligible for time-based auto-expiry. WEBHOOK_DISABLED is intentionally
# excluded — a revoked Discord webhook stays revoked until an operator rotates
# it. Only SCORING_DISABLED has a transient external cause (5h CLI rate-limit).
_COOLDOWN_MARKERS: frozenset[Marker] = frozenset({Marker.SCORING_DISABLED})
_DEFAULT_COOLDOWN_HOURS = 6.0

log = logging.getLogger(__name__)


def _path(data_dir: Path, marker: Marker) -> Path:
    return data_dir / marker.value


def _use_db(conn: Any | None) -> bool:
    return getattr(conn, "dialect", "sqlite") == "postgres"


def _cooldown_seconds() -> float:
    raw = os.environ.get("SCOUT_BREAKER_COOLDOWN_HOURS")
    if raw is None or raw.strip() == "":
        return _DEFAULT_COOLDOWN_HOURS * 3600.0
    try:
        hours = float(raw)
    except ValueError:
        log.warning(
            "invalid SCOUT_BREAKER_COOLDOWN_HOURS=%r; using default %.1fh",
            raw, _DEFAULT_COOLDOWN_HOURS,
        )
        return _DEFAULT_COOLDOWN_HOURS * 3600.0
    if hours <= 0:
        return _DEFAULT_COOLDOWN_HOURS * 3600.0
    return hours * 3600.0


def write_marker(data_dir: Path, marker: Marker, reason: str, conn: Any | None = None) -> None:
    if _use_db(conn):
        from api.repositories import runtime_markers as storage

        storage.write_runtime_marker(conn, marker.value, reason)
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    _path(data_dir, marker).write_text(f"{ts}\n{reason}\n")


def _marker_age_seconds(data_dir: Path, marker: Marker, conn: Any | None) -> float | None:
    """Return seconds since the marker was last written, or None if missing."""
    if _use_db(conn):
        from api.repositories import runtime_markers as storage

        ts = storage.runtime_marker_updated_at(conn, marker.value)
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return (datetime.now(UTC) - ts).total_seconds()
    p = _path(data_dir, marker)
    if not p.exists():
        return None
    return max(0.0, datetime.now(UTC).timestamp() - p.stat().st_mtime)


def is_cooldown_expired(data_dir: Path, marker: Marker, conn: Any | None = None) -> bool:
    """True iff the marker is on the cooldown list AND its age exceeds the
    cooldown window. Returns False for markers without cooldown support and
    for absent markers."""
    if marker not in _COOLDOWN_MARKERS:
        return False
    age = _marker_age_seconds(data_dir, marker, conn)
    if age is None:
        return False
    return age >= _cooldown_seconds()


def is_marked(data_dir: Path, marker: Marker, conn: Any | None = None) -> bool:
    """Return True if the marker is present AND (for cooldown-eligible
    markers) the cooldown window has not elapsed."""
    if _use_db(conn):
        from api.repositories import runtime_markers as storage

        if not storage.is_runtime_marked(conn, marker.value):
            return False
    else:
        if not _path(data_dir, marker).exists():
            return False

    if marker in _COOLDOWN_MARKERS and is_cooldown_expired(data_dir, marker, conn):
        log.info(
            "marker %s exists but cooldown elapsed; treating as not-marked",
            marker.value,
        )
        return False
    return True


def touch_marker(data_dir: Path, marker: Marker, reason: str, conn: Any | None = None) -> None:
    """Reset the cooldown clock on an existing marker. Used when the
    auto-retry after cooldown also failed: we keep the marker but bump
    its mtime so the cooldown window restarts."""
    write_marker(data_dir, marker, reason, conn=conn)


def clear_marker(data_dir: Path, marker: Marker, conn: Any | None = None) -> None:
    if _use_db(conn):
        from api.repositories import runtime_markers as storage

        storage.clear_runtime_marker(conn, marker.value)
        return
    p = _path(data_dir, marker)
    if p.exists():
        p.unlink()
