"""Smoke: bot exits cleanly when BOT_DISABLED marker is set.

The launchd plist for the scout-bot (Task 7) uses ``KeepAlive`` so a
crashing process gets restarted. Yang's escape hatch is the
``Marker.BOT_DISABLED`` file — setting it must cause ``run_bot`` to
return normally without registering any commands, so the keep-alive
policy treats the exit as intentional.

We assert two things:

1. With the marker set, the client factory is NEVER called (no
   discord.py connection, no command registration).
2. Without the marker, the client factory IS called exactly once
   (regression guard for an accidental short-circuit elsewhere).
"""
from __future__ import annotations

from pathlib import Path

from api.integrations.discord.bot import (
    BotConfig,
    FriendAllowlist,
    run_bot,
)
from api.tasks.markers import Marker, write_marker


def _make_cfg(tmp_path: Path) -> BotConfig:
    db = tmp_path / "events.db"
    db.touch()
    return BotConfig(
        data_dir=tmp_path,
        db_path=db,
        bot_token="not-a-real-token",
        webhook_url="https://discord.com/api/webhooks/123/abc",
        admin_discord_ids=frozenset({"42"}),
        friend_allowlist=FriendAllowlist(by_discord_id={}),
    )


def test_bot_exits_cleanly_when_marker_set(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    write_marker(cfg.data_dir, Marker.BOT_DISABLED, "test")

    called = {"count": 0}

    def _factory(_cfg: BotConfig):
        called["count"] += 1
        return None

    # Must return without raising; must NOT invoke the client factory.
    run_bot(cfg, client_factory=_factory)
    assert called["count"] == 0


def test_bot_invokes_factory_when_marker_absent(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)

    called = {"count": 0}

    def _factory(_cfg: BotConfig):
        called["count"] += 1
        return None  # no coroutine — skip the asyncio.run path

    run_bot(cfg, client_factory=_factory)
    assert called["count"] == 1


def test_bot_creates_log_directory(tmp_path: Path) -> None:
    """configure_bot_logging runs even when the marker fires."""
    cfg = _make_cfg(tmp_path)
    write_marker(cfg.data_dir, Marker.BOT_DISABLED, "test")
    run_bot(cfg, client_factory=lambda _c: None)
    assert (tmp_path / "logs").exists()
