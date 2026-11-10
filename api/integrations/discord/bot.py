"""Long-running Discord bot exposing scout-bot slash commands.

Operationally SEPARATE from the cycle process — runs as its own
launchd service (plist arrives in Task 7). The cycle handles
scrape-score-push; this bot handles human-driven discovery (`/today`,
`/week`), friend onboarding (`/scout link`), and admin correction
(`/scout retract`).

The bot does NOT listen for reactions of any kind. Post-event ratings
are deferred per spec §7.

CRITICAL authorization note (spec §3): Discord's ``MANAGE_MESSAGES``
permission controls slash-command *visibility* in the client UI, NOT
execution. Every privileged handler re-checks ``interaction.user.id``
against an env-driven allowlist inside the handler. Visibility-based
gating is informational only.

Handlers are exposed here as plain async helpers (``handle_today``,
``handle_week``, ``handle_scout_link``, ``handle_scout_retract``) so the
unit-test suite can drive them with a lightweight ``InteractionLike``
protocol — no real Discord gateway connection is required to assert
behavior.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from api.integrations.discord.webhook import (
    BRAND_COLOR,
    DESC_MAX,
    TITLE_MAX,
    TITLE_PREFIX,
    format_embed,
    patch_event,
)
from api.integrations.llm import NotablePerson
from api.repositories import _storage as storage
from api.tasks.markers import Marker, is_marked

log = logging.getLogger(__name__)

# Eastern (NYC) time for the "midnight tonight" boundary in /today. Spec
# §3: "/today returns events with start_at in [now, midnight tonight]."
# The bot's user is in NYC; the cycle stores start_at as UTC, but the
# day boundary the human cares about is wall-clock midnight in NYC.
# ``zoneinfo.ZoneInfo`` is stdlib (Python 3.9+) and tracks DST
# automatically — both the spring-forward (EST→EDT, UTC-5→UTC-4) and
# fall-back (EDT→EST, UTC-4→UTC-5) transitions resolve correctly without
# any code changes here. The previous fixed-offset shortcut produced
# a 1-hour-too-late window from mid-March to early November.
NYC_TZ = ZoneInfo("America/New_York")

# Filename for the rotating log file. Created under <data_dir>/logs/ by
# ``configure_bot_logging``. Used by Task 7's launchd plist to point
# stdout/stderr at the same directory.
LOG_FILENAME = "scout-bot.log"


# ---------------------------------------------------------------------------
# Allowlist parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FriendAllowlist:
    """Parsed ``SCOUT_FRIEND_ALLOWLIST`` mapping.

    Keys are Discord user ids (str); values are the friend's email
    (already normalized via ``storage.normalize_email``). An empty
    mapping is the safe default — no env, no entries, no matches.
    """

    by_discord_id: Mapping[str, str]

    @property
    def is_empty(self) -> bool:
        return not self.by_discord_id

    def lookup_email(self, discord_user_id: str) -> str | None:
        return self.by_discord_id.get(str(discord_user_id).strip())


def parse_friend_allowlist(raw: str | None) -> FriendAllowlist:
    """Parse ``SCOUT_FRIEND_ALLOWLIST`` env into a structured mapping.

    Format: comma-separated ``<discord_user_id>:<friend_email>`` pairs.
    Whitespace around tokens is stripped. Malformed entries (missing
    ``:`` separator, blank id, blank email, invalid email) are silently
    skipped — the bot must come up even when one entry is fat-fingered
    rather than refusing to register commands. Each parse decision is
    logged at DEBUG with a count only (never the email or id).
    """
    if not raw:
        return FriendAllowlist(by_discord_id={})
    mapping: dict[str, str] = {}
    seen_emails: set[str] = set()
    skipped = 0
    for token in raw.split(","):
        entry = token.strip()
        if not entry:
            continue
        if ":" not in entry:
            skipped += 1
            continue
        did_raw, email_raw = entry.split(":", 1)
        did = did_raw.strip()
        email = email_raw.strip()
        if not did or not email:
            skipped += 1
            continue
        try:
            normalized = storage.normalize_email(email)
        except ValueError:
            skipped += 1
            continue
        if did in mapping or normalized in seen_emails:
            # Duplicate keys would shadow; record and skip. Either side
            # of the pair colliding is a config bug worth surfacing.
            skipped += 1
            continue
        mapping[did] = normalized
        seen_emails.add(normalized)
    if skipped:
        log.debug("scout-bot friend allowlist: skipped %d malformed entries", skipped)
    return FriendAllowlist(by_discord_id=mapping)


def parse_admin_allowlist(raw: str | None) -> frozenset[str]:
    """Parse ``SCOUT_ADMIN_DISCORD_IDS`` env into a frozenset of ids.

    Comma-separated. Whitespace stripped. Empty entries dropped. The
    initial deploy contains just Yang's Discord id; the set type makes
    the membership check O(1) and prevents accidental mutation.
    """
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def parse_guild_ids(raw: str | None) -> tuple[int, ...]:
    """Parse ``SCOUT_GUILD_IDS`` env into a tuple of int guild ids.

    Comma-separated. Whitespace stripped. Empty entries dropped.
    Non-integer entries raise ``ValueError`` — fail loud at startup
    rather than silently dropping a typo'd id that would push the
    bot to use the wrong scope.

    When the result is empty, the bot falls back to global slash
    command sync (up to 1h propagation). When non-empty, the bot
    syncs the command tree to each guild explicitly (instant). The
    Event Bros - NYC friend deploy sets this to a single-element
    tuple so command updates are visible immediately.
    """
    if not raw:
        return ()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return tuple(int(p) for p in parts)


# ---------------------------------------------------------------------------
# Interaction protocol (test seam)
# ---------------------------------------------------------------------------


class _UserLike(Protocol):
    id: int


class InteractionLike(Protocol):
    """Minimum surface the handlers consume.

    Real ``discord.Interaction`` already implements this shape. Tests
    pass a lightweight stand-in that records calls — no real gateway
    connection needed. ``response.send_message`` is async and returns
    None per discord.py's contract.
    """

    user: _UserLike

    @property
    def response(self) -> Any: ...


# ---------------------------------------------------------------------------
# DB executor helper
# ---------------------------------------------------------------------------


# Type alias for any function we run in the executor. Returns whatever
# the wrapped sync DB call returns.
_DbCall = Callable[[], Any]


async def run_db_call(call: _DbCall) -> Any:
    """Run a synchronous DB call on the default thread-pool executor.

    ``_storage.py`` is sync (sqlite3 / SQLAlchemy compat). Slash-command
    handlers are infrequent (human typing rate), so the executor
    overhead is negligible compared to the cost of blocking the asyncio
    loop. Centralized so every handler stays consistent and the
    executor monkeypatch in the unit-test suite has a single seam.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, call)


