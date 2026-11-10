"""Cycle health tracker — counts consecutive all-failed scoring cycles.

After 3 strikes the cycle command writes the .scoring_disabled marker so
launchd doesn't keep re-firing into the same fatal state.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HEALTH_FILE = ".cycle_health.json"


@dataclass
class CycleHealth:
    consecutive_all_failed: int = 0
    last_error: str | None = None
    last_failed_at: str | None = None
    last_success_at: str | None = None


def _use_db(conn: Any | None) -> bool:
    return getattr(conn, "dialect", "sqlite") == "postgres"


def load_health(data_dir: Path, conn: Any | None = None) -> CycleHealth:
    if _use_db(conn):
        from api.repositories import runtime_markers as storage

        return CycleHealth(**storage.load_cycle_health_row(conn))
    path = data_dir / HEALTH_FILE
    if not path.exists():
        return CycleHealth()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return CycleHealth()
    return CycleHealth(**{k: data.get(k) for k in CycleHealth.__dataclass_fields__})


def save_health(data_dir: Path, h: CycleHealth, conn: Any | None = None) -> None:
    if _use_db(conn):
        from api.repositories import runtime_markers as storage

        storage.save_cycle_health_row(conn, asdict(h))
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / HEALTH_FILE).write_text(json.dumps(asdict(h), indent=2))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def record_all_failed(h: CycleHealth, error: str) -> CycleHealth:
    return CycleHealth(
        consecutive_all_failed=h.consecutive_all_failed + 1,
        last_error=error,
        last_failed_at=_now(),
        last_success_at=h.last_success_at,
    )


def record_partial_or_success(h: CycleHealth) -> CycleHealth:
    return CycleHealth(
        consecutive_all_failed=0,
        last_error=h.last_error,
        last_failed_at=h.last_failed_at,
        last_success_at=_now(),
    )
