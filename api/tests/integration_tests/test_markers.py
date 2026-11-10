from __future__ import annotations

import os
from pathlib import Path

import pytest

from api.tasks.markers import (
    Marker,
    clear_marker,
    is_cooldown_expired,
    is_marked,
    touch_marker,
    write_marker,
)


def test_write_creates_file(tmp_path: Path):
    write_marker(tmp_path, Marker.WEBHOOK_DISABLED, "404 from Discord")
    assert (tmp_path / ".webhook_disabled").exists()


def test_is_marked_true_after_write(tmp_path: Path):
    write_marker(tmp_path, Marker.SCORING_DISABLED, "3 all-failed cycles")
    assert is_marked(tmp_path, Marker.SCORING_DISABLED) is True


def test_is_marked_false_when_absent(tmp_path: Path):
    assert is_marked(tmp_path, Marker.WEBHOOK_DISABLED) is False


def test_clear_marker_removes(tmp_path: Path):
    write_marker(tmp_path, Marker.WEBHOOK_DISABLED, "x")
    clear_marker(tmp_path, Marker.WEBHOOK_DISABLED)
    assert not (tmp_path / ".webhook_disabled").exists()


def test_marker_records_reason_and_timestamp(tmp_path: Path):
    write_marker(tmp_path, Marker.WEBHOOK_DISABLED, "404")
    text = (tmp_path / ".webhook_disabled").read_text()
    assert "404" in text
    # ISO8601-ish timestamp present
    assert "T" in text or "Z" in text or "-" in text


# ---- Auto-cooldown for SCORING_DISABLED ----
#
# The 5-hour claude CLI rate-limit used to trip the breaker permanently
# because `.scoring_disabled` had no time-based clear. These tests pin the
# self-healing behavior: fresh marker = blocked, stale marker = treated as
# expired, but only for SCORING_DISABLED — WEBHOOK_DISABLED stays manual.


def _age_marker(path: Path, hours: float) -> None:
    """Backdate the marker's mtime by ``hours`` for cooldown tests."""
    target = path.stat().st_mtime - (hours * 3600.0)
    os.utime(path, (target, target))


def test_scoring_disabled_marker_blocks_when_fresh(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SCOUT_BREAKER_COOLDOWN_HOURS", "6")
    write_marker(tmp_path, Marker.SCORING_DISABLED, "fresh trip")
    assert is_marked(tmp_path, Marker.SCORING_DISABLED) is True
    assert is_cooldown_expired(tmp_path, Marker.SCORING_DISABLED) is False


def test_scoring_disabled_marker_expires_after_cooldown(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SCOUT_BREAKER_COOLDOWN_HOURS", "6")
    write_marker(tmp_path, Marker.SCORING_DISABLED, "old trip")
    _age_marker(tmp_path / Marker.SCORING_DISABLED.value, hours=7.0)
    assert is_cooldown_expired(tmp_path, Marker.SCORING_DISABLED) is True
    # is_marked treats expired markers as not-marked so the cycle can retry.
    assert is_marked(tmp_path, Marker.SCORING_DISABLED) is False
    # The file itself remains until the cycle reconciles success/failure.
    assert (tmp_path / Marker.SCORING_DISABLED.value).exists()


def test_webhook_disabled_marker_never_expires(tmp_path: Path, monkeypatch):
    """A revoked Discord webhook stays revoked until an operator rotates it.
    Cooldown is intentionally NOT applied to WEBHOOK_DISABLED."""
    monkeypatch.setenv("SCOUT_BREAKER_COOLDOWN_HOURS", "6")
    write_marker(tmp_path, Marker.WEBHOOK_DISABLED, "404")
    _age_marker(tmp_path / Marker.WEBHOOK_DISABLED.value, hours=999.0)
    assert is_cooldown_expired(tmp_path, Marker.WEBHOOK_DISABLED) is False
    assert is_marked(tmp_path, Marker.WEBHOOK_DISABLED) is True


def test_cooldown_env_override_changes_window(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SCOUT_BREAKER_COOLDOWN_HOURS", "1")
    write_marker(tmp_path, Marker.SCORING_DISABLED, "x")
    _age_marker(tmp_path / Marker.SCORING_DISABLED.value, hours=2.0)
    assert is_marked(tmp_path, Marker.SCORING_DISABLED) is False
    # And the opposite: a longer override holds the breaker open.
    monkeypatch.setenv("SCOUT_BREAKER_COOLDOWN_HOURS", "24")
    assert is_marked(tmp_path, Marker.SCORING_DISABLED) is True


def test_cooldown_invalid_env_falls_back_to_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SCOUT_BREAKER_COOLDOWN_HOURS", "not-a-number")
    write_marker(tmp_path, Marker.SCORING_DISABLED, "x")
    _age_marker(tmp_path / Marker.SCORING_DISABLED.value, hours=7.0)
    # 7h > default 6h means expired even with garbage env value.
    assert is_marked(tmp_path, Marker.SCORING_DISABLED) is False


def test_touch_marker_resets_cooldown(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SCOUT_BREAKER_COOLDOWN_HOURS", "6")
    write_marker(tmp_path, Marker.SCORING_DISABLED, "first trip")
    _age_marker(tmp_path / Marker.SCORING_DISABLED.value, hours=7.0)
    assert is_cooldown_expired(tmp_path, Marker.SCORING_DISABLED) is True

    touch_marker(tmp_path, Marker.SCORING_DISABLED, "retry failed; restart cooldown")
    assert is_cooldown_expired(tmp_path, Marker.SCORING_DISABLED) is False
    assert is_marked(tmp_path, Marker.SCORING_DISABLED) is True


@pytest.mark.parametrize("marker", list(Marker))
def test_is_cooldown_expired_false_when_absent(marker: Marker, tmp_path: Path):
    assert is_cooldown_expired(tmp_path, marker) is False
