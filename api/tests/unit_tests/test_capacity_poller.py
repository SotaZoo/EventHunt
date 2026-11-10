"""Pure-function and idempotency tests for the capacity poller (spec §4).

These tests don't touch the network. They drive the threshold-crossing
function directly and use minimal stub callables to exercise the
PATCH-first idempotency contract documented in
``api/tasks/capacity_poller.py``.

Integration coverage (real PATCH against a pytest-httpx Discord stub +
real SQLite via ``apply_pending``) lives in
``api/tests/integration_tests/test_capacity_patch.py`` and
``test_capacity_cancellation.py``.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api.integrations.discord.webhook import (
    CAPACITY_WARNING_THRESHOLDS,
    PostResult,
    PostStatus,
)
from api.integrations.scrapers.luma import CapacitySnapshot, LumaCapacityFetchError
from api.repositories.events import (
    connect,
    fetch_active_pushed_events,
)
from api.repositories.migrate import apply_pending
from api.tasks.capacity_poller import (
    BASE_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    CapacityPollerConfig,
    CapacityPollerResult,
    _backoff_sleep,
    _process_event,
    _scrape_capacity_with_backoff,
    run_capacity_poll,
    thresholds_to_fire,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"
WEBHOOK = "https://discord.com/api/webhooks/123/abc"


# -- Threshold-crossing logic (pure function) ---------------------------


def _no_flags() -> dict[int, bool]:
    return {t: False for t in CAPACITY_WARNING_THRESHOLDS}


def test_thresholds_to_fire_above_all_bands_fires_nothing() -> None:
    """100 → 60: capacity 60 > all three thresholds (50/25/10)."""
    assert thresholds_to_fire(current_capacity=60, fired_flags=_no_flags()) == []


def test_thresholds_to_fire_crosses_50_only() -> None:
    """100 → 45: capacity 45 ≤ 50 but > 25 — fire only the 50 band."""
    assert thresholds_to_fire(current_capacity=45, fired_flags=_no_flags()) == [50]


def test_thresholds_to_fire_crosses_50_and_25() -> None:
    """100 → 20: capacity 20 ≤ 50 ≤ 25, but > 10 — fire 50 then 25."""
    assert thresholds_to_fire(current_capacity=20, fired_flags=_no_flags()) == [50, 25]


def test_thresholds_to_fire_crosses_all_three() -> None:
    """100 → 8: capacity 8 ≤ all three. Fire 50, 25, 10 in that order."""
    assert thresholds_to_fire(
        current_capacity=8, fired_flags=_no_flags()
    ) == [50, 25, 10]


def test_thresholds_to_fire_already_fired_50_skips_50() -> None:
    """cap_50_fired=1 already → 50 band must not re-fire even when breached."""
    flags = {50: True, 25: False, 10: False}
    # capacity 20 would normally fire [50, 25]; with 50 already fired,
    # only 25 is left.
    assert thresholds_to_fire(current_capacity=20, fired_flags=flags) == [25]


def test_thresholds_to_fire_all_already_fired_returns_empty() -> None:
    flags = {50: True, 25: True, 10: True}
    assert thresholds_to_fire(current_capacity=2, fired_flags=flags) == []


def test_thresholds_to_fire_exactly_at_threshold_fires() -> None:
    """The bar is ``capacity <= threshold`` — exact equality fires."""
    assert thresholds_to_fire(current_capacity=50, fired_flags=_no_flags()) == [50]
    assert thresholds_to_fire(current_capacity=25, fired_flags=_no_flags()) == [50, 25]
    assert thresholds_to_fire(
        current_capacity=10, fired_flags=_no_flags()
    ) == [50, 25, 10]


def test_thresholds_to_fire_zero_capacity_fires_all() -> None:
    """Sold out (capacity 0) fires every unfired band."""
    assert thresholds_to_fire(
        current_capacity=0, fired_flags=_no_flags()
    ) == [50, 25, 10]


# -- DB fixture ---------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "events.db"
    c = connect(db)
    apply_pending(c, MIG_DIR)
    yield c
    c.close()


def _insert_pushed_event(
    conn: sqlite3.Connection,
    *,
    url: str = "https://lu.ma/test",
    title: str = "Test Event",
    message_id: str = "msg_123",
    last_known_capacity: int | None = 100,
    cap_50_fired: int = 0,
    cap_25_fired: int = 0,
    cap_10_fired: int = 0,
    cancelled_at: str | None = None,
    start_at_delta_days: int = 2,
) -> int:
    """Insert a fully-pushed event for capacity-poller tests.

    Defaults paint the row the way the scout-bot writes it after a
    successful push: scoring done, posted, ``discord_push_message_id``
    captured, no thresholds fired yet, future ``start_at``.
    """
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    start_at = (datetime.now(UTC) + timedelta(days=start_at_delta_days)).isoformat()
    cur = conn.execute(
        """INSERT INTO events
            (source, url, title, start_at, fit_score, scoring_status,
             first_seen, last_seen, urgency, urgency_reason,
             discord_push_message_id, last_known_capacity,
             cap_50_fired, cap_25_fired, cap_10_fired, cancelled_at,
             notable_people, event_type, food_provided)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "luma", url, title, start_at, 0.9, "scored", now_iso, now_iso,
            None, None, message_id, last_known_capacity,
            cap_50_fired, cap_25_fired, cap_10_fired, cancelled_at,
            None, None, None,
        ),
    )
    return int(cur.lastrowid)


