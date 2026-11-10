"""Unit tests for the scout-bot slash-command handlers.

The handlers are extracted from discord.py decorators on purpose (see
``api/integrations/discord/bot.py``) so we can drive them with a
lightweight ``FakeInteraction`` — no real Discord gateway connection
needed. This pins the spec §3 behavior:

- /today and /week reply ephemerally with the right window.
- /scout link writes ``discord_user_id`` ONLY for the matching friend.
- /scout retract is admin-gated INSIDE the handler (CRITICAL: spec §3
  says ``MANAGE_MESSAGES`` visibility is informational only).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from api.integrations.discord.bot import (
    SCOUT_LINK_NO_ROW,
    SCOUT_LINK_OK,
    SCOUT_LINK_REJECT,
    SCOUT_RETRACT_DENIED,
    SCOUT_RETRACT_NOT_FOUND,
    SCOUT_RETRACT_NOT_PUSHED,
    SCOUT_RETRACT_OK,
    SCOUT_RETRACT_PATCH_FAILED,
    FriendAllowlist,
    build_retracted_embed,
    handle_scout_link,
    handle_scout_retract,
    handle_today,
    handle_week,
    today_window,
    week_window,
)
from api.integrations.discord.webhook import PostResult, PostStatus
from api.repositories._storage import (
    connect,
    create_user,
)
from api.repositories.migrate import apply_pending

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"


# ---------------------------------------------------------------------------
# Fakes — minimal Discord interaction protocol
# ---------------------------------------------------------------------------


@dataclass
class FakeUser:
    id: int


@dataclass
class FakeResponse:
    """Records calls to ``response.send_message`` for assertions."""
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def send_message(
        self,
        content: str | None = None,
        *,
        embed: dict[str, Any] | None = None,
        embeds: list[dict[str, Any]] | None = None,
        ephemeral: bool = False,
    ) -> None:
        self.calls.append(
            {
                "content": content,
                "embed": embed,
                "embeds": embeds,
                "ephemeral": ephemeral,
            }
        )


@dataclass
class FakeInteraction:
    user: FakeUser
    response: FakeResponse = field(default_factory=FakeResponse)


# ---------------------------------------------------------------------------
# DB fixture + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "events.db"
    conn = connect(db)
    apply_pending(conn, MIG_DIR)
    conn.close()
    return db


def _db_factory(db_path: Path):
    def _open() -> sqlite3.Connection:
        return connect(db_path)
    return _open


def _insert_event(
    db_path: Path,
    *,
    title: str = "AI Event",
    url: str = "https://lu.ma/x",
    start_at: datetime | None = None,
    fit_score: float = 0.92,
    scoring_status: str = "scored",
    discord_push_message_id: str | None = None,
) -> int:
    if start_at is None:
        start_at = datetime.now(UTC) + timedelta(hours=4)
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO events
                (source, url, title, start_at, fit_score, scoring_status,
                 first_seen, last_seen, discord_push_message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "luma", url, title, start_at.isoformat(),
                fit_score, scoring_status, now_iso, now_iso,
                discord_push_message_id,
            ),
        )
        return int(cur.lastrowid)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /today and /week
# ---------------------------------------------------------------------------


async def test_today_returns_events_in_today_window(db_path: Path) -> None:
    """Event later today should appear; event next week must not."""
    later_today = datetime.now(UTC) + timedelta(hours=3)
    next_week = datetime.now(UTC) + timedelta(days=8)
    _insert_event(db_path, title="In window", start_at=later_today, url="https://x/1")
    _insert_event(db_path, title="Out of window", start_at=next_week, url="https://x/2")

    interaction = FakeInteraction(user=FakeUser(id=42))
    await handle_today(interaction, db_factory=_db_factory(db_path))

    assert len(interaction.response.calls) == 1
    call = interaction.response.calls[0]
    assert call["ephemeral"] is True
    # Either one in-window event embed (when "today" is still ahead of
    # midnight) or the "no events today" fallback when it's already
    # past local midnight in NYC. Both branches are valid; we assert
    # the message stays ephemeral and never leaks the out-of-window
    # event title.
    # Production sends ``discord.Embed`` objects to ``interaction.response``,
    # NOT the raw dicts that webhook flows use. The FakeInteraction records
    # whatever ``send_message`` was called with, so we assert on the Embed
    # object's ``.title`` attribute. (Live QA found that dicts here cause
    # ``AttributeError: 'dict' object has no attribute 'to_dict'`` —
    # tightening this assertion prevents that regression.)
    embed_titles: list[str] = []
    if call["embeds"]:
        embed_titles = [e.title for e in call["embeds"]]
    elif call["embed"]:
        embed_titles = [call["embed"].title]
    serialized = " ".join(embed_titles)
    assert "Out of window" not in serialized


async def test_week_returns_events_in_week_window(db_path: Path) -> None:
    in_window = datetime.now(UTC) + timedelta(days=2)
    out_window = datetime.now(UTC) + timedelta(days=10)
    _insert_event(db_path, title="Weekie", start_at=in_window, url="https://x/a")
    _insert_event(db_path, title="Far future", start_at=out_window, url="https://x/b")

    interaction = FakeInteraction(user=FakeUser(id=7))
    await handle_week(interaction, db_factory=_db_factory(db_path))

    assert len(interaction.response.calls) == 1
    call = interaction.response.calls[0]
    assert call["ephemeral"] is True
    assert call["embeds"] is not None
    titles = [e.title for e in call["embeds"]]
    assert any("Weekie" in t for t in titles)
    assert all("Far future" not in t for t in titles)


async def test_today_sends_discord_embed_objects_not_dicts(db_path: Path) -> None:
    """Regression: live QA found that handle_today was passing raw dicts to
    discord.py's ``interaction.response.send_message``, which expects
    ``discord.Embed`` instances and crashed with ``AttributeError: 'dict'
    object has no attribute 'to_dict'``. The unit tests had been passing
    because ``FakeResponse.send_message`` accepted anything.

    This test pins the contract: anything sent must be an Embed.
    """
    import discord

    _insert_event(
        db_path, title="Embed type check",
        start_at=datetime.now(UTC) + timedelta(hours=3),
        url="https://x/typecheck",
    )
    interaction = FakeInteraction(user=FakeUser(id=42))
    await handle_today(interaction, db_factory=_db_factory(db_path))

    call = interaction.response.calls[0]
    sent_embeds = call["embeds"] or ([call["embed"]] if call["embed"] else [])
    assert sent_embeds, "handle_today did not send any embed"
    for embed in sent_embeds:
        assert isinstance(embed, discord.Embed), (
            f"handle_today sent {type(embed).__name__}, expected discord.Embed "
            "— this is the production bug live QA caught"
        )


async def test_week_window_includes_seven_full_days() -> None:
    """Spec §3: /week is ``[now, now+7d]`` — boundaries documented here."""
    fixed = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    start, end = week_window(fixed)
    assert start == fixed
    assert end == fixed + timedelta(days=7)


async def test_week_window_spans_seven_calendar_days_during_edt() -> None:
    """/week is a flat 168-hour rolling window — pin EDT (May) baseline.

    Spec §3 says /week is ``[now, now+7d]``. We deliberately do NOT snap
    to local midnight (unlike /today) because users mean "the next 168
    hours" when they ask. This test pins that semantic so a future
    refactor to "next Sunday midnight NYC" doesn't silently change scope.
    """
    fixed = datetime(2026, 5, 14, 18, 0, tzinfo=UTC)  # mid-May, NYC on EDT
    start, end = week_window(fixed)
    assert (end - start) == timedelta(days=7)
    assert (end - start).total_seconds() == 7 * 24 * 3600


async def test_week_window_survives_spring_forward_correctly() -> None:
    """DST regression: a /week request the day before spring-forward.

    On 2026-03-08 NYC clocks jump 02:00 EST → 03:00 EDT (a 23-hour day).
    A /week starting at 2026-03-07 18:00 UTC should still return exactly
    168 UTC hours later. If a future refactor wall-clock-snaps the upper
    bound, the spring-forward day's "lost hour" would silently shorten
    /week's window to ~167 hours (or shift it).
    """
    fixed = datetime(2026, 3, 7, 18, 0, tzinfo=UTC)  # day before SF
    start, end = week_window(fixed)
    assert end == fixed + timedelta(days=7)
    # Sanity: the 168-hour invariant survives the DST transition.
    assert (end - start).total_seconds() == 7 * 24 * 3600


async def test_week_window_survives_fall_back_correctly() -> None:
    """DST regression: a /week request the day before fall-back.

    On 2026-11-01 NYC clocks fall 02:00 EDT → 01:00 EST (a 25-hour day).
    The 168-hour rolling window must remain 168 hours regardless.
    """
    fixed = datetime(2026, 10, 31, 18, 0, tzinfo=UTC)  # day before FB
    start, end = week_window(fixed)
    assert end == fixed + timedelta(days=7)
    assert (end - start).total_seconds() == 7 * 24 * 3600


async def test_today_window_ends_at_local_nyc_midnight_edt() -> None:
    """Spec §3: /today ends at NYC wall-clock midnight (EDT in May)."""
    # 2026-05-14 18:00 UTC = 14:00 NYC (UTC-4, EDT). Next NYC midnight is
    # 2026-05-15 00:00 NYC == 2026-05-15 04:00 UTC.
    fixed = datetime(2026, 5, 14, 18, 0, tzinfo=UTC)
    start, end = today_window(fixed)
    assert start == fixed
    assert end == datetime(2026, 5, 15, 4, 0, tzinfo=UTC)


async def test_today_window_ends_at_local_nyc_midnight_est() -> None:
    """DST regression: in January NYC is on EST (UTC-5)."""
    # 2026-01-14 18:00 UTC = 13:00 NYC (UTC-5, EST). Next NYC midnight is
    # 2026-01-15 00:00 NYC == 2026-01-15 05:00 UTC.
    fixed = datetime(2026, 1, 14, 18, 0, tzinfo=UTC)
    start, end = today_window(fixed)
    assert start == fixed
    assert end == datetime(2026, 1, 15, 5, 0, tzinfo=UTC)


async def test_today_window_spring_forward_transition() -> None:
    """DST regression: spring-forward (EST→EDT) on 2026-03-08 must resolve cleanly.

    On 2026-03-08, NYC clocks jump from 02:00 EST to 03:00 EDT. The next
    NYC midnight after 2026-03-08 12:00 UTC is 2026-03-09 00:00 EDT,
    which is 2026-03-09 04:00 UTC (UTC-4, post-spring-forward).
    """
    fixed = datetime(2026, 3, 8, 12, 0, tzinfo=UTC)
    start, end = today_window(fixed)
    assert start == fixed
    assert end == datetime(2026, 3, 9, 4, 0, tzinfo=UTC)
    # Sanity: the bound is wall-clock midnight in NYC (00:00 local).
    from zoneinfo import ZoneInfo
    assert end.astimezone(ZoneInfo("America/New_York")).hour == 0


async def test_today_window_fall_back_transition() -> None:
    """DST regression: fall-back (EDT→EST) on 2026-11-01 must resolve cleanly.

    On 2026-11-01, NYC clocks fall from 02:00 EDT to 01:00 EST. The next
    NYC midnight after 2026-11-01 12:00 UTC (still pre-transition window
    in NYC wall-clock terms — but our base instant is post-transition UTC)
    is 2026-11-02 00:00 EST == 2026-11-02 05:00 UTC (UTC-5).
    """
    fixed = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    start, end = today_window(fixed)
    assert start == fixed
    assert end == datetime(2026, 11, 2, 5, 0, tzinfo=UTC)
    from zoneinfo import ZoneInfo
    assert end.astimezone(ZoneInfo("America/New_York")).hour == 0


async def test_today_empty_window_returns_ephemeral_fallback(db_path: Path) -> None:
    """No events in the window must still reply ephemerally (never leak silence to channel)."""
    interaction = FakeInteraction(user=FakeUser(id=42))
    await handle_today(interaction, db_factory=_db_factory(db_path))

    assert len(interaction.response.calls) == 1
    call = interaction.response.calls[0]
    assert call["ephemeral"] is True
    # Either an embed or content; never a non-ephemeral reply.


# ---------------------------------------------------------------------------
# /scout link
# ---------------------------------------------------------------------------


async def test_scout_link_match_writes_discord_user_id(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        create_user(
            conn, email="friend@example.com",
            display_name="Friend", invite_code="abc",
        )
    finally:
        conn.close()

    allowlist = FriendAllowlist(by_discord_id={"99": "friend@example.com"})
    interaction = FakeInteraction(user=FakeUser(id=99))
    await handle_scout_link(
        interaction,
        db_factory=_db_factory(db_path),
        friend_allowlist=allowlist,
    )

    assert len(interaction.response.calls) == 1
    call = interaction.response.calls[0]
    assert call["ephemeral"] is True
    assert call["content"] == SCOUT_LINK_OK

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT discord_user_id FROM users WHERE email = ?",
            ("friend@example.com",),
        ).fetchone()
    finally:
        conn.close()
    assert row["discord_user_id"] == "99"


async def test_scout_link_no_match_rejects_and_does_not_write(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        create_user(
            conn, email="friend@example.com",
            display_name="Friend", invite_code="abc",
        )
    finally:
        conn.close()

    allowlist = FriendAllowlist(by_discord_id={"99": "friend@example.com"})
    interaction = FakeInteraction(user=FakeUser(id=12345))  # NOT on the allowlist
    await handle_scout_link(
        interaction,
        db_factory=_db_factory(db_path),
        friend_allowlist=allowlist,
    )

    assert len(interaction.response.calls) == 1
    call = interaction.response.calls[0]
    assert call["ephemeral"] is True
    assert call["content"] == SCOUT_LINK_REJECT

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT discord_user_id FROM users WHERE email = ?",
            ("friend@example.com",),
        ).fetchone()
    finally:
        conn.close()
    assert row["discord_user_id"] is None


async def test_scout_link_allowlist_entry_without_db_row(db_path: Path) -> None:
    """If the env points at an email with no active user row, reply politely."""
    allowlist = FriendAllowlist(by_discord_id={"99": "ghost@example.com"})
    interaction = FakeInteraction(user=FakeUser(id=99))
    await handle_scout_link(
        interaction,
        db_factory=_db_factory(db_path),
        friend_allowlist=allowlist,
    )

    assert interaction.response.calls[0]["content"] == SCOUT_LINK_NO_ROW


# ---------------------------------------------------------------------------
# /scout retract
# ---------------------------------------------------------------------------


async def test_scout_retract_non_admin_rejected_no_patch(db_path: Path) -> None:
    event_id = _insert_event(db_path, discord_push_message_id="m_1")

    patch_calls: list[tuple[Any, ...]] = []

    def _patch(url: str, mid: str, embed: dict[str, Any]) -> PostResult:
        patch_calls.append((url, mid, embed))
        return PostResult(PostStatus.OK)

    interaction = FakeInteraction(user=FakeUser(id=555))
    await handle_scout_retract(
        interaction,
        event_id,
        db_factory=_db_factory(db_path),
        admin_discord_ids=frozenset({"42"}),  # 555 NOT in admin set
        webhook_url="https://discord.com/api/webhooks/123/abc",
        patch_fn=_patch,
    )

    assert interaction.response.calls[0]["ephemeral"] is True
    assert interaction.response.calls[0]["content"] == SCOUT_RETRACT_DENIED
    assert patch_calls == []  # critical: no PATCH on denial

    # Row must not be marked retracted.
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT scoring_status, scoring_failed_reason FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["scoring_status"] == "scored"
    assert row["scoring_failed_reason"] is None


async def test_scout_retract_admin_patches_and_marks_row(db_path: Path) -> None:
    event_id = _insert_event(
        db_path,
        title="To retract",
        url="https://lu.ma/r1",
        discord_push_message_id="msg_xyz",
    )

    patch_calls: list[tuple[str, str, dict[str, Any]]] = []

    def _patch(url: str, mid: str, embed: dict[str, Any]) -> PostResult:
        patch_calls.append((url, mid, embed))
        return PostResult(PostStatus.OK)

    interaction = FakeInteraction(user=FakeUser(id=42))
    await handle_scout_retract(
        interaction,
        event_id,
        db_factory=_db_factory(db_path),
        admin_discord_ids=frozenset({"42"}),
        webhook_url="https://discord.com/api/webhooks/123/abc",
        patch_fn=_patch,
    )

    assert interaction.response.calls[0]["ephemeral"] is True
    assert interaction.response.calls[0]["content"] == SCOUT_RETRACT_OK
    assert len(patch_calls) == 1
    sent_url, sent_mid, sent_embed = patch_calls[0]
    assert sent_url == "https://discord.com/api/webhooks/123/abc"
    assert sent_mid == "msg_xyz"
    assert sent_embed["title"].startswith("⚡ ~~")
    assert "Retracted" in sent_embed["description"]

    # Row must be marked retracted via scoring_failed_reason reuse.
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT scoring_status, scoring_failed_reason FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["scoring_status"] == "failed"
    assert row["scoring_failed_reason"] == "retracted_by_admin"


async def test_scout_retract_event_not_found(db_path: Path) -> None:
    interaction = FakeInteraction(user=FakeUser(id=42))

    def _patch(*_a, **_kw) -> PostResult:
        raise AssertionError("PATCH must not run when event id is unknown")

    await handle_scout_retract(
        interaction,
        9999,
        db_factory=_db_factory(db_path),
        admin_discord_ids=frozenset({"42"}),
        webhook_url="https://discord.com/api/webhooks/123/abc",
        patch_fn=_patch,
    )
    assert interaction.response.calls[0]["content"] == SCOUT_RETRACT_NOT_FOUND


async def test_scout_retract_event_never_pushed(db_path: Path) -> None:
    """Event with no discord_push_message_id cannot be patched."""
    event_id = _insert_event(db_path, discord_push_message_id=None)
    interaction = FakeInteraction(user=FakeUser(id=42))

    def _patch(*_a, **_kw) -> PostResult:
        raise AssertionError("PATCH must not run when message id is NULL")

    await handle_scout_retract(
        interaction,
        event_id,
        db_factory=_db_factory(db_path),
        admin_discord_ids=frozenset({"42"}),
        webhook_url="https://discord.com/api/webhooks/123/abc",
        patch_fn=_patch,
    )
    assert interaction.response.calls[0]["content"] == SCOUT_RETRACT_NOT_PUSHED


async def test_scout_retract_patch_failure_does_not_mark_row(db_path: Path) -> None:
    """If Discord PATCH fails, don't pretend we retracted — the row stays scored."""
    event_id = _insert_event(db_path, discord_push_message_id="m_99")
    interaction = FakeInteraction(user=FakeUser(id=42))

    def _patch(*_a, **_kw) -> PostResult:
        return PostResult(PostStatus.FATAL, detail="404")

    await handle_scout_retract(
        interaction,
        event_id,
        db_factory=_db_factory(db_path),
        admin_discord_ids=frozenset({"42"}),
        webhook_url="https://discord.com/api/webhooks/123/abc",
        patch_fn=_patch,
    )

    assert interaction.response.calls[0]["content"] == SCOUT_RETRACT_PATCH_FAILED
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT scoring_status FROM events WHERE id = ?", (event_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["scoring_status"] == "scored"


# ---------------------------------------------------------------------------
# build_retracted_embed
# ---------------------------------------------------------------------------


def test_build_retracted_embed_has_strikethrough_title() -> None:
    embed = build_retracted_embed(title="Big event", url="https://lu.ma/big")
    assert embed["title"].startswith("⚡ ~~")
    assert embed["title"].endswith("~~")
    assert "Big event" in embed["title"]
    assert embed["url"] == "https://lu.ma/big"
    # No fields, no friend signal, no metadata.
    assert "fields" not in embed
    assert "Retracted" in embed["description"]
