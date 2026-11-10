"""One scrape→score→post cycle. The heart of v1."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.configs.settings import llm_scoring_mode
from api.integrations.discord.webhook import PostStatus, format_embed, post_event
from api.integrations.llm import (
    CURRENT_SCORING_PROMPT,
    EventFit,
    NotablePerson,
    compute_taste_sha,
    score_batch,
)
from api.integrations.llm.cli_fallback import CliFallbackError, score_batch_sync
from api.integrations.scrapers import SCRAPERS
from api.models.events import RawEvent
from api.repositories.availability import terminal_source_identities
from api.repositories.database import (
    open_database,
)
from api.repositories.events import (
    compute_friends_going,
    connect,
    fetch_pending_scoring,
    fetch_top_unpushed_future_event,
    fetch_top_unpushed_future_score,
    mark_posted,
    mark_scoring_failed,
    update_fit,
    upsert_raw,
    write_scout_metric,
)
from api.repositories.posting_claims import (
    claim_postable_for_scout,
    release_posting_claim,
)
from api.repositories.runtime_markers import (
    release_cycle_lock,
    try_acquire_cycle_lock,
)
from api.tasks.availability import refresh_availability
from api.tasks.cleanup import (
    CycleHealth,
    load_health,
    record_all_failed,
    record_partial_or_success,
    save_health,
)
from api.tasks.markers import (
    Marker,
    clear_marker,
    is_cooldown_expired,
    is_marked,
    touch_marker,
    write_marker,
)

log = logging.getLogger(__name__)

ALL_FAILED_THRESHOLD = 3  # consecutive cycles before scoring is disabled
SYSTEM_EMBED_RED = 0xE74C3C


@dataclass
class CycleConfig:
    db_path: Path | None
    data_dir: Path
    webhook_url: str
    taste_profile: str
    # The current prompt preserves ranking and separates source facts from freshness.
    # The CLI mirrors this default; old prompt files remain replayable.
    prompt_version: str = CURRENT_SCORING_PROMPT
    # Scout-bot push gate (spec §10 step 1; migration 0006). Top-1 unpushed
    # future event whose fit_score clears this is pushed to the scout channel
    # each cycle. 0.85 keeps the channel quiet — push fires only when the
    # event clearly crosses the bar. This is now the sole post threshold:
    # the legacy ``claim_postable`` path that posted everything >= 0.7 was
    # retired so the channel doesn't get 5+ medium-fit fire-and-forget
    # messages per cycle on top of the curated top pick.
    high_bar_threshold: float = 0.85
    # Per-LLM-call batch: 5 keeps each scoring subprocess well under the 180s
    # CLI timeout on real Luma payloads (long descriptions + 7 output fields
    # per event). Smaller batches also isolate failures — one bad event no
    # longer kills 20. The 20-event default was a Sonnet-era convenience that
    # stopped fitting once the v2 prompt added notable_people / event_type /
    # food_provided extraction.
    scoring_batch_size: int = 5
    # Per-cycle cap across multiple LLM calls. The cycle now loops scoring in
    # chunks of ``scoring_batch_size`` until either this cap is hit or there
    # are no more pending events, so a single cycle still drains a normal
    # scrape's backlog. Default 20 is the previous behavior when each cycle
    # made one big LLM call — preserved here as 4 small Haiku calls instead.
    scoring_max_per_cycle: int = 20
    scrape_sources: tuple[str, ...] = field(default_factory=lambda: tuple(SCRAPERS))
    scrape_limit_per_source: int = 50


@dataclass
class CycleResult:
    scraped: int = 0
    scored_ok: int = 0
    scored_failed: int = 0
    posted: int = 0
    skipped_reason: str | None = None
    availability_checked: int = 0
    availability_confirmed: int = 0


def run_scrapers(sources: tuple[str, ...], limit: int,
                 *, excluded_sources: frozenset[tuple[str, str]] = frozenset()) -> list[RawEvent]:
    """Indirection for tests to monkeypatch."""
    out: list[RawEvent] = []
    for s in sources:
        try:
            scraper = SCRAPERS[s]()
            scraper.excluded_sources = excluded_sources
            out.extend(scraper.scrape(limit=limit))
        except Exception as e:
            log.warning("scraper %s failed: %s", s, e)
    return out


def score_batch_sync_for_cycle(
    events: list[dict[str, Any]],
    taste_profile: str,
    today: str,
    prompt_version: str,
) -> list[EventFit]:
    """Indirection for tests to monkeypatch."""
    mode = llm_scoring_mode()
    if mode in ("api", "pi-auth"):
        return asyncio.run(
            score_batch(
                events,
                taste_profile=taste_profile,
                today=today,
                prompt_version=prompt_version,
            )
        )
    if mode == "codex-cli":
        from api.integrations.llm.codex_cli import score_batch_sync as codex_score_batch_sync

        return codex_score_batch_sync(
            events,
            taste_profile=taste_profile,
            today=today,
            prompt_version=prompt_version,
        )
    return score_batch_sync(
        events,
        taste_profile=taste_profile,
        today=today,
        prompt_version=prompt_version,
    )


def _coerce_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _scoring_date(value: str | datetime | None) -> str | None:
    """Normalize SQLite strings and Postgres datetimes for the JSON prompt."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_get(row: Any, key: str) -> Any:
    """Safely read ``key`` from a sqlite3.Row or dict-like row.

    Defense-in-depth for hand-crafted test rows that construct synthetic
    rows without all scout-bot columns. The production path is guarded by
    ``_assert_scout_columns_present`` (which fails loud at cycle start when
    migration 0006 isn't applied), so this helper exists only to tolerate
    legacy fixture shapes — NOT to silently degrade real pushes.
    """
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