def _fetch_row(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM events WHERE id = ?", (event_id,),
    ).fetchone()


# -- Stubs for injection -----------------------------------------------


class StubPatch:
    """Records every ``patch_event``-shaped call and returns ``responses[i]``.

    ``responses`` is consumed left-to-right; when exhausted, falls back to
    a generic ``PostStatus.OK`` so callers don't have to enumerate every
    no-op tail call.
    """

    def __init__(self, responses: list[PostResult] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, webhook_url: str, message_id: str, embed: dict) -> PostResult:
        self.calls.append((webhook_url, message_id, dict(embed)))
        if self.responses:
            return self.responses.pop(0)
        return PostResult(PostStatus.OK)


def _make_scrape_stub(snapshot: CapacitySnapshot):
    """Build a ``_scrape_capacity_with_backoff``-shaped callable."""
    def _scrape(url: str, *, sleep=None) -> CapacitySnapshot:
        return snapshot
    return _scrape


def _no_sleep(attempt: int, retry_after: float | None) -> None:
    """Inject this as ``sleep`` to skip the real time.sleep in backoff tests."""


# -- Cancelled-event handling ------------------------------------------


def test_process_event_cancelled_sets_cancelled_at_and_patches_once(
    conn: sqlite3.Connection,
) -> None:
    """Spec §4 cancellation branch: PATCH apology + set ``cancelled_at``."""
    event_id = _insert_pushed_event(conn)
    row = _fetch_row(conn, event_id)
    cfg = CapacityPollerConfig(db_path=None, webhook_url=WEBHOOK)
    result = CapacityPollerResult()
    patch_stub = StubPatch()

    _process_event(
        row=row, cfg=cfg, conn=conn, result=result,
        sleep=_no_sleep,
        patch_callable=patch_stub,
        scrape_callable=_make_scrape_stub(
            CapacitySnapshot(capacity_remaining=None, cancelled=True),
        ),
    )

    # Exactly one cancellation PATCH issued; embed has strikethrough title.
    assert len(patch_stub.calls) == 1
    _, msg_id, embed = patch_stub.calls[0]
    assert msg_id == "msg_123"
    assert embed["title"].startswith("~~") and embed["title"].endswith("~~")
    assert "cancelled" in embed["description"].lower()

    # cancelled_at is set; result counter incremented.
    refreshed = _fetch_row(conn, event_id)
    assert refreshed["cancelled_at"] is not None
    assert result.cancellations == 1
    assert result.patches_ok == 1