async def _record_slash_metric(
    *,
    db_factory: Callable[[], Any],
    command: str,
    success: bool,
    event_id: int | None = None,
    extra_kind: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    """Spec §11 telemetry helper for slash-command handlers.

    Always emits a ``slash_command`` row. If ``extra_kind`` is set, also
    emits that row (e.g., ``retract`` carries its own additional payload).
    Never raises into the handler — telemetry must not break a reply.
    The underlying ``write_scout_metric`` already swallows IO errors; the
    extra try/except below is defense-in-depth against the executor layer.
    """
    def _write() -> None:
        conn = db_factory()
        try:
            storage.write_scout_metric(
                conn,
                event_kind="slash_command",
                event_id=event_id,
                payload={"command": command, "success": success},
            )
            if extra_kind is not None:
                storage.write_scout_metric(
                    conn,
                    event_kind=extra_kind,
                    event_id=event_id,
                    payload=extra_payload,
                )
        finally:
            _close_conn(conn)
    try:
        await run_db_call(_write)
    except Exception:
        log.warning("scout-bot slash_command metric write raised")


# ---------------------------------------------------------------------------
# /today and /week — event listing
# ---------------------------------------------------------------------------


def _coerce_datetime(value: str | datetime | None) -> datetime | None:
    """Same boundary as cycle.py's ``_coerce_datetime`` but tolerant of None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_notable_people(raw: str | None) -> list[NotablePerson]:
    """Hydrate the ``notable_people`` TEXT column back into typed models.

    Same parse contract as ``api.tasks.cycle._parse_notable_people``.
    Duplicated rather than imported to avoid pulling cycle.py's transitive
    scraper imports into the bot's startup path; the parse is short and
    unlikely to drift.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[NotablePerson] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(NotablePerson(**entry))
        except (TypeError, ValueError):
            continue
    return out


def today_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """``[now, midnight tonight NYC-local]`` as a UTC datetime tuple.

    Spec §3: /today returns events in ``[now, midnight tonight]``. The
    boundary is wall-clock midnight in NYC since the bot's audience is
    NYC-local. We compute the next NYC midnight, then express both
    bounds as UTC for the storage layer.
    """
    base = now or datetime.now(UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    local = base.astimezone(NYC_TZ)
    next_local_midnight = datetime.combine(
        local.date() + timedelta(days=1), time(0, 0), tzinfo=NYC_TZ,
    )
    return base, next_local_midnight.astimezone(UTC)


def week_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """``[now, now+7d]`` in UTC. Spec §3 /week boundary."""
    base = now or datetime.now(UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    return base, base + timedelta(days=7)


def _row_get(row: Any, key: str) -> Any:
    """Tolerate test fixtures that omit optional columns."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def render_event_embeds(
    rows: list[Any],
    *,
    friends_by_event: Mapping[int, list[str]],
) -> list[dict[str, Any]]:
    """Render rows from ``fetch_events_for_time_window`` into Discord embeds.

    Pure function. Tests feed canned rows + a friends mapping; the bot's
    async handlers call ``compute_friends_going`` in the executor and
    pass the result here.
    """
    embeds: list[dict[str, Any]] = []
    for r in rows:
        start_at = _coerce_datetime(r["start_at"])
        if start_at is None:
            # Defensive — fetch_events_for_time_window filters for non-null
            # but a hand-built fixture might slip through. Skip silently
            # rather than crash the handler.
            continue
        embeds.append(
            format_embed(
                title=r["title"],
                url=r["url"],
                start_at=start_at,
                end_at=_coerce_datetime(_row_get(r, "ends_at")),
                neighborhood=_row_get(r, "neighborhood"),
                capacity_remaining=_row_get(r, "last_known_capacity"),
                why_line=_row_get(r, "urgency_reason"),
                notable_people=_parse_notable_people(_row_get(r, "notable_people")),
                event_type=_row_get(r, "event_type"),
                food_provided=_row_get(r, "food_provided"),
                agenda=None,
                friends_going=friends_by_event.get(int(r["id"]), []),
                cover_image_url=None,
                source=r["source"],
                fit_score=r["fit_score"],
            )
        )
    return embeds


def _empty_window_embed(label: str) -> dict[str, Any]:
    return {
        "title": f"{TITLE_PREFIX} no events {label}",
        "description": "no scored events in window",
        "color": BRAND_COLOR,
    }


def _to_discord_embed(embed_dict: dict[str, Any]) -> Any:
    """Convert a webhook-shaped embed dict to a ``discord.Embed``.

    The scout-bot push path renders to raw dicts (webhook API takes JSON
    directly). The slash-command path replies via ``interaction.response``,
    which requires ``discord.Embed`` instances. This adapter sits at the
    boundary so the renderers in ``api/integrations/discord/webhook.py``
    don't need a hard dependency on discord.py.

    Identity-passthrough for already-Embed inputs lets tests construct
    real ``discord.Embed`` objects when they want stricter assertions.
    """
    import discord
    if isinstance(embed_dict, discord.Embed):
        return embed_dict
    return discord.Embed.from_dict(embed_dict)


async def _send_event_embeds(
    interaction: Any,
    *,
    db_factory: Callable[[], Any],
    window_label: str,
    window: tuple[datetime, datetime],
    limit: int = 3,
) -> None:
    """Shared body of /today and /week.

    ``db_factory`` returns a freshly opened DB connection per call — the
    handler closes it after the executor returns. Keeping the connection
    short-lived avoids long-held sqlite locks while the bot waits on
    Discord's response API.
    """
    start, end = window

    def _query() -> tuple[list[Any], dict[int, list[str]]]:
        conn = db_factory()
        try:
            rows = storage.fetch_events_for_time_window(
                conn, start=start, end=end, limit=limit,
            )
            # TODO(deferred): N+1 query — compute_friends_going is called
            # once per event row. At MVP scale (<=3 events x <=6 friends per
            # /today, /week) this is academic. A batched single-query
            # (``WHERE event_id IN (...)`` joining users) is the proper
            # fix when the friend cohort grows. Tracked under deferred
            # items per spec §7.
            friends_map: dict[int, list[str]] = {
                int(r["id"]): storage.compute_friends_going(conn, int(r["id"]))
                for r in rows
            }
            return rows, friends_map
        finally:
            _close_conn(conn)

    rows, friends = await run_db_call(_query)
    log.info("scout-bot %s: returned_count=%d", window_label, len(rows))

    if not rows:
        embed_dict = _empty_window_embed(window_label)
        await interaction.response.send_message(
            embed=_to_discord_embed(embed_dict), ephemeral=True,
        )
        return

    embed_dicts = render_event_embeds(rows, friends_by_event=friends)
    # Discord accepts up to 10 embeds per message; we only ever send 1-3.
    # Convert raw dicts (the webhook flow's native shape) to discord.Embed
    # objects which is what discord.py's interaction API requires.
    await interaction.response.send_message(
        embeds=[_to_discord_embed(d) for d in embed_dicts], ephemeral=True,
    )


async def handle_today(
    interaction: Any,
    *,
    db_factory: Callable[[], Any],
    now: datetime | None = None,
) -> None:
    """Slash-command handler for ``/today``. Ephemeral reply."""
    success = False
    try:
        await _send_event_embeds(
            interaction,
            db_factory=db_factory,
            window_label="today",
            window=today_window(now),
        )
        success = True
    finally:
        await _record_slash_metric(
            db_factory=db_factory, command="today", success=success,
        )


async def handle_week(
    interaction: Any,
    *,
    db_factory: Callable[[], Any],
    now: datetime | None = None,
) -> None:
    """Slash-command handler for ``/week``. Ephemeral reply."""
    success = False
    try:
        await _send_event_embeds(
            interaction,
            db_factory=db_factory,
            window_label="week",
            window=week_window(now),
        )
        success = True
    finally:
        await _record_slash_metric(
            db_factory=db_factory, command="week", success=success,
        )


# ---------------------------------------------------------------------------
# /scout link — friend onboarding
# ---------------------------------------------------------------------------


SCOUT_LINK_REJECT = (
    "You're not on the scout-bot allowlist. Ping Yang if this is a mistake."
)
SCOUT_LINK_OK = "Linked! You'll see friend signals on your events."
SCOUT_LINK_NO_ROW = (
    "Your allowlist entry isn't backed by an active user row. Ping Yang."
)
SCOUT_LINK_COLLISION = (
    "That Discord account is already linked to a different friend. Ping Yang."
)


async def handle_scout_link(
    interaction: Any,
    *,
    db_factory: Callable[[], Any],
    friend_allowlist: FriendAllowlist,
) -> None:
    """Handler for ``/scout link``: friend onboarding.

    Matches the invoker's Discord id against the env-driven allowlist.
    On match, writes ``users.discord_user_id`` for the friend's row.
    Replies ephemerally either way so a mistaken invocation doesn't
    leak the rejection message into the channel.
    """
    success = False
    try:
        discord_id = str(interaction.user.id)
        email = friend_allowlist.lookup_email(discord_id)
        if email is None:
            # Aggregate-only log: never the Discord id verbatim per CLAUDE.md.
            log.info("scout-bot /scout link: rejected (not on allowlist)")
            await interaction.response.send_message(
                SCOUT_LINK_REJECT, ephemeral=True,
            )
            return

        def _link() -> tuple[int, str | None]:
            conn = db_factory()
            try:
                try:
                    rowcount = storage.link_discord_user(
                        conn, email=email, discord_user_id=discord_id,
                    )
                except sqlite3.IntegrityError:
                    # Unique partial index from migration 0006 fired — this
                    # Discord id is already mapped to a different user.
                    return 0, "collision"
                return rowcount, None
            finally:
                _close_conn(conn)

        rowcount, err = await run_db_call(_link)
        if err == "collision":
            log.info("scout-bot /scout link: collision on discord_user_id")
            await interaction.response.send_message(
                SCOUT_LINK_COLLISION, ephemeral=True,
            )
            return
        if rowcount == 0:
            log.info("scout-bot /scout link: allowlist entry has no active user row")
            await interaction.response.send_message(
                SCOUT_LINK_NO_ROW, ephemeral=True,
            )
            return
        log.info("scout-bot /scout link: linked rows=%d", rowcount)
        await interaction.response.send_message(SCOUT_LINK_OK, ephemeral=True)
        success = True
    finally:
        await _record_slash_metric(
            db_factory=db_factory, command="scout_link", success=success,
        )


# ---------------------------------------------------------------------------
# /scout retract — admin-only retraction
# ---------------------------------------------------------------------------


SCOUT_RETRACT_DENIED = (
    "You don't have permission to retract scout-bot pushes."
)
SCOUT_RETRACT_NOT_FOUND = "Event id not found."
SCOUT_RETRACT_NOT_PUSHED = (
    "That event was never pushed to #scout (no message id on file)."
)
SCOUT_RETRACT_OK = "Retracted."
SCOUT_RETRACT_PATCH_FAILED = (
    "Discord PATCH failed; retraction not visible in #scout yet."
)


def build_retracted_embed(*, title: str, url: str) -> dict[str, Any]:
    """Build the strikethrough/apology embed for ``/scout retract``.

    Pure function. The title gets Discord's ``~~strikethrough~~``
    markdown plus the lightning-bolt prefix (so a reader sees the same
    "scout-bot" identity even when retracted). The description carries
    a single apology line — no friend signal, no metadata, nothing that
    might encourage clicks on a retracted push.
    """
    safe_title = title[: TITLE_MAX - len(TITLE_PREFIX) - 8]
    description = "_Retracted by Yang. Sorry for the noise._"[:DESC_MAX]
    return {
        "title": f"{TITLE_PREFIX} ~~{safe_title}~~",
        "url": url,
        "description": description,
        "color": BRAND_COLOR,
    }


async def handle_scout_retract(
    interaction: Any,
    event_id: int,
    *,
    db_factory: Callable[[], Any],
    admin_discord_ids: frozenset[str],
    webhook_url: str,
    patch_fn: Callable[[str, str, dict[str, Any]], Any] = patch_event,
) -> None:
    """Handler for ``/scout retract <event_id>``.

    Authorization (CRITICAL): re-checks ``interaction.user.id`` against
    ``admin_discord_ids`` INSIDE the handler. ``MANAGE_MESSAGES``
    visibility gating in Discord doesn't constrain execution; a friend
    who learns the command name could still try to invoke it.

    On success: PATCHes the original message to the strikethrough/apology
    embed and marks the row via ``mark_event_retracted`` so the scoring
    engine learns from this.
    """
    success = False
    patch_status: str | None = None
    try:
        invoker_id = str(interaction.user.id)
        if invoker_id not in admin_discord_ids:
            # Aggregate-only log; never the id verbatim.
            log.info("scout-bot /scout retract: denied (non-admin)")
            await interaction.response.send_message(
                SCOUT_RETRACT_DENIED, ephemeral=True,
            )
            return

        def _load() -> Any | None:
            conn = db_factory()
            try:
                return conn.execute(
                    """SELECT id, title, url, discord_push_message_id
                       FROM events WHERE id = ?""",
                    (int(event_id),),
                ).fetchone()
            finally:
                _close_conn(conn)

        row = await run_db_call(_load)
        if row is None:
            log.info("scout-bot /scout retract: event_id=%d not found", int(event_id))
            await interaction.response.send_message(
                SCOUT_RETRACT_NOT_FOUND, ephemeral=True,
            )
            return
        message_id = row["discord_push_message_id"]
        if not message_id:
            log.info(
                "scout-bot /scout retract: event_id=%d never pushed", int(event_id),
            )
            await interaction.response.send_message(
                SCOUT_RETRACT_NOT_PUSHED, ephemeral=True,
            )
            return

        embed = build_retracted_embed(title=row["title"], url=row["url"])

        def _patch() -> Any:
            return patch_fn(webhook_url, message_id, embed)

        res = await run_db_call(_patch)
        # PostResult.status is an enum from webhook.py; bare-string check
        # keeps the import surface small and works against test doubles.
        status_value = getattr(res.status, "value", res.status)
        patch_status = status_value
        if status_value != "ok":
            log.info(
                "scout-bot /scout retract: PATCH failed event_id=%d status=%s",
                int(event_id), status_value,
            )
            await interaction.response.send_message(
                SCOUT_RETRACT_PATCH_FAILED, ephemeral=True,
            )
            return

        def _mark() -> int:
            conn = db_factory()
            try:
                return storage.mark_event_retracted(conn, int(event_id))
            finally:
                _close_conn(conn)

        await run_db_call(_mark)
        log.info("scout-bot /scout retract: ok event_id=%d", int(event_id))
        await interaction.response.send_message(SCOUT_RETRACT_OK, ephemeral=True)
        success = True
    finally:
        # Always emit slash_command; emit the additional `retract` row only
        # when the PATCH was actually attempted (status_value is known).
        await _record_slash_metric(
            db_factory=db_factory,
            command="scout_retract",
            success=success,
            event_id=int(event_id),
            extra_kind="retract" if patch_status is not None else None,
            extra_payload=(
                {"patch_status": patch_status} if patch_status is not None else None
            ),
        )


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def configure_bot_logging(data_dir: Path, *, verbose: bool = False) -> None:
    """Wire a rotating file handler at ``<data_dir>/logs/scout-bot.log``.

    The bot is long-running, so a single growing log file would dwarf
    the rest of ``data/``. Rotation keeps 5 x 1 MiB shards. The handler
    is attached to the root logger so all of the bot's transitive
    loggers (httpx, discord.py, this module) end up in the same file.

    Idempotent: a second call replaces the existing handler so a
    restart-in-place doesn't double-log.
    """
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / LOG_FILENAME

    root = logging.getLogger()
    # Drop any prior scout-bot handler so reload doesn't double-attach.
    for h in list(root.handlers):
        if getattr(h, "_scout_bot_handler", False):
            root.removeHandler(h)

    handler = RotatingFileHandler(
        path, maxBytes=1_048_576, backupCount=5, encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"),
    )
    handler._scout_bot_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # discord.py is chatty at INFO; surface only WARNING+.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def _close_conn(conn: Any) -> None:
    """Close a connection regardless of which storage backend opened it."""
    close = getattr(conn, "close", None)
    if callable(close):
        close()


def make_sqlite_factory(db_path: Path) -> Callable[[], sqlite3.Connection]:
    """Return a thunk that opens a fresh sqlite3 connection per call.

    Used by the bot at startup and by the integration test suite. A
    fresh connection per call mirrors how the cycle code uses
    ``_cycle_transaction`` — short-lived, autocommit, no concurrency
    surprises across the executor boundary.
    """

    def _open() -> sqlite3.Connection:
        return storage.connect(db_path)

    return _open


# ---------------------------------------------------------------------------
# Bot service entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BotConfig:
    data_dir: Path
    db_path: Path
    bot_token: str
    webhook_url: str
    admin_discord_ids: frozenset[str]
    friend_allowlist: FriendAllowlist
    # When non-empty, sync slash commands per-guild (instant). When empty,
    # fall back to global sync (up to 1h propagation per Discord SLA). For
    # a single-server friend bot like the Event Bros - NYC deploy, this
    # should always be set so every command-tree change is visible
    # immediately. See ``parse_guild_ids`` for the env contract.
    guild_ids: tuple[int, ...] = ()


def _make_client_factory() -> Callable[[BotConfig], Awaitable[None] | None]:
    """Build the real discord.py client. Indirection lets tests inject a stub.

    Importing discord.py at module top level would pay its (small) cost
    on every CLI invocation including unrelated test runs. Lazy-import
    inside the factory keeps the bot module fast to import for
    handler-level tests.
    """

    def _factory(cfg: BotConfig) -> Awaitable[None]:
        import discord
        from discord import app_commands

        # No `members` / `message_reactions` intents — the bot does not
        # need to enumerate guild members or listen for reactions in MVP.
        # `default()` would still pull `message_content`, which we also
        # don't need. The minimum-surface intent set is `Intents.none()`
        # plus `guilds` so the slash-command tree can register.
        intents = discord.Intents.none()
        intents.guilds = True

        client = discord.Client(intents=intents)
        tree = app_commands.CommandTree(client)

        db_factory = make_sqlite_factory(cfg.db_path)

        @tree.command(
            name="today",
            description="Top 1-3 AI events happening today (NYC).",
        )
        async def today_cmd(interaction: discord.Interaction) -> None:
            await handle_today(interaction, db_factory=db_factory)

        @tree.command(
            name="week",
            description="Top 1-3 AI events in the next 7 days.",
        )
        async def week_cmd(interaction: discord.Interaction) -> None:
            await handle_week(interaction, db_factory=db_factory)

        # /scout group: sub-commands `link` and `retract`.
        scout_group = app_commands.Group(
            name="scout", description="Scout-bot friend + admin actions.",
        )

        @scout_group.command(
            name="link",
            description="Link your Discord account to your friend-alpha row.",
        )
        async def link_cmd(interaction: discord.Interaction) -> None:
            await handle_scout_link(
                interaction,
                db_factory=db_factory,
                friend_allowlist=cfg.friend_allowlist,
            )

        @scout_group.command(
            name="retract",
            description="Retract a scout-bot push by event id (admin only).",
        )
        @app_commands.describe(event_id="DB id of the event to retract")
        async def retract_cmd(
            interaction: discord.Interaction, event_id: int,
        ) -> None:
            await handle_scout_retract(
                interaction,
                event_id,
                db_factory=db_factory,
                admin_discord_ids=cfg.admin_discord_ids,
                webhook_url=cfg.webhook_url,
            )

        tree.add_command(scout_group)

        @client.event
        async def on_ready() -> None:
            # Sync slash commands on first connect. Branch on cfg.guild_ids:
            #   - non-empty → per-guild sync (instant; the Event Bros - NYC
            #     friend deploy sets SCOUT_GUILD_IDS so command-tree
            #     changes show up the moment the bot starts).
            #   - empty → global sync (Discord's up-to-1h propagation;
            #     correct fallback for unknown deploy targets).
            # ``copy_global_to`` mirrors the in-memory tree onto the guild
            # before sync so the same set of commands lands in each scope.
            if cfg.guild_ids:
                import discord
                for gid in cfg.guild_ids:
                    guild = discord.Object(id=gid)
                    tree.copy_global_to(guild=guild)
                    await tree.sync(guild=guild)
                log.info(
                    "scout-bot ready: commands synced to guilds %s",
                    list(cfg.guild_ids),
                )
            else:
                await tree.sync()
                log.info("scout-bot ready: commands synced globally")

        async def _run() -> None:
            try:
                # ``log_handler=None`` so discord.py doesn't override our
                # rotating handler with its own stream handler. The token
                # never appears in any log line: discord.py masks it in
                # its own logs, and we never pass it to log.*.
                await client.start(cfg.bot_token, reconnect=True)
            except KeyboardInterrupt:
                await client.close()
                raise

        return _run()

    return _factory


def run_bot(
    cfg: BotConfig,
    *,
    client_factory: Callable[[BotConfig], Awaitable[None] | None] | None = None,
) -> None:
    """Boot the bot loop.

    Checks ``Marker.BOT_DISABLED`` BEFORE registering any commands so
    Yang can kill the bot via marker without unloading the launchd plist.
    Returns cleanly (no exception) when the marker is set so the launchd
    keep-alive policy treats it as an intentional exit.

    ``client_factory`` is injectable so the smoke test can mock the
    discord client and assert the no-op-when-marked path without
    importing discord.py's gateway machinery.
    """
    configure_bot_logging(cfg.data_dir)

    if is_marked(cfg.data_dir, Marker.BOT_DISABLED):
        log.warning("scout-bot disabled marker present; exiting cleanly")
        return

    factory = client_factory or _make_client_factory()
    coro = factory(cfg)
    if coro is None:
        return
    asyncio.run(coro)


def load_bot_config(
    *,
    data_dir: Path,
    db_path: Path,
    bot_token: str,
    webhook_url: str,
    admin_discord_ids: str,
    friend_allowlist: str,
    guild_ids: str = "",
) -> BotConfig:
    """Build a ``BotConfig`` from CLI/env strings.

    Centralized so the CLI command stays a thin wrapper and tests can
    construct the same shape without re-deriving the parse rules.
    """
    return BotConfig(
        data_dir=data_dir,
        db_path=db_path,
        bot_token=bot_token,
        webhook_url=webhook_url,
        admin_discord_ids=parse_admin_allowlist(admin_discord_ids),
        friend_allowlist=parse_friend_allowlist(friend_allowlist),
        guild_ids=parse_guild_ids(guild_ids),
    )


__all__ = [
    "LOG_FILENAME",
    "SCOUT_LINK_COLLISION",
    "SCOUT_LINK_NO_ROW",
    "SCOUT_LINK_OK",
    "SCOUT_LINK_REJECT",
    "SCOUT_RETRACT_DENIED",
    "SCOUT_RETRACT_NOT_FOUND",
    "SCOUT_RETRACT_NOT_PUSHED",
    "SCOUT_RETRACT_OK",
    "SCOUT_RETRACT_PATCH_FAILED",
    "BotConfig",
    "FriendAllowlist",
    "InteractionLike",
    "build_retracted_embed",
    "configure_bot_logging",
    "handle_scout_link",
    "handle_scout_retract",
    "handle_today",
    "handle_week",
    "load_bot_config",
    "make_sqlite_factory",
    "parse_admin_allowlist",
    "parse_friend_allowlist",
    "render_event_embeds",
    "run_bot",
    "run_db_call",
    "today_window",
    "week_window",
]