SCOUT_MIGRATION_FILENAME = "0006_scout_bot_mvp.sql"


def _assert_scout_columns_present(conn: Any) -> None:
    """Fail loud if migration 0006 hasn't been applied.

    Silent degradation (no neighborhood, no event_type, no food, no capacity
    in the push) is the worst trust-failure mode for a "metadata-adding,
    not editorializing" bot. Run this once per cycle, before any scout-bot
    rendering, so an un-migrated DB is impossible-to-miss instead of
    rendering thin, content-stripped cards.
    """
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE filename = ?",
        (SCOUT_MIGRATION_FILENAME,),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"Migration {SCOUT_MIGRATION_FILENAME} not applied; "
            f"scout-bot cannot run. Apply with: uv run nyc-ai-scout migrate"
        )
    if conn.execute("SELECT 1 FROM schema_migrations WHERE filename = ?",
                    ("0011_registration_availability.sql",)).fetchone() is None:
        raise RuntimeError("Migration 0011_registration_availability.sql not applied; "
                           "apply catalog migrations before promoting this worker")
    if conn.execute("SELECT 1 FROM schema_migrations WHERE filename = ?",
                    ("0013_event_annotations.sql",)).fetchone() is None:
        raise RuntimeError("Migration 0013_event_annotations.sql not applied; "
                           "apply catalog migrations before promoting this worker")


@contextmanager
def _cycle_transaction(cfg: CycleConfig, database: Any | None = None) -> Iterator[Any]:
    if cfg.db_path is not None:
        conn = connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()
        return
    if database is None:
        database = open_database()
    with database.compat_connect() as conn:
        yield conn