def test_cancelled_event_drops_out_of_scan(conn: sqlite3.Connection) -> None:
    """Once ``cancelled_at`` is set, ``fetch_active_pushed_events`` skips it."""
    _insert_pushed_event(
        conn, url="https://lu.ma/cancelled",
        cancelled_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    rows = fetch_active_pushed_events(conn)
    assert rows == []


def test_retracted_event_drops_out_of_scan(conn: sqlite3.Connection) -> None:
    """Z1 regression: retracted events must not appear in the capacity scan.

    Scenario: /scout retract just ran on an event — it set
    ``scoring_status='failed'`` and PATCHed the Discord message to a
    strikethrough+apology. The next hourly capacity poll must NOT re-fetch
    that row, because PATCHing again with a capacity-warning embed would
    overwrite the retraction and re-surface the event to the channel.

    Filter chain in fetch_active_pushed_events:
      cancelled_at IS NULL  — handles Luma-upstream cancellation
      scoring_status='scored'  — handles admin retracts (this test)
    Without the second filter, retracts leak through.
    """
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat(timespec="seconds")
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    # Retracted: pushed (has message id), future, NOT cancelled, but failed.
    conn.execute(
        """INSERT INTO events
            (id, source, url, title, start_at, fit_score, scoring_status,
             scoring_failed_reason, discord_push_message_id,
             first_seen, last_seen)
           VALUES (42, 'luma', 'https://lu.ma/retracted', 'Retracted event',
                   ?, 0.95, 'failed', 'retracted_by_admin', 'msg_retracted',
                   ?, ?)""",
        (future, now_iso, now_iso),
    )
    # Active pushed event the poller SHOULD still see — sanity baseline.
    conn.execute(
        """INSERT INTO events
            (id, source, url, title, start_at, fit_score, scoring_status,
             discord_push_message_id, first_seen, last_seen)
           VALUES (43, 'luma', 'https://lu.ma/active', 'Active event',
                   ?, 0.90, 'scored', 'msg_active', ?, ?)""",
        (future, now_iso, now_iso),
    )
    rows = fetch_active_pushed_events(conn)
    ids = [r["id"] for r in rows]
    assert 42 not in ids, (
        "fetch_active_pushed_events returned a retracted event — capacity "
        "poller will PATCH over the retract message (Z1 regression)"
    )
    assert 43 in ids, "active scored event was filtered out — over-filtering"


# -- Terminal state (past events) --------------------------------------


def test_past_event_not_picked_up_by_scan(conn: sqlite3.Connection) -> None:
    """``start_at < now()`` — terminal state per spec §4 step 6."""
    _insert_pushed_event(
        conn, url="https://lu.ma/past", start_at_delta_days=-2,
    )
    rows = fetch_active_pushed_events(conn)
    assert rows == []


def test_unpushed_event_not_picked_up_by_scan(conn: sqlite3.Connection) -> None:
    """``discord_push_message_id IS NULL`` — never pushed, nothing to PATCH."""
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")
    start_at = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    conn.execute(
        """INSERT INTO events
            (source, url, title, start_at, fit_score, scoring_status,
             first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("luma", "https://lu.ma/unpushed", "T", start_at, 0.9, "scored",
         now_iso, now_iso),
    )
    rows = fetch_active_pushed_events(conn)
    assert rows == []


# -- Multi-threshold + idempotency -------------------------------------


def test_process_event_fires_50_only_when_capacity_drops_to_45(
    conn: sqlite3.Connection,
) -> None:
    event_id = _insert_pushed_event(conn, last_known_capacity=100)
    row = _fetch_row(conn, event_id)
    cfg = CapacityPollerConfig(db_path=None, webhook_url=WEBHOOK)
    result = CapacityPollerResult()
    patch_stub = StubPatch()

    _process_event(
        row=row, cfg=cfg, conn=conn, result=result,
        sleep=_no_sleep,
        patch_callable=patch_stub,
        scrape_callable=_make_scrape_stub(
            CapacitySnapshot(capacity_remaining=45, cancelled=False),
        ),
    )

    assert len(patch_stub.calls) == 1
    refreshed = _fetch_row(conn, event_id)
    assert refreshed["cap_50_fired"] == 1
    assert refreshed["cap_25_fired"] == 0
    assert refreshed["cap_10_fired"] == 0
    assert refreshed["last_known_capacity"] == 45
    assert result.thresholds_fired[50] == 1
    assert result.thresholds_fired[25] == 0


def test_process_event_fires_all_three_in_order_when_capacity_collapses(
    conn: sqlite3.Connection,
) -> None:
    """100 → 8: spec §4 says fire 50, 25, 10 in that order."""
    event_id = _insert_pushed_event(conn, last_known_capacity=100)
    row = _fetch_row(conn, event_id)
    cfg = CapacityPollerConfig(db_path=None, webhook_url=WEBHOOK)
    result = CapacityPollerResult()
    patch_stub = StubPatch()

    _process_event(
        row=row, cfg=cfg, conn=conn, result=result,
        sleep=_no_sleep,
        patch_callable=patch_stub,
        scrape_callable=_make_scrape_stub(
            CapacitySnapshot(capacity_remaining=8, cancelled=False),
        ),
    )

    # Three PATCHes, message id locked, ordered top-down.
    assert len(patch_stub.calls) == 3
    refreshed = _fetch_row(conn, event_id)
    assert refreshed["cap_50_fired"] == 1
    assert refreshed["cap_25_fired"] == 1
    assert refreshed["cap_10_fired"] == 1


def test_process_event_idempotency_skips_already_fired_threshold(
    conn: sqlite3.Connection,
) -> None:
    """cap_50_fired=1 already → 50 band must not re-fire."""
    event_id = _insert_pushed_event(
        conn, last_known_capacity=100, cap_50_fired=1,
    )
    row = _fetch_row(conn, event_id)
    cfg = CapacityPollerConfig(db_path=None, webhook_url=WEBHOOK)
    result = CapacityPollerResult()
    patch_stub = StubPatch()

    # Capacity 20 → would normally fire [50, 25]; with 50 already fired,
    # only 25 fires.
    _process_event(
        row=row, cfg=cfg, conn=conn, result=result,
        sleep=_no_sleep,
        patch_callable=patch_stub,
        scrape_callable=_make_scrape_stub(
            CapacitySnapshot(capacity_remaining=20, cancelled=False),
        ),
    )

    assert len(patch_stub.calls) == 1
    refreshed = _fetch_row(conn, event_id)
    assert refreshed["cap_25_fired"] == 1
    assert refreshed["cap_10_fired"] == 0


# -- PATCH-first crash recovery (the documented cosmetic-dup contract) --


class _SimulatedCrash(Exception):
    """Raised to simulate a process crash between PATCH and DB write."""


def test_patch_first_idempotency_dup_edit_on_crash_between_patch_and_write(
    conn: sqlite3.Connection,
) -> None:
    """Pin the spec §4 cosmetic-dup contract.

    The scenario: PATCH succeeds at Discord, but the process dies before
    ``cap_<threshold>_fired = 1`` is committed. Next poll re-scans the
    event with the column STILL 0, and fires the SAME threshold again —
    cosmetic duplicate edit, but the data is never lost.

    We simulate the crash by monkeypatching the DB write helper to raise
    AFTER PATCH succeeds. After "recovery" (clearing the patch), the
    second poll fires the threshold again and finally writes.
    """
    event_id = _insert_pushed_event(conn, last_known_capacity=100)
    row = _fetch_row(conn, event_id)
    cfg = CapacityPollerConfig(db_path=None, webhook_url=WEBHOOK)
    result = CapacityPollerResult()
    patch_stub = StubPatch()

    # Round 1: simulate a crash by monkeypatching the column-write call.
    import api.tasks.capacity_poller as mod
    original_mark = mod.mark_capacity_threshold_fired
    raised = {"happened": False}

    def _explode(conn, event_id, threshold):
        raised["happened"] = True
        raise _SimulatedCrash("simulated death after Discord PATCH succeeded")

    mod.mark_capacity_threshold_fired = _explode  # type: ignore[assignment]
    try:
        with pytest.raises(_SimulatedCrash):
            _process_event(
                row=row, cfg=cfg, conn=conn, result=result,
                sleep=_no_sleep,
                patch_callable=patch_stub,
                scrape_callable=_make_scrape_stub(
                    CapacitySnapshot(capacity_remaining=45, cancelled=False),
                ),
            )
    finally:
        mod.mark_capacity_threshold_fired = original_mark  # type: ignore[assignment]

    # State after crash: PATCH happened ONCE; column NOT yet 1; the row is
    # inconsistent (Discord shows the warning, DB doesn't know).
    assert raised["happened"]
    assert len(patch_stub.calls) == 1
    refreshed = _fetch_row(conn, event_id)
    assert refreshed["cap_50_fired"] == 0, (
        "PATCH-first contract: the column must NOT be set if the write crashed"
    )

    # Round 2: next poll re-scans, fires the SAME threshold (cosmetic dup
    # edit), and this time the column write succeeds.
    row2 = _fetch_row(conn, event_id)
    result2 = CapacityPollerResult()
    _process_event(
        row=row2, cfg=cfg, conn=conn, result=result2,
        sleep=_no_sleep,
        patch_callable=patch_stub,
        scrape_callable=_make_scrape_stub(
            CapacitySnapshot(capacity_remaining=45, cancelled=False),
        ),
    )

    # Discord saw the 50 PATCH twice (cosmetic dup).
    assert len(patch_stub.calls) == 2
    refreshed = _fetch_row(conn, event_id)
    assert refreshed["cap_50_fired"] == 1


# -- 429 backoff -------------------------------------------------------


def test_scrape_with_backoff_retries_on_429_then_succeeds() -> None:
    """``_scrape_capacity_with_backoff`` exponential-backs-off on 429."""
    attempts = {"n": 0}

    def _fake_fetch(url: str, *, timeout=10.0, client=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise LumaCapacityFetchError(
                "rate-limited: 429 retry_after=0.1", retry_after=0.1,
            )
        return CapacitySnapshot(capacity_remaining=42, cancelled=False)

    sleeps: list[tuple[int, float | None]] = []

    def _capture_sleep(attempt: int, retry_after: float | None) -> None:
        sleeps.append((attempt, retry_after))

    import api.tasks.capacity_poller as mod
    original_fetch = mod.fetch_capacity_for_event
    mod.fetch_capacity_for_event = _fake_fetch  # type: ignore[assignment]
    try:
        snap = _scrape_capacity_with_backoff(
            "https://lu.ma/x", max_retries=5, sleep=_capture_sleep,
        )
    finally:
        mod.fetch_capacity_for_event = original_fetch  # type: ignore[assignment]

    assert snap == CapacitySnapshot(capacity_remaining=42, cancelled=False)
    assert attempts["n"] == 3  # two failures, third call succeeds
    # Backoff was applied between the failures (slept 2x before the
    # successful call), and each sleep honoured the ``retry_after`` hint.
    assert len(sleeps) == 2
    assert all(retry_after == 0.1 for _, retry_after in sleeps)


def test_scrape_with_backoff_eventually_gives_up() -> None:
    """After exhausting retries, the error is re-raised so the poller skips."""
    def _always_429(url: str, *, timeout=10.0, client=None):
        raise LumaCapacityFetchError(
            "rate-limited: 429 retry_after=0.01", retry_after=0.01,
        )

    import api.tasks.capacity_poller as mod
    original_fetch = mod.fetch_capacity_for_event
    mod.fetch_capacity_for_event = _always_429  # type: ignore[assignment]
    try:
        with pytest.raises(LumaCapacityFetchError):
            _scrape_capacity_with_backoff(
                "https://lu.ma/x", max_retries=2, sleep=_no_sleep,
            )
    finally:
        mod.fetch_capacity_for_event = original_fetch  # type: ignore[assignment]


def test_backoff_sleep_caps_at_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exponential growth must clamp at ``MAX_BACKOFF_SECONDS``."""
    slept: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("api.tasks.capacity_poller.time.sleep", _fake_sleep)
    # Huge attempt count → would otherwise produce 2**20 = 1M seconds.
    _backoff_sleep(20, retry_after=None)
    assert len(slept) == 1
    # Capped at MAX_BACKOFF_SECONDS plus up to 0.25s jitter.
    assert MAX_BACKOFF_SECONDS <= slept[0] <= MAX_BACKOFF_SECONDS + 0.25


def test_backoff_sleep_honours_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 ``Retry-After`` of 2s should sleep approximately 2s + jitter."""
    slept: list[float] = []
    monkeypatch.setattr(
        "api.tasks.capacity_poller.time.sleep", lambda s: slept.append(s),
    )
    _backoff_sleep(0, retry_after=2.0)
    assert 2.0 <= slept[0] <= 2.25


# -- Scrape failure for one event doesn't break the sweep ---------------


def test_run_capacity_poll_continues_when_one_event_scrape_fails(
    conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Spec §4: scraper failure for one event must not abort the rest."""
    # Two pushed-and-future events; the FIRST scrape raises, the SECOND
    # returns a clean capacity reading.
    e1 = _insert_pushed_event(
        conn, url="https://lu.ma/will-fail", message_id="msg_1",
        start_at_delta_days=1,
    )
    e2 = _insert_pushed_event(
        conn, url="https://lu.ma/will-succeed", message_id="msg_2",
        last_known_capacity=100, start_at_delta_days=2,
    )
    conn.close()  # release; run_capacity_poll opens its own connection

    cfg = CapacityPollerConfig(
        db_path=tmp_path / "events.db", webhook_url=WEBHOOK,
    )

    calls: list[str] = []

    def _scrape(url: str, *, sleep=None):
        calls.append(url)
        if "will-fail" in url:
            raise LumaCapacityFetchError("server error: 503")
        return CapacitySnapshot(capacity_remaining=40, cancelled=False)

    patch_stub = StubPatch()
    result = run_capacity_poll(
        cfg,
        patch_callable=patch_stub,
        scrape_callable=_scrape,
        sleep=_no_sleep,
    )

    # Both events were attempted; the failing one was logged-and-skipped;
    # the succeeding one fired the 50 threshold.
    assert result.scanned == 2
    assert result.scrapes_failed == 1
    assert result.scrapes_ok == 1
    assert result.thresholds_fired[50] == 1
    assert len(patch_stub.calls) == 1
    # The PATCH was issued for the message_id of the SUCCESSFUL event.
    assert patch_stub.calls[0][1] == "msg_2"

    # Persisted state: e1 unchanged (no scrape success), e2 has cap_50_fired.
    c2 = connect(tmp_path / "events.db")
    try:
        r1 = _fetch_row(c2, e1)
        r2 = _fetch_row(c2, e2)
    finally:
        c2.close()
    assert r1["cap_50_fired"] == 0
    assert r2["cap_50_fired"] == 1
    assert r2["last_known_capacity"] == 40


def test_run_capacity_poll_skips_when_patch_fails(
    conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """A failing PATCH must NOT set the column (PATCH-first contract)."""
    event_id = _insert_pushed_event(conn, last_known_capacity=100)
    conn.close()

    cfg = CapacityPollerConfig(
        db_path=tmp_path / "events.db", webhook_url=WEBHOOK,
    )

    patch_stub = StubPatch(
        responses=[PostResult(PostStatus.RETRYABLE, detail="503")],
    )
    result = run_capacity_poll(
        cfg,
        patch_callable=patch_stub,
        scrape_callable=_make_scrape_stub(
            CapacitySnapshot(capacity_remaining=45, cancelled=False),
        ),
        sleep=_no_sleep,
    )

    assert result.patches_failed == 1
    assert result.thresholds_fired[50] == 0

    c2 = connect(tmp_path / "events.db")
    try:
        refreshed = _fetch_row(c2, event_id)
    finally:
        c2.close()
    assert refreshed["cap_50_fired"] == 0, (
        "PATCH-first: column must remain 0 when the PATCH didn't succeed"
    )


def test_thresholds_remaining_after_50_fired_keeps_25_and_10() -> None:
    """A second poll with 50 already fired and capacity still ≤ 25 fires 25."""
    flags = {50: True, 25: False, 10: False}
    assert thresholds_to_fire(current_capacity=20, fired_flags=flags) == [25]


def test_base_backoff_constant_is_positive() -> None:
    """Spec sanity: backoff base + cap are valid (no zero-delay loop)."""
    assert BASE_BACKOFF_SECONDS > 0
    assert MAX_BACKOFF_SECONDS >= BASE_BACKOFF_SECONDS
