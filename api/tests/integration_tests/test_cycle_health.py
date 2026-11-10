from pathlib import Path

from api.tasks.cleanup import (
    CycleHealth,
    load_health,
    record_all_failed,
    record_partial_or_success,
    save_health,
)


def test_load_health_initializes_when_missing(tmp_path: Path):
    h = load_health(tmp_path)
    assert h.consecutive_all_failed == 0
    assert h.last_error is None


def test_record_all_failed_increments(tmp_path: Path):
    h = load_health(tmp_path)
    h = record_all_failed(h, "timeout")
    assert h.consecutive_all_failed == 1
    h = record_all_failed(h, "timeout")
    assert h.consecutive_all_failed == 2
    assert h.last_error == "timeout"


def test_record_partial_resets_to_zero(tmp_path: Path):
    h = CycleHealth(consecutive_all_failed=2, last_error="x",
                    last_failed_at="2026-04-25T22:00:00Z", last_success_at=None)
    h = record_partial_or_success(h)
    assert h.consecutive_all_failed == 0
    assert h.last_success_at is not None


def test_save_and_reload_roundtrip(tmp_path: Path):
    h = load_health(tmp_path)
    h = record_all_failed(h, "boom")
    save_health(tmp_path, h)
    loaded = load_health(tmp_path)
    assert loaded.consecutive_all_failed == 1
    assert loaded.last_error == "boom"


def test_save_health_zero_overwrites_existing_count(tmp_path: Path):
    """The reset-breaker CLI and the marker auto-cooldown path both rely on
    saving a fresh ``CycleHealth()`` to zero the counter. Pin the round-trip."""
    h = record_all_failed(load_health(tmp_path), "boom")
    h = record_all_failed(h, "boom2")
    save_health(tmp_path, h)
    assert load_health(tmp_path).consecutive_all_failed == 2

    save_health(tmp_path, CycleHealth())
    reloaded = load_health(tmp_path)
    assert reloaded.consecutive_all_failed == 0
    assert reloaded.last_error is None