def _parse_notable_people(raw: str | None) -> list[NotablePerson]:
    """Hydrate the ``notable_people`` TEXT column back into Pydantic models.

    Storage round-trips the v2 LLM extraction as a JSON-encoded list (see
    ``update_fit``); the renderer wants typed ``NotablePerson`` objects so
    the confidence filter can run. Tolerate missing / malformed payloads
    (older rows, manual fixture edits) by returning an empty list — the
    renderer then omits the ``notable people:`` line entirely.
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
            # Defensive: don't let a single bad row drop the whole render.
            continue
    return out


def _scan_and_push_for_scout(
    cfg: CycleConfig,
    database: Any | None,
    result: CycleResult,
) -> bool:
    """Top-1 unpushed future event → POST to #scout with ``?wait=true``.

    Spec §1 (scan-and-push trigger). Runs once per cycle tick, after the
    scrape+score loop. Increments ``result.posted`` on successful push so
    the cycle's outward-facing counter still reflects work done after the
    legacy ``claim_postable`` path was retired.

    Returns True if the scout-scan wrote the ``WEBHOOK_DISABLED`` marker
    (caller should short-circuit the rest of the tick).
    """
    with _cycle_transaction(cfg, database) as conn:
        # Spec §11 telemetry: always read the unfiltered top1 first so we
        # capture the absolute distribution of the unpushed-future pool,
        # not just the score that crossed the bar. Yang tunes the bar from
        # this distribution after a week of production data.
        top1_score = fetch_top_unpushed_future_score(conn)
        row = (
            fetch_top_unpushed_future_event(
                conn, high_bar_threshold=cfg.high_bar_threshold,
            )
            if top1_score is not None and top1_score >= cfg.high_bar_threshold
            else None
        )
        write_scout_metric(
            conn,
            event_kind="tick_scan",
            payload={
                "top1_score": top1_score,
                "bar": cfg.high_bar_threshold,
                "qualified": row is not None,
            },
        )
    if row is None:
        return False

    claim_token = str(uuid.uuid4())
    with _cycle_transaction(cfg, database) as conn:
        claim = claim_postable_for_scout(
            conn, event_id=int(row["id"]), token=claim_token,
        )
    if claim is None:
        # Another process holds the claim; skip this tick. The next tick
        # will try again once the claim either resolves or its TTL expires.
        return False

    with _cycle_transaction(cfg, database) as conn:
        friends = compute_friends_going(conn, int(row["id"]))
        # Spec §11 telemetry: count only — friend display names must never
        # leak to the metrics column (it's treated as semi-public).
        write_scout_metric(
            conn,
            event_kind="friend_signal",
            event_id=int(row["id"]),
            payload={
                "rendered_count": len(friends),
                "status_set": "registering,applied,pending_approval,confirmed",
            },
        )

    why_line: str | None = None
    urgency = row["urgency"]
    urgency_reason = row["urgency_reason"]
    if urgency == "high" and urgency_reason:
        why_line = urgency_reason

    # TODO: ``agenda`` and ``cover_image_url`` were deferred from the
    # scout-bot MVP.
    # Agenda extraction from the source listing and cover-image scraping
    # are tracked items but not yet implemented; pass None here.
    # ``format_embed`` omits the corresponding lines per Task 2's contract
    # (renderer treats None as "omit the line", not "fake the line"), so
    # the push card just renders without these fields today.
    embed = format_embed(
        title=row["title"],
        url=row["url"],
        start_at=_coerce_datetime(row["start_at"]),
        end_at=_coerce_optional_datetime(row["ends_at"]),
        neighborhood=row["neighborhood"],
        capacity_remaining=row["last_known_capacity"],
        why_line=why_line,
        notable_people=_parse_notable_people(row["notable_people"]),
        event_type=row["event_type"],
        food_provided=row["food_provided"],
        agenda=None,
        friends_going=friends,
        cover_image_url=None,
        source=row["source"],
        fit_score=row["fit_score"],
    )

    # Crash-recovery (spec §1 — accepted rare-duplicate risk): the claim is
    # held across the POST. If the process dies between the POST returning
    # 2xx and the ``mark_posted`` write below, Discord has the message but
    # we never recorded the id. The claim sits for up to the 1h TTL, the
    # next scan finds the row again (``discord_push_message_id IS NULL``),
    # and re-pushes. Cosmetic dup, observable to Yang via ``/scout retract``.
    # ``mark_posted`` writes the message id, ``posted_at``, and clears the
    # claim columns in one UPDATE so we can't strand the row half-written
    # (autocommit means a previous two-statement sequence had a real gap).
    res = post_event(cfg.webhook_url, embed)
    # Spec §11 telemetry: per-push attempt. Fires regardless of outcome so
    # Yang can see retryable/fatal trends without correlating logs.
    with _cycle_transaction(cfg, database) as conn:
        write_scout_metric(
            conn,
            event_kind="push_attempt",
            event_id=int(row["id"]),
            payload={
                "status": res.status.value,
                "message_id_captured": bool(res.message_id),
            },
        )

    if res.status == PostStatus.OK:
        if not res.message_id:
            # ``?wait=true`` was sent; Discord should have returned an id.
            # If it didn't, treat as retryable — without the id we can't
            # PATCH later, so re-running on the next tick is safer than
            # silently degrading.
            with _cycle_transaction(cfg, database) as conn:
                release_posting_claim(conn, int(row["id"]), token=claim.token)
            log.error(
                "scout push: 2xx but no message_id; releasing claim "
                "event_id=%d", int(row["id"]),
            )
            return False
        # One atomic UPDATE: writes ``discord_push_message_id``,
        # ``posted_at``, and clears the claim columns together. See
        # ``mark_posted`` docstring on why this matters under autocommit.
        with _cycle_transaction(cfg, database) as conn:
            mark_posted(
                conn, int(row["id"]),
                discord_push_message_id=res.message_id,
            )
        result.posted += 1
        return False

    if res.status == PostStatus.FATAL:
        with _cycle_transaction(cfg, database) as conn:
            release_posting_claim(conn, int(row["id"]), token=claim.token)
            write_marker(
                cfg.data_dir, Marker.WEBHOOK_DISABLED,
                f"webhook fatal: {res.detail}",
                conn=conn,
            )
        # Aggregate-only error log: no event title (the title is verbatim
        # Luma copy, but the row-level detail can be reconstructed via
        # event_id offline if needed). Friend data stays out of logs.
        log.error(
            "scout push fatal: event_id=%d status=%s",
            int(row["id"]), res.status.value,
        )
        return True

    # RETRYABLE — release the claim so the next tick can pick it up.
    with _cycle_transaction(cfg, database) as conn:
        release_posting_claim(conn, int(row["id"]), token=claim.token)
    log.info(
        "scout push deferred (retryable): event_id=%d", int(row["id"]),
    )
    return False


def _coerce_optional_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    return _coerce_datetime(value)


def run_cycle(cfg: CycleConfig) -> CycleResult:
    database = None if cfg.db_path is not None else open_database()
    lock_owner: str | None = None
    try:
        with _cycle_transaction(cfg, database) as conn:
            # Fail loud if the scout-bot schema isn't in place. Run once per
            # cycle, before any scoring or rendering, so an un-migrated DB
            # surfaces immediately instead of silently producing thin pushes
            # via ``_row_get`` returning None for every new column.
            _assert_scout_columns_present(conn)

            # A scoring/push breaker must not prevent source-availability refresh.
            skipped_reason = None
            if is_marked(cfg.data_dir, Marker.WEBHOOK_DISABLED, conn=conn):
                skipped_reason = "webhook_disabled"
            elif is_marked(cfg.data_dir, Marker.SCORING_DISABLED, conn=conn):
                skipped_reason = "scoring_disabled"
            # The marker may exist but its cooldown has elapsed (is_marked
            # returns False in that case). When that happens we proceed with
            # the cycle, but pre-reset ``consecutive_all_failed`` so a single
            # fresh failure doesn't immediately re-trip the breaker.
            scoring_retry_after_cooldown = is_cooldown_expired(
                cfg.data_dir, Marker.SCORING_DISABLED, conn=conn,
            )
            if scoring_retry_after_cooldown:
                log.info(
                    "scoring_disabled cooldown elapsed; retrying with fresh counter",
                )
                save_health(cfg.data_dir, CycleHealth(), conn=conn)

            candidate_lock_owner = str(uuid.uuid4())
            if not try_acquire_cycle_lock(
                conn, name="cycle", owner=candidate_lock_owner, ttl_seconds=3600
            ):
                log.warning("cycle lock already held; skipping")
                return CycleResult(skipped_reason="cycle_already_running")
            lock_owner = candidate_lock_owner

        result = CycleResult()
        if skipped_reason:
            refresh = refresh_availability(lambda: _cycle_transaction(cfg, database))
            return CycleResult(skipped_reason=skipped_reason,
                               availability_checked=refresh.attempted,
                               availability_confirmed=refresh.confirmed)
        with _cycle_transaction(cfg, database) as conn:
            health = load_health(cfg.data_dir, conn=conn)
            excluded_sources = terminal_source_identities(conn)

        raws = run_scrapers(cfg.scrape_sources, cfg.scrape_limit_per_source,
                            excluded_sources=excluded_sources)
        result.scraped = len(raws)

        with _cycle_transaction(cfg, database) as conn:
            for raw in raws:
                upsert_raw(conn, raw)

        refresh = refresh_availability(lambda: _cycle_transaction(cfg, database))
        result.availability_checked = refresh.attempted
        result.availability_confirmed = refresh.confirmed

        # Loop scoring in chunks of ``scoring_batch_size`` until either the
        # per-cycle cap is hit or no pending events remain. Each chunk is a
        # separate LLM subprocess: a single bad event can fail one
        # chunk without taking the whole cycle's scoring with it.
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        taste_sha = compute_taste_sha(cfg.taste_profile)
        scored_this_cycle = 0
        attempted_ids: set[int] = set()
        while scored_this_cycle < cfg.scoring_max_per_cycle:
            remaining_budget = cfg.scoring_max_per_cycle - scored_this_cycle
            chunk_limit = min(cfg.scoring_batch_size, remaining_budget)
            with _cycle_transaction(cfg, database) as conn:
                pending = fetch_pending_scoring(
                    conn, limit=chunk_limit,
                    refresh_prompt_version=cfg.prompt_version if cfg.prompt_version == CURRENT_SCORING_PROMPT else None,
                    exclude_ids=frozenset(attempted_ids),
                )
            if not pending:
                break
            payload = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "date": _scoring_date(r["start_at"]),
                    "host": r["host"],
                    "venue": r["venue"],
                    "neighborhood": r["neighborhood"],
                    "attendance_mode": r["attendance_mode"],
                    "url": r["url"],
                    "notes": r["description"],
                }
                for r in pending
            ]
            try:
                fits = score_batch_sync_for_cycle(
                    payload, cfg.taste_profile, today, cfg.prompt_version,
                )
                fits_by_id = {f.event_id: f for f in fits}
                with _cycle_transaction(cfg, database) as conn:
                    for r in pending:
                        fit = fits_by_id.get(r["id"])
                        if fit is None:
                            mark_scoring_failed(conn, r["id"], "not in batch result",
                                                expected_input_hash=r["fit_input_hash"])
                            result.scored_failed += 1
                        else:
                            persisted = update_fit(
                                conn, r["id"],
                                fit_score=fit.fit_score,
                                reasoning=fit.reasoning,
                                urgency=fit.urgency,
                                urgency_reason=fit.urgency_reason,
                                prompt_version=cfg.prompt_version,
                                taste_sha=taste_sha,
                                notable_people=[
                                    p.model_dump() for p in fit.notable_people
                                ] or None,
                                event_type=fit.event_type,
                                food_provided=fit.food_provided,
                                highlight=fit.highlight.model_dump(mode="json") if fit.highlight else None,
                                urgency_note=fit.urgency_note.model_dump(mode="json") if fit.urgency_note else None,
                                expected_input_hash=r["fit_input_hash"],
                            )
                            result.scored_ok += int(persisted)
            except CliFallbackError as e:
                # Aggregate-only error log: CliFallbackError bodies can carry
                # cleaned[:400] of raw LLM output (see _extract_json_array),
                # which under v2 contains event titles and notable-person
                # names. CLAUDE.md forbids leaking that to logs — keep the
                # error line a structured count and route the body to debug.
                log.error(
                    "score_batch failed: kind=%s n_pending=%d",
                    type(e).__name__, len(pending),
                )
                log.debug("score_batch failure detail", exc_info=e)
                # Same friend-data concern for the DB column: str(e) can echo
                # cleaned[:400] of raw LLM output into scoring_failed_reason,
                # which is a TEXT column we pull into triage dumps. Persist
                # only the exception class as a triage hint; the full body
                # stays in the debug log above.
                failure_reason = type(e).__name__
                with _cycle_transaction(cfg, database) as conn:
                    for r in pending:
                        mark_scoring_failed(conn, r["id"], failure_reason,
                                            expected_input_hash=r["fit_input_hash"])
                        result.scored_failed += 1
            scored_this_cycle += len(pending)
            attempted_ids.update(r["id"] for r in pending)

        # Update cycle health
        if result.scored_failed > 0 and result.scored_ok == 0:
            health = record_all_failed(health, "all events failed scoring")
        elif result.scored_ok > 0:
            health = record_partial_or_success(health)

        # Trip the breaker
        breaker_tripped = health.consecutive_all_failed >= ALL_FAILED_THRESHOLD
        with _cycle_transaction(cfg, database) as conn:
            save_health(cfg.data_dir, health, conn=conn)
            if breaker_tripped:
                write_marker(
                    cfg.data_dir, Marker.SCORING_DISABLED,
                    f"{ALL_FAILED_THRESHOLD} consecutive all-failed cycles; "
                    f"last error: {health.last_error}",
                    conn=conn,
                )
            elif scoring_retry_after_cooldown:
                # Auto-cooldown retry path: the marker still exists on disk
                # but its mtime was older than the cooldown window so we let
                # this cycle proceed. Reconcile the marker state now:
                #   - success (scored_ok > 0)        → clear the marker
                #   - all-failed but not yet tripped → touch mtime to restart cooldown
                #   - no pending events to score     → leave marker untouched;
                #     a benign empty cycle shouldn't claim recovery.
                if result.scored_ok > 0:
                    clear_marker(cfg.data_dir, Marker.SCORING_DISABLED, conn=conn)
                    log.info(
                        "scoring_disabled cleared after successful cooldown retry",
                    )
                elif result.scored_failed > 0:
                    touch_marker(
                        cfg.data_dir, Marker.SCORING_DISABLED,
                        f"cooldown retry failed; last error: {health.last_error}",
                        conn=conn,
                    )
                    log.warning(
                        "scoring_disabled cooldown retry failed; restarting cooldown",
                    )
        if health.consecutive_all_failed >= ALL_FAILED_THRESHOLD:
            _try_post_system_embed(cfg, "scout offline — scoring failing",
                                   health.last_error or "unknown")
            return result

        # Scout-bot scan-and-push (spec §1). Runs AFTER scoring so the same
        # tick can both score a new event AND push it if it crosses the high
        # bar. This is now the only post path — the legacy
        # ``claim_postable`` loop that fired for everything >= 0.7 was
        # retired because (a) it duplicated the channel with mid-fit
        # fire-and-forget posts on top of the curated top pick, and (b) it
        # didn't capture Discord's message id so the capacity-poller could
        # never PATCH those messages.
        webhook_dead = _scan_and_push_for_scout(cfg, database, result)
        if webhook_dead:
            return result
    finally:
        if lock_owner is not None:
            with _cycle_transaction(cfg, database) as conn:
                release_cycle_lock(conn, name="cycle", owner=lock_owner)
        if database is not None:
            database.engine.dispose()

    return result


def _try_post_system_embed(cfg: CycleConfig, title: str, description: str) -> None:
    """Best-effort red embed when the breaker trips."""
    embed = {
        "title": title,
        "description": description[:4096],
        "color": SYSTEM_EMBED_RED,
    }
    try:
        post_event(cfg.webhook_url, embed)
    except Exception as e:
        log.warning("system embed post failed: %s", e)
