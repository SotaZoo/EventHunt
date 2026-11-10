"""End-to-end tests for ``/scout link``.

Asserts the friend-onboarding round-trip against a real migrated SQLite
DB (per api/tests/AGENTS.md — integration tests hit a real DB via
``migrated_db()`` style fixtures).

The handler shape is async; we drive it from a small sync wrapper that
uses ``asyncio.run`` to keep each test self-contained.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from api.integrations.discord.bot import (
    SCOUT_LINK_OK,
    SCOUT_LINK_REJECT,
    handle_scout_link,
    parse_friend_allowlist,
)
from api.repositories._storage import (
    connect,
    create_user,
)
from api.repositories.migrate import apply_pending

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"


@dataclass
class FakeUser:
    id: int


@dataclass
class FakeResponse:
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


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "events.db"
    conn = connect(db)
    apply_pending(conn, MIG_DIR)
    create_user(conn, email="alice@example.com", display_name="Alice", invite_code="a1")
    create_user(conn, email="bob@example.com", display_name="Bob", invite_code="b1")
    conn.close()
    return db


def _factory(path: Path):
    def _open():
        return connect(path)
    return _open


def _run(coro):
    """Single sync entry to drive an async handler from a test fn."""
    return asyncio.new_event_loop().run_until_complete(coro)


def test_link_writes_discord_user_id_for_matching_friend(db_path: Path) -> None:
    allow = parse_friend_allowlist("100:alice@example.com,200:bob@example.com")
    interaction = FakeInteraction(user=FakeUser(id=100))

    _run(handle_scout_link(
        interaction,
        db_factory=_factory(db_path),
        friend_allowlist=allow,
    ))

    assert interaction.response.calls[0]["content"] == SCOUT_LINK_OK
    conn = connect(db_path)
    try:
        alice = conn.execute(
            "SELECT discord_user_id FROM users WHERE email = ?",
            ("alice@example.com",),
        ).fetchone()
        bob = conn.execute(
            "SELECT discord_user_id FROM users WHERE email = ?",
            ("bob@example.com",),
        ).fetchone()
    finally:
        conn.close()
    assert alice["discord_user_id"] == "100"
    assert bob["discord_user_id"] is None  # not linked, must stay null


def test_link_with_unknown_invoker_does_not_write(db_path: Path) -> None:
    allow = parse_friend_allowlist("100:alice@example.com")
    interaction = FakeInteraction(user=FakeUser(id=987654))  # not on allowlist

    _run(handle_scout_link(
        interaction,
        db_factory=_factory(db_path),
        friend_allowlist=allow,
    ))

    assert interaction.response.calls[0]["content"] == SCOUT_LINK_REJECT
    conn = connect(db_path)
    try:
        alice = conn.execute(
            "SELECT discord_user_id FROM users WHERE email = ?",
            ("alice@example.com",),
        ).fetchone()
    finally:
        conn.close()
    assert alice["discord_user_id"] is None


def test_link_is_idempotent(db_path: Path) -> None:
    """Second link by the same invoker is a no-op write (same value)."""
    allow = parse_friend_allowlist("100:alice@example.com")
    # First call
    interaction_a = FakeInteraction(user=FakeUser(id=100))
    _run(handle_scout_link(
        interaction_a,
        db_factory=_factory(db_path),
        friend_allowlist=allow,
    ))
    # Second call
    interaction_b = FakeInteraction(user=FakeUser(id=100))
    _run(handle_scout_link(
        interaction_b,
        db_factory=_factory(db_path),
        friend_allowlist=allow,
    ))

    assert interaction_a.response.calls[0]["content"] == SCOUT_LINK_OK
    assert interaction_b.response.calls[0]["content"] == SCOUT_LINK_OK
    conn = connect(db_path)
    try:
        alice = conn.execute(
            "SELECT discord_user_id FROM users WHERE email = ?",
            ("alice@example.com",),
        ).fetchone()
    finally:
        conn.close()
    assert alice["discord_user_id"] == "100"


# ---------------------------------------------------------------------------
# Allowlist parsing edge cases
# ---------------------------------------------------------------------------


def test_parse_friend_allowlist_empty_env_returns_empty_mapping() -> None:
    assert parse_friend_allowlist(None).is_empty is True
    assert parse_friend_allowlist("").is_empty is True
    assert parse_friend_allowlist("   ").is_empty is True


def test_parse_friend_allowlist_skips_malformed_entries() -> None:
    # Each malformed entry exercises a different reject branch:
    #   "bogus"               — no ``:`` separator
    #   ":no-id@example.com"  — blank Discord id
    #   "42:"                 — blank email
    #   "77:not-an-email"     — email missing ``@`` (shape check, I-3)
    #   "88:nodom@"           — email missing domain (shape check, I-3)
    #   "99:nodot@example"    — domain has no dot (shape check, I-3)
    raw = (
        "100:alice@example.com,bogus,:no-id@example.com,42:,"
        "77:not-an-email,88:nodom@,99:nodot@example,200:bob@example.com"
    )
    parsed = parse_friend_allowlist(raw)
    assert parsed.by_discord_id == {
        "100": "alice@example.com",
        "200": "bob@example.com",
    }


# ---------------------------------------------------------------------------
# SCOUT_GUILD_IDS parsing — controls fast vs slow slash command sync
# ---------------------------------------------------------------------------


def test_parse_guild_ids_happy_path() -> None:
    from api.integrations.discord.bot import parse_guild_ids
    assert parse_guild_ids("1497761246513201202") == (1497761246513201202,)
    assert parse_guild_ids("100, 200,300") == (100, 200, 300)


def test_parse_guild_ids_empty_returns_empty_tuple() -> None:
    """Empty env → empty tuple → bot falls back to global sync."""
    from api.integrations.discord.bot import parse_guild_ids
    assert parse_guild_ids("") == ()
    assert parse_guild_ids(None) == ()
    assert parse_guild_ids("   ") == ()
    assert parse_guild_ids(",,,") == ()


def test_parse_guild_ids_rejects_non_integer_fails_loud() -> None:
    """A typo'd guild id should crash at config load, not silently fall
    back to global sync (which would leave the operator wondering why
    their commands aren't showing up for an hour).
    """
    from api.integrations.discord.bot import parse_guild_ids
    with pytest.raises(ValueError):
        parse_guild_ids("123, not-a-number, 456")


def test_load_bot_config_carries_guild_ids_into_bot_config() -> None:
    """Round-trip: the CLI string is parsed and lands on BotConfig.guild_ids
    where the on_ready handler reads it. Failing this means the sync branch
    would silently use global sync regardless of --guild-ids.
    """
    from pathlib import Path

    from api.integrations.discord.bot import load_bot_config

    cfg = load_bot_config(
        data_dir=Path("data"),
        db_path=Path("data/events.db"),
        bot_token="token",  # not validated here
        webhook_url="https://example/webhook",
        admin_discord_ids="",
        friend_allowlist="",
        guild_ids="1497761246513201202",
    )
    assert cfg.guild_ids == (1497761246513201202,)


def test_load_bot_config_empty_guild_ids_defaults_to_empty_tuple() -> None:
    """The default (no env, no flag) must mean global sync — the existing
    fallback that production used before this config knob existed.
    """
    from pathlib import Path

    from api.integrations.discord.bot import load_bot_config

    cfg = load_bot_config(
        data_dir=Path("data"),
        db_path=Path("data/events.db"),
        bot_token="token",
        webhook_url="https://example/webhook",
        admin_discord_ids="",
        friend_allowlist="",
    )
    assert cfg.guild_ids == ()


def test_parse_friend_allowlist_strips_whitespace() -> None:
    raw = "  100 : Alice@Example.COM  ,  200 :   bob@example.com   "
    parsed = parse_friend_allowlist(raw)
    assert parsed.lookup_email("100") == "alice@example.com"
    assert parsed.lookup_email("200") == "bob@example.com"


def test_parse_friend_allowlist_drops_duplicate_discord_ids() -> None:
    """First occurrence wins; later collisions don't silently shadow."""
    raw = "100:alice@example.com,100:bob@example.com"
    parsed = parse_friend_allowlist(raw)
    # Only one mapping for 100; whichever the parser kept.
    assert len(parsed.by_discord_id) == 1


def test_parse_friend_allowlist_drops_duplicate_emails() -> None:
    """Two Discord ids mapping to the same email is a config bug; skip the dup."""
    raw = "100:alice@example.com,200:alice@example.com"
    parsed = parse_friend_allowlist(raw)
    assert len(parsed.by_discord_id) == 1
