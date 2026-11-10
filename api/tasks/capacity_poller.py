"""Hourly capacity ticker (spec §4).

Scans every event the scout-bot has pushed (``discord_push_message_id IS
NOT NULL``) that is still future and not yet cancelled, refreshes the
remaining capacity from Luma, and PATCHes the Discord message when
``capacity_remaining`` crosses 50 / 25 / 10 spots. On a host-cancelled
event, PATCHes once with a strikethrough title + apology line and sets
``cancelled_at`` so the row stays out of future scans.

Idempotency contract (CRITICAL):
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

PATCH-first, then write. We PATCH Discord BEFORE setting the corresponding
``cap_<threshold>_fired`` column. If the process dies between the PATCH
success and the column write, the next hour's poll re-fires the same
threshold and the channel sees a duplicate edit at the same band.
Accepted MVP cosmetic dup, NOT data corruption. The inverse ordering
(write-first) is worse: a transient network failure between write and
PATCH would silently swallow a real warning forever.

Multiple-threshold crossing:
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When capacity drops from 60 → 8 in one tick, all three bands fire — 50
first, then 25, then 10 — in that order, each followed by its column
write. Three messages over ~3 seconds is acceptable per spec §4.

Logging:
~~~~~~~~

Never log event titles or friend data at INFO+ level. Aggregate counts
only. Per-event detail (event_id) is fine; row-level reconstruction off
event_id is a manual SQL one-liner if needed.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.integrations.discord.webhook import (
    CAPACITY_WARNING_THRESHOLDS,
    PostStatus,
    format_embed,
    patch_event,
)
from api.integrations.scrapers.luma import (
    CapacitySnapshot,
    LumaCapacityFetchError,
    fetch_capacity_for_event,
)
from api.repositories.database import open_database
from api.repositories.events import (
    compute_friends_going,
    connect,
    fetch_active_pushed_events,
    mark_capacity_threshold_fired,
    mark_event_cancelled,
    update_last_known_capacity,
    write_scout_metric,
)
from api.tasks.cycle import (
    _coerce_datetime,
    _coerce_optional_datetime,
    _parse_notable_people,
)

log = logging.getLogger(__name__)

# Per-host rate-limit backoff. The hourly cadence already keeps us well
# below Luma's threshold, but a transient 429 (or 5xx) should back off
# instead of hammering the same host harder. We sleep ``base * 2**n``
# capped at ``MAX_BACKOFF_SECONDS`` between same-host retries.
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
DEFAULT_POLL_BATCH = 100


@dataclass
class CapacityPollerConfig:
    """Configuration for one capacity poll run."""

    db_path: Path | None
    webhook_url: str
    batch_size: int = DEFAULT_POLL_BATCH


@dataclass
class CapacityPollerResult:
    """Aggregate counts from a single poll run.

    Per-event titles and friend data must never appear here — these counts
    are safe to log at INFO+ and emit to telemetry. ``patches_failed``
    and ``scrapes_failed`` carry only counts; per-event detail goes to
    DEBUG via the structured ``event_id=`` log lines below.
    """

    scanned: int = 0
    scrapes_ok: int = 0
    scrapes_failed: int = 0
    patches_ok: int = 0
    patches_failed: int = 0
    cancellations: int = 0
    thresholds_fired: dict[int, int] = field(
        default_factory=lambda: {t: 0 for t in CAPACITY_WARNING_THRESHOLDS}
    )


def thresholds_to_fire(
    *,
    current_capacity: int,
    fired_flags: dict[int, bool],
) -> list[int]:
    """Return the ordered list of thresholds that should fire this poll.

    Pure function. Highest-to-lowest order (50 → 25 → 10) so multi-band
    crossings PATCH in the same sequence the user reads them (the message
    history scrolls top-down). A threshold fires when:

      1. ``current_capacity <= threshold`` (the band is breached), AND
      2. ``fired_flags[threshold] is False`` (we haven't already fired
         this band for this event).

    Spec §4 examples (with the absolute-spot interpretation of 50/25/10):
      - 100 → 60: no fires (60 > 50).
      - 100 → 45: fires [50] (45 ≤ 50, 45 > 25).
      - 100 → 20: fires [50, 25] (20 ≤ 50, 20 ≤ 25, 20 > 10).
      - 100 →  8: fires [50, 25, 10] (8 ≤ all three).
    """
    out: list[int] = []
    for threshold in CAPACITY_WARNING_THRESHOLDS:
        if fired_flags.get(threshold, False):
            continue
        if current_capacity <= threshold:
            out.append(threshold)
    return out


def _row_to_embed_kwargs(row: Any, *, friends_going: list[str]) -> dict[str, Any]:
    """Build the ``format_embed`` kwargs for a PATCH render.

    Re-renders the canonical card from the event row so the rest of the
    embed (time, friend-signal, notable people, etc.) stays intact across
    the PATCH per spec §4. ``friends_going`` is re-queried at PATCH time
    by the caller (``_process_event``) — the friend line reflects the
    LIVE state at edit time, not a stale snapshot from push time. This
    costs one extra query per fired threshold but only ~3 fired thresholds
    per event lifetime, so the N+1 risk is bounded.

    ``agenda`` and ``cover_image_url`` are deferred per spec §7 (same as
    cycle.py); the renderer omits the corresponding lines when these
    fields are None.
    """
    return dict(
        title=row["title"],
        url=row["url"],
        start_at=_coerce_datetime(row["start_at"]),
        end_at=_coerce_optional_datetime(row["ends_at"]),
        neighborhood=row["neighborhood"],
        capacity_remaining=row["last_known_capacity"],
        why_line=(
            row["urgency_reason"]
            if row["urgency"] == "high" and row["urgency_reason"]
            else None
        ),
        notable_people=_parse_notable_people(row["notable_people"]),
        event_type=row["event_type"],
        food_provided=row["food_provided"],
        agenda=None,  # Deferred (spec §7); see cycle.py for the same TODO.
        friends_going=friends_going,
        cover_image_url=None,
        source=row["source"],
        fit_score=row["fit_score"],
    )


def _fired_flags_from_row(row: Any) -> dict[int, bool]:
    """Read the three idempotency columns into a ``{threshold: bool}`` dict."""
    return {
        50: bool(row["cap_50_fired"]),
        25: bool(row["cap_25_fired"]),
        10: bool(row["cap_10_fired"]),
    }


def _backoff_sleep(attempt: int, retry_after: float | None) -> None:
    """Sleep for exponential backoff before the next same-host attempt.

    Respects ``Retry-After`` (capped) when Luma supplied one, else falls
    back to ``BASE_BACKOFF_SECONDS * 2**attempt`` with a small jitter so
    a thundering-herd of polls (e.g., multi-machine) doesn't synchronize.
    """
    if retry_after is not None:
        delay = min(retry_after, MAX_BACKOFF_SECONDS)
    else:
        delay = min(BASE_BACKOFF_SECONDS * (2 ** attempt), MAX_BACKOFF_SECONDS)
    # Add up to 250ms of jitter to break herd synchronization.
    delay += random.uniform(0, 0.25)
    time.sleep(delay)


def _scrape_capacity_with_backoff(
    event_url: str,
    *,
    max_retries: int = 3,
    sleep: Any = _backoff_sleep,
) -> CapacitySnapshot:
    """Fetch capacity with exponential backoff on 429 / 5xx / timeout.

    Returns the snapshot on the first success. Re-raises
    ``LumaCapacityFetchError`` after exhausting retries so the poller can
    skip this event for this cycle without breaking the rest of the sweep.

    ``sleep`` is injectable so tests can avoid actual delays.
    """
    last_exc: LumaCapacityFetchError | None = None
    for attempt in range(max_retries):
        try:
            return fetch_capacity_for_event(event_url)
        except LumaCapacityFetchError as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                break
            sleep(attempt, exc.retry_after)
    assert last_exc is not None  # loop only exits via return or exception
    raise last_exc


def _process_event(
    *,
    row: Any,
    cfg: CapacityPollerConfig,
    conn: Any,
    result: CapacityPollerResult,
    sleep: Any = _backoff_sleep,
    patch_callable: Any = patch_event,
    scrape_callable: Any = _scrape_capacity_with_backoff,
) -> None:
    """Poll one event end-to-end. See module docstring for the contract.

    Splits cleanly so tests can pass mock ``patch_callable`` /
    ``scrape_callable`` without touching the network. ``patch_callable``
    follows the ``patch_event`` signature; ``scrape_callable`` follows
    ``_scrape_capacity_with_backoff``.
    """
    event_id = int(row["id"])
    message_id = row["discord_push_message_id"]

    # Scrape phase. A single-event failure must not break the whole poll.
    try:
        snapshot = scrape_callable(row["url"], sleep=sleep)
    except LumaCapacityFetchError as exc:
        result.scrapes_failed += 1
        # No event title in this log line (CLAUDE.md / friend-data + scout
        # privacy concerns). event_id is enough for offline correlation.
        log.warning(
            "capacity scrape failed event_id=%d kind=%s",
            event_id, type(exc).__name__,
        )
        return
    result.scrapes_ok += 1

    # Cancellation branch. Single PATCH, then set cancelled_at so future
    # polls skip the row. ``compute_friends_going`` is queried fresh so
    # the apology card carries the SAME friend-signal the original push
    # had — keeping the embed shape stable across the edit per spec §4.
    if snapshot.cancelled:
        friends = compute_friends_going(conn, event_id)
        embed = format_embed(
            **_row_to_embed_kwargs(row, friends_going=friends),
            cancelled=True,
        )
        res = patch_callable(cfg.webhook_url, message_id, embed)
        if res.status == PostStatus.OK:
            mark_event_cancelled(conn, event_id)
            result.cancellations += 1
            result.patches_ok += 1
            log.info("capacity cancellation patched event_id=%d", event_id)
        else:
            result.patches_failed += 1
            log.warning(
                "capacity cancellation PATCH failed event_id=%d status=%s",
                event_id, res.status.value,
            )
        return

    # No new capacity reading → nothing to compare against. Skip this
    # tick so the next poll re-attempts.
    if snapshot.capacity_remaining is None:
        return

    current = snapshot.capacity_remaining
    fired_flags = _fired_flags_from_row(row)
    to_fire = thresholds_to_fire(
        current_capacity=current, fired_flags=fired_flags,
    )

    # Even if no thresholds fire, refresh last_known_capacity so the row
    # carries the freshest count (used by /today and /week renders, and
    # by future capacity-drop diagnostics).
    if not to_fire:
        update_last_known_capacity(conn, event_id, current)
        return

    # Fire in descending order (50 → 25 → 10). For each threshold:
    # 1. Re-render the embed with capacity_warning_threshold=T and the
    #    NEW capacity (we update last_known_capacity in memory before the
    #    render so the time/place line shows the latest spot count).
    # 2. PATCH Discord.
    # 3. ONLY on 2xx, set cap_<T>_fired = 1. PATCH-first ordering is the
    #    idempotency contract (see module docstring).
    # The row mutation here is in-memory only; the persistent
    # last_known_capacity write happens once at the end so a mid-loop
    # crash leaves the row in a recoverable state (next poll re-scans).
    # Friend signal is re-queried ONCE (not per threshold) so a 3-band
    # collapse doesn't issue 3 friend queries.
    friends = compute_friends_going(conn, event_id)
    base_kwargs = _row_to_embed_kwargs(row, friends_going=friends)
    base_kwargs["capacity_remaining"] = current  # snapshot wins over stale row
    for threshold in to_fire:
        embed = format_embed(
            **base_kwargs,
            capacity_warning_threshold=threshold,
        )
        res = patch_callable(cfg.webhook_url, message_id, embed)
        # Spec §11 telemetry: per-threshold attempt (attempted + succeeded
        # counters fold into this single row via status). Fires regardless
        # of PATCH outcome.
        write_scout_metric(
            conn,
            event_kind="capacity_patch",
            event_id=event_id,
            payload={"threshold": threshold, "status": res.status.value},
        )
        if res.status != PostStatus.OK:
            result.patches_failed += 1
            log.warning(
                "capacity ticker PATCH failed event_id=%d threshold=%d status=%s",
                event_id, threshold, res.status.value,
            )
            # Stop firing further thresholds; we'll retry the whole
            # sequence next hour (cap_<T>_fired is still 0).
            return
        # PATCH 2xx — NOW mark the column. The (rare) crash window
        # between these two lines is the documented cosmetic-dup risk.
        mark_capacity_threshold_fired(conn, event_id, threshold)
        result.patches_ok += 1
        result.thresholds_fired[threshold] += 1
        log.info(
            "capacity ticker patched event_id=%d threshold=%d",
            event_id, threshold,
        )

    # All requested thresholds fired (or one mid-sequence failed and
    # returned above). Persist the snapshot so the next poll diff-checks
    # against the freshest reading.
    update_last_known_capacity(conn, event_id, current)


def run_capacity_poll(
    cfg: CapacityPollerConfig,
    *,
    database: Any | None = None,
    patch_callable: Any = patch_event,
    scrape_callable: Any = _scrape_capacity_with_backoff,
    sleep: Any = _backoff_sleep,
) -> CapacityPollerResult:
    """Run one pass of the capacity poller.

    Spec §4 hourly task. Iterates ``fetch_active_pushed_events`` and
    processes each event independently — a scrape failure for one event
    skips that row but does NOT abort the remaining sweep (spec §4
    "scraper failure handling").

    ``patch_callable`` / ``scrape_callable`` / ``sleep`` are injectable so
    tests can drive the whole flow without network I/O. Production
    callers leave them at their defaults.
    """
    result = CapacityPollerResult()

    def _write_summary(conn: Any) -> None:
        # Spec §11 telemetry: per-poll sweep summary. event_id is None
        # because this is the aggregate, not a per-event row. Numeric
        # counts only — no event ids or titles in the payload.
        write_scout_metric(
            conn,
            event_kind="capacity_poll",
            payload={
                "scanned": result.scanned,
                "scrapes_ok": result.scrapes_ok,
                "scrapes_failed": result.scrapes_failed,
                "patches_ok": result.patches_ok,
                "patches_failed": result.patches_failed,
                "cancellations": result.cancellations,
                "fired_50": result.thresholds_fired[50],
                "fired_25": result.thresholds_fired[25],
                "fired_10": result.thresholds_fired[10],
            },
        )

    if cfg.db_path is not None:
        conn = connect(cfg.db_path)
        try:
            rows = fetch_active_pushed_events(conn, limit=cfg.batch_size)
            result.scanned = len(rows)
            for row in rows:
                _process_event(
                    row=row, cfg=cfg, conn=conn, result=result,
                    sleep=sleep,
                    patch_callable=patch_callable,
                    scrape_callable=scrape_callable,
                )
            _write_summary(conn)
        finally:
            conn.close()
    else:
        if database is None:
            database = open_database()
        try:
            with database.compat_connect() as conn:
                rows = fetch_active_pushed_events(conn, limit=cfg.batch_size)
                result.scanned = len(rows)
                for row in rows:
                    _process_event(
                        row=row, cfg=cfg, conn=conn, result=result,
                        sleep=sleep,
                        patch_callable=patch_callable,
                        scrape_callable=scrape_callable,
                    )
                _write_summary(conn)
        finally:
            database.engine.dispose()
    log.info(
        "capacity poll done scanned=%d scrape_ok=%d scrape_fail=%d "
        "patch_ok=%d patch_fail=%d cancel=%d fired_50=%d fired_25=%d fired_10=%d",
        result.scanned, result.scrapes_ok, result.scrapes_failed,
        result.patches_ok, result.patches_failed, result.cancellations,
        result.thresholds_fired[50],
        result.thresholds_fired[25],
        result.thresholds_fired[10],
    )
    return result
