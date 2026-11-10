"""End-to-end cancellation flow against pytest-httpx (spec §4).

When the Luma page reports the event as cancelled (``eventStatus`` set to
schema.org's ``EventCancelled``), the capacity poller PATCHes Discord
ONCE with a strikethrough title + apology line, then sets ``cancelled_at``
so subsequent polls skip the row.
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
PATCH_URL = "https://discord.com/api/webhooks/123/abc/messages/msg_cancelled"


def _no_sleep(attempt: int, retry_after: float | None) -> None:
    """Skip real time.sleep for tests."""


def _make_scrape(snapshot: CapacitySnapshot):
    def _scrape(url: str, *, sleep=None):
        return snapshot
    return _scrape


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "events.db"
    c = connect(db)
    apply_pending(c, MIG_DIR)
    c.close()
    return db


def _insert_pushed_event(
    db_path: Path,
    *,
    message_id: str = "msg_cancelled",
    title: str = "AI Talk",
) -> int:
    c = connect(db_path)
    try:
        now_iso = datetime.now(UTC).isoformat(timespec="seconds")
        start_at = (datetime.now(UTC) + timedelta(days=3)).isoformat()
        cur = c.execute(
            """INSERT INTO events
                (source, url, title, start_at, fit_score, scoring_status,
                 first_seen, last_seen, discord_push_message_id,
                 last_known_capacity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("luma", "https://lu.ma/cancelled", title, start_at, 0.92, "scored",
             now_iso, now_iso, message_id, 50),
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


def test_cancelled_event_patches_strikethrough_and_sets_cancelled_at(
    db_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """Spec §4 cancellation branch."""
    event_id = _insert_pushed_event(db_path)
    httpx_mock.add_response(
        url=PATCH_URL, method="PATCH",
        status_code=200, json={"id": "msg_cancelled"},
    )

    cfg = CapacityPollerConfig(db_path=db_path, webhook_url=WEBHOOK)
    result = run_capacity_poll(
        cfg,
        scrape_callable=_make_scrape(
            CapacitySnapshot(capacity_remaining=None, cancelled=True),
        ),
        sleep=_no_sleep,
    )

    sent = httpx_mock.get_requests()
    assert len(sent) == 1
    embed = json.loads(sent[0].content)["embeds"][0]
    # The title is wrapped in Discord markdown strikethrough (``~~...~~``)
    # so the channel sees the row was retracted by the host.
    assert embed["title"].startswith("~~")
    assert embed["title"].endswith("~~")
    assert "AI Talk" in embed["title"]
    # The apology banner is the first line of the description.
    assert embed["description"].splitlines()[0].startswith("❌")
    assert "cancelled" in embed["description"].lower()

    # ``cancelled_at`` is set and the result counter incremented.
    refreshed = _row(db_path, event_id)
    assert refreshed["cancelled_at"] is not None
    assert result.cancellations == 1
    assert result.patches_ok == 1


def test_cancelled_event_does_not_patch_again_on_next_poll(
    db_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """Single-fire: ``cancelled_at`` filter excludes the row from re-scans."""
    event_id = _insert_pushed_event(db_path)
    # First poll: PATCH the apology.
    httpx_mock.add_response(
        url=PATCH_URL, method="PATCH",
        status_code=200, json={"id": "msg_cancelled"},
    )

    cfg = CapacityPollerConfig(db_path=db_path, webhook_url=WEBHOOK)
    run_capacity_poll(
        cfg,
        scrape_callable=_make_scrape(
            CapacitySnapshot(capacity_remaining=None, cancelled=True),
        ),
        sleep=_no_sleep,
    )

    # Confirm cancelled_at is set after the first poll.
    refreshed = _row(db_path, event_id)
    assert refreshed["cancelled_at"] is not None

    # Second poll: no new PATCH endpoint registered. pytest-httpx fails
    # the test if any HTTP call is issued — proving the row was filtered
    # out by the scan rather than re-PATCHed.
    result2 = run_capacity_poll(
        cfg,
        scrape_callable=_make_scrape(
            CapacitySnapshot(capacity_remaining=None, cancelled=True),
        ),
        sleep=_no_sleep,
    )
    assert result2.scanned == 0
    assert result2.cancellations == 0


def test_cancellation_patch_failure_leaves_cancelled_at_unset(
    db_path: Path, httpx_mock: HTTPXMock,
) -> None:
    """A 503 cancellation PATCH must NOT set ``cancelled_at``.

    Same PATCH-first contract as the capacity ticker: if the apology
    didn't reach Discord, the row stays in the scan so the next poll
    retries.
    """
    event_id = _insert_pushed_event(db_path)
    httpx_mock.add_response(
        url=PATCH_URL, method="PATCH", status_code=503,
    )

    cfg = CapacityPollerConfig(db_path=db_path, webhook_url=WEBHOOK)
    result = run_capacity_poll(
        cfg,
        scrape_callable=_make_scrape(
            CapacitySnapshot(capacity_remaining=None, cancelled=True),
        ),
        sleep=_no_sleep,
    )

    refreshed = _row(db_path, event_id)
    assert refreshed["cancelled_at"] is None
    assert result.cancellations == 0
    assert result.patches_failed == 1
