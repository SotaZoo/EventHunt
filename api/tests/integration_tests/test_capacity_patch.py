"""End-to-end capacity PATCH flow against pytest-httpx (spec §4).

Drives the full poller: real SQLite via ``apply_pending``, mocked Luma
scrape, REAL Discord PATCH via ``patch_event`` hitting the
pytest-httpx-mocked endpoint. The threshold-crossing PATCH must land
at ``/webhooks/{...}/messages/{message_id}`` and toggle the
corresponding ``cap_<X>_fired`` column to 1.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from api.integrations.scrapers.luma import CapacitySnapshot
from api.repositories.events import connect
from api.repositories.migrate import apply_pending
from api.tasks.capacity_poller import (
    CapacityPollerConfig,
    run_capacity_poll,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"

WEBHOOK = "https://discord.com/api/webhooks/123/abc"
PATCH_URL = "https://discord.com/api/webhooks/123/abc/messages/msg_123"


def _no_sleep(attempt: int, retry_after: float | None) -> None:
    """Skip real time.sleep in backoff for tests."""


def _make_scrape(snapshot: CapacitySnapshot):
    def _scrape(url: str, *, sleep=None):
        return snapshot
    return _scrape


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Migrated, empty SQLite DB."""
    db = tmp_path / "events.db"
    c = connect(db)
    apply_pending(c, MIG_DIR)
    c.close()
    return db


def _insert_pushed_event(
    db_path: Path,
    *,
    url: str = "https://lu.ma/test",
    title: str = "AI Hackathon",
    message_id: str = "msg_123",
    last_known_capacity: int = 100,
    cap_50_fired: int = 0,
) -> int:
    c = connect(db_path)
    try:
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        start_at = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        cur = c.execute(
            """INSERT INTO events
                (source, url, title, start_at, fit_score, scoring_status,
                 first_seen, last_seen, discord_push_message_id,
                 last_known_capacity, cap_50_fired)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("luma", url, title, start_at, 0.9, "scored",
             now_iso, now_iso, message_id, last_known_capacity, cap_50_fired),
        )
        return int(cur.lastrowid)
    finally:
        c.close()


def _row(db_path: Path, event_id: int) -> sqlite3.Row:
    c = connect(db_path)
    try:
        return c.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,),
        ).fetchone()
    finally:
        c.close()


def test_capacity_drop_below_50_patches_discord_and_sets_column(
    db_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """Spec §4 end-to-end: 100 → 40 fires the 50 band PATCH."""
    event_id = _insert_pushed_event(
        db_path, last_known_capacity=100, cap_50_fired=0,
    )
    httpx_mock.add_response(
        url=PATCH_URL, method="PATCH",
        status_code=200, json={"id": "msg_123"},
    )

    cfg = CapacityPollerConfig(db_path=db_path, webhook_url=WEBHOOK)
    result = run_capacity_poll(
        cfg,
        scrape_callable=_make_scrape(
            CapacitySnapshot(capacity_remaining=40, cancelled=False),
        ),
        sleep=_no_sleep,
    )

    # The PATCH landed at the right URL with the right embed shape.
    sent = httpx_mock.get_requests()
    assert len(sent) == 1
    assert str(sent[0].url) == PATCH_URL
    body = json.loads(sent[0].content)
    assert "embeds" in body and len(body["embeds"]) == 1
    embed = body["embeds"][0]
    # The capacity-ticker prefix line uses the wording locked by the
    # renderer (``⚠️ now ~X spots left.``).
    assert "⚠️" in embed["description"]
    assert "40 spots left" in embed["description"]
    # The rest of the embed (title, time line) is intact.
    assert embed["title"] == "⚡ AI Hackathon"
    assert embed["url"] == "https://lu.ma/test"

    # DB state: cap_50_fired flipped; cap_25_fired and cap_10_fired
    # untouched; last_known_capacity refreshed.
    refreshed = _row(db_path, event_id)
    assert refreshed["cap_50_fired"] == 1
    assert refreshed["cap_25_fired"] == 0
    assert refreshed["cap_10_fired"] == 0
    assert refreshed["last_known_capacity"] == 40

    # Result counters reflect the single PATCH.
    assert result.scanned == 1
    assert result.patches_ok == 1
    assert result.thresholds_fired[50] == 1
    assert result.thresholds_fired[25] == 0


def test_capacity_collapse_to_8_fires_three_patches_in_order(
    db_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """100 → 8: fire 50, then 25, then 10 — three PATCHes, in order."""
    event_id = _insert_pushed_event(db_path, last_known_capacity=100)
    # Three sequential 200 OK responses for the three PATCHes.
    for _ in range(3):
        httpx_mock.add_response(
            url=PATCH_URL, method="PATCH",
            status_code=200, json={"id": "msg_123"},
        )

    cfg = CapacityPollerConfig(db_path=db_path, webhook_url=WEBHOOK)
    result = run_capacity_poll(
        cfg,
        scrape_callable=_make_scrape(
            CapacitySnapshot(capacity_remaining=8, cancelled=False),
        ),
        sleep=_no_sleep,
    )

    sent = httpx_mock.get_requests()
    assert len(sent) == 3
    # All three PATCHes target the same message id; the embed prefix line
    # encodes the threshold via its spot count (always 8 — the current
    # capacity), so we don't assert per-threshold text here. The DB state
    # below proves all three columns flipped.
    for req in sent:
        assert str(req.url) == PATCH_URL
        body = json.loads(req.content)
        assert "8 spots left" in body["embeds"][0]["description"]

    refreshed = _row(db_path, event_id)
    assert refreshed["cap_50_fired"] == 1
    assert refreshed["cap_25_fired"] == 1
    assert refreshed["cap_10_fired"] == 1
    assert refreshed["last_known_capacity"] == 8
    assert result.thresholds_fired == {50: 1, 25: 1, 10: 1}


def test_capacity_drop_above_50_does_not_patch(
    db_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """100 → 60: no threshold crossed; no PATCH. Only ``last_known_capacity``
    is refreshed.
    """
    event_id = _insert_pushed_event(db_path, last_known_capacity=100)
    # No PATCH endpoint registered: pytest-httpx fails the test if any
    # HTTP call is issued.

    cfg = CapacityPollerConfig(db_path=db_path, webhook_url=WEBHOOK)
    result = run_capacity_poll(
        cfg,
        scrape_callable=_make_scrape(
            CapacitySnapshot(capacity_remaining=60, cancelled=False),
        ),
        sleep=_no_sleep,
    )

    refreshed = _row(db_path, event_id)
    assert refreshed["cap_50_fired"] == 0
    assert refreshed["last_known_capacity"] == 60
    assert result.patches_ok == 0
    assert result.thresholds_fired[50] == 0


def test_capacity_patch_failure_leaves_column_unset(
    db_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """A 503 PATCH must NOT set ``cap_50_fired`` (PATCH-first contract)."""
    event_id = _insert_pushed_event(db_path, last_known_capacity=100)
    httpx_mock.add_response(url=PATCH_URL, method="PATCH", status_code=503)

    cfg = CapacityPollerConfig(db_path=db_path, webhook_url=WEBHOOK)
    result = run_capacity_poll(
        cfg,
        scrape_callable=_make_scrape(
            CapacitySnapshot(capacity_remaining=40, cancelled=False),
        ),
        sleep=_no_sleep,
    )

    refreshed = _row(db_path, event_id)
    assert refreshed["cap_50_fired"] == 0
    # The poll bailed before persisting capacity (so the next poll re-fires).
    assert refreshed["last_known_capacity"] == 100
    assert result.patches_failed == 1
    assert result.thresholds_fired[50] == 0
