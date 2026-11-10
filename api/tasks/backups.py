from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

BACKUP_PREFIX = "events-"
BACKUP_SUFFIX = ".db"
BACKUP_TIMESTAMP = "%Y%m%d-%H%M%S"
BACKUP_NAME_RE = re.compile(
    rf"^{re.escape(BACKUP_PREFIX)}(?P<timestamp>\d{{8}}-\d{{6}})(?:-\d+)?{re.escape(BACKUP_SUFFIX)}$"
)


@dataclass(frozen=True)
class BackupResult:
    path: Path
    pruned: tuple[Path, ...] = ()
    mirror_path: Path | None = None


def _backup_timestamp(path: Path) -> datetime | None:
    match = BACKUP_NAME_RE.match(path.name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group("timestamp"), BACKUP_TIMESTAMP).replace(tzinfo=UTC)
    except ValueError:
        return None


def _backup_destination(backup_dir: Path, timestamp: str) -> Path:
    destination = backup_dir / f"{BACKUP_PREFIX}{timestamp}{BACKUP_SUFFIX}"
    if not destination.exists():
        return destination
    index = 1
    while True:
        candidate = backup_dir / f"{BACKUP_PREFIX}{timestamp}-{index}{BACKUP_SUFFIX}"
        if not candidate.exists():
            return candidate
        index += 1


def _temporary_backup_path(directory: Path, destination: Path) -> Path:
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=directory,
        delete=False,
    ) as handle:
        return Path(handle.name)


def _verify_backup(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if result is None or result[0] != "ok":
        raise RuntimeError(f"backup failed integrity check: {path}")


def backup_sqlite_database(
    *,
    source: Path,
    backup_dir: Path,
    retention_days: int = 30,
    now: datetime | None = None,
    mirror_dir: Path | None = None,
) -> BackupResult:
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    if not source.exists():
        raise FileNotFoundError(source)

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = _backup_destination(backup_dir, now.astimezone(UTC).strftime(BACKUP_TIMESTAMP))

    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    temp_path = _temporary_backup_path(backup_dir, destination)
    try:
        src = sqlite3.connect(source_uri, uri=True)
        dest = sqlite3.connect(temp_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        _verify_backup(temp_path)
        temp_path.replace(destination)
    except Exception:
        with suppress(FileNotFoundError):
            temp_path.unlink()
        raise

    cutoff = now.astimezone(UTC) - timedelta(days=retention_days)
    pruned: list[Path] = []
    for candidate in backup_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"):
        if candidate == destination:
            continue
        timestamp = _backup_timestamp(candidate)
        if timestamp is not None and timestamp < cutoff:
            candidate.unlink()
            pruned.append(candidate)

    mirror_path = None
    if mirror_dir is not None:
        mirror_dir.mkdir(parents=True, exist_ok=True)
        mirror_path = mirror_dir / destination.name
        mirror_temp = _temporary_backup_path(mirror_dir, mirror_path)
        try:
            shutil.copy2(destination, mirror_temp)
            _verify_backup(mirror_temp)
            mirror_temp.replace(mirror_path)
        except Exception:
            with suppress(FileNotFoundError):
                mirror_temp.unlink()
            raise

    return BackupResult(path=destination, pruned=tuple(pruned), mirror_path=mirror_path)
