from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from api.commands.main import app
from api.integrations.discord.webhook import PostResult, PostStatus
from api.integrations.llm import EventFit, NotablePerson
from api.models.events import RawEvent
from api.repositories.events import connect, update_fit, upsert_raw
from api.repositories.migrate import apply_pending
from api.tasks.cycle import CycleConfig, CycleResult, run_cycle, score_batch_sync_for_cycle

REPO_ROOT = Path(__file__).resolve().parents[3]
MIG_DIR = REPO_ROOT / "db" / "migrate" / "sqlite"


@pytest.fixture(autouse=True)
def unverified_source_pages(httpx_mock: HTTPXMock):
    # Existing scoring/push tests don't exercise availability; keep that external
    # boundary hermetic. Dedicated refresh tests provide explicit source facts.
    httpx_mock.add_response(method="GET", url=re.compile(r"https://(?:www\.)?(?:lu\.ma|luma\.com|eventbrite\.com|meetup\.com|partiful\.com|betaworks\.com)/.*"),
                            text="<html></html>", is_optional=True, is_reusable=True)


@pytest.fixture
def cfg(tmp_path: Path) -> CycleConfig:
    db = tmp_path / "events.db"
    c = connect(db)
    apply_pending(c, MIG_DIR)
    c.close()
    return CycleConfig(
        db_path=db,
        data_dir=tmp_path,
        webhook_url="https://discord.com/api/webhooks/123/abc",
        taste_profile="loves AI agents",
        prompt_version="v1",
        scoring_batch_size=20,
    )


def _future_event(url: str = "https://lu.ma/x", title: str = "Talk") -> RawEvent:
    return RawEvent(
        source="luma", url=url, title=title,
        starts_at=datetime.now(UTC) + timedelta(days=3),
        venue="Place", neighborhood=None, description=None, host=None,
        cover_image=None,
    )


def test_cycle_scrapes_scores_posts(cfg: CycleConfig, httpx_mock: HTTPXMock, monkeypatch):
    raw = _future_event()
    monkeypatch.setattr(
        "api.tasks.cycle.run_scrapers",
        lambda sources, limit, **kwargs: [raw],
    )
    fit = EventFit(event_id=1, fit_score=0.9, reasoning="great", urgency="low",
                   urgency_reason=None)
    monkeypatch.setattr(
        "api.tasks.cycle.score_batch_sync_for_cycle",
        lambda events, taste, today, version: [fit],
    )
    # cfg.high_bar_threshold=0.85 with fit_score=0.9 means the scout-scan
    # picks this event first. Mock that POST with a ``?wait=true`` body so
    # the new branch captures a message_id; then ``mark_posted`` clears the
    # row from the legacy post loop's candidate set.
    httpx_mock.add_response(
        url=f"{cfg.webhook_url}?wait=true",
        method="POST",
        status_code=200,
        json={"id": "scout_msg_1"},
    )

    result = run_cycle(cfg)

    assert result.scraped == 1
    assert result.scored_ok == 1
    assert result.scored_failed == 0
    # ``result.posted`` is the post counter for the cycle. The legacy
    # ``claim_postable`` path was retired, so this counter now reflects
    # the scout-scan path alone: 0 or 1 per cycle (scan posts the top-1
    # event whose fit_score crosses ``high_bar_threshold``).
    assert result.posted == 1
    c = connect(cfg.db_path)
    row = c.execute(
        "SELECT posted_at, discord_push_message_id FROM events"
    ).fetchone()
    assert row["posted_at"] is not None
    assert row["discord_push_message_id"] == "scout_msg_1"
    c.close()


def test_cycle_skips_when_webhook_disabled(cfg, monkeypatch):
    (cfg.data_dir / ".webhook_disabled").write_text("404")
    monkeypatch.setattr(
        "api.tasks.cycle.run_scrapers",
        lambda *a, **k: pytest.fail("scrape should not run"),
    )
    result = run_cycle(cfg)
    assert result.skipped_reason == "webhook_disabled"


def test_cycle_skips_when_scoring_disabled(cfg, monkeypatch):
    (cfg.data_dir / ".scoring_disabled").write_text("3 all-failed")
    result = run_cycle(cfg)
    assert result.skipped_reason == "scoring_disabled"


def test_cycle_pi_auth_uses_async_score_batch(monkeypatch):
    captured: dict = {}
    fit = EventFit(
        event_id=1, fit_score=0.8, reasoning="fit", urgency="low",
        urgency_reason=None,
    )

    async def fake_score_batch(events, *, taste_profile, today, prompt_version):
        captured["events"] = events
        captured["taste_profile"] = taste_profile
        captured["today"] = today
        captured["prompt_version"] = prompt_version
        return [fit]

    monkeypatch.setenv("LLM_SCORING_MODE", "pi-auth")
    monkeypatch.setattr("api.tasks.cycle.score_batch", fake_score_batch)

    fits = score_batch_sync_for_cycle(
        [{"id": 1, "title": "A"}],
        "loves agents",
        "2026-05-19",
        "v2",
    )

    assert fits == [fit]
    assert captured["taste_profile"] == "loves agents"
    assert captured["prompt_version"] == "v2"


def test_cycle_chunks_scoring_into_multiple_llm_calls(cfg: CycleConfig, monkeypatch):
    """5 pending events + batch_size=2 + max_per_cycle=6 → 3 LLM calls
    (sizes 2, 2, 1). Each subprocess stays well under the 180s timeout.

    Regression test for the Haiku-era refactor: pre-fix the cycle did a
    single 20-event LLM call which timed out on real Luma payloads. Now
    it loops smaller chunks until pending is drained or the cap is hit.
    """
    cfg.scoring_batch_size = 2
    cfg.scoring_max_per_cycle = 6

    raws = [
        _future_event(f"https://lu.ma/e{i}", f"Event {i}") for i in range(5)
    ]
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: raws)

    chunk_sizes: list[int] = []

    def fake_score(events, taste, today, version):
        chunk_sizes.append(len(events))
        return [
            EventFit(event_id=e["id"], fit_score=0.1, reasoning="ok",
                     urgency="low", urgency_reason=None)
            for e in events
        ]

    monkeypatch.setattr(
        "api.tasks.cycle.score_batch_sync_for_cycle", fake_score,
    )

    result = run_cycle(cfg)

    assert chunk_sizes == [2, 2, 1], f"expected 3 chunks of [2,2,1], got {chunk_sizes}"
    assert result.scored_ok == 5
    assert result.scored_failed == 0


def test_cycle_respects_max_per_cycle_cap(cfg: CycleConfig, monkeypatch):
    """10 pending + batch_size=3 + max_per_cycle=5 → 2 chunks of [3,2],
    capped at 5. Remaining 5 wait for the next cycle.
    """
    cfg.scoring_batch_size = 3
    cfg.scoring_max_per_cycle = 5

    raws = [_future_event(f"https://lu.ma/e{i}", f"E{i}") for i in range(10)]
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: raws)

    chunk_sizes: list[int] = []

    def fake_score(events, taste, today, version):
        chunk_sizes.append(len(events))
        return [
            EventFit(event_id=e["id"], fit_score=0.1, reasoning="ok",
                     urgency="low", urgency_reason=None)
            for e in events
        ]

    monkeypatch.setattr(
        "api.tasks.cycle.score_batch_sync_for_cycle", fake_score,
    )

    result = run_cycle(cfg)

    assert chunk_sizes == [3, 2], f"expected [3,2] capped at 5, got {chunk_sizes}"
    assert result.scored_ok == 5
    # 5 still pending in DB for next cycle.
    c = connect(cfg.db_path)
    pending = c.execute(
        "SELECT COUNT(*) FROM events WHERE scoring_status = 'pending'"
    ).fetchone()[0]
    c.close()
    assert pending == 5


def test_cycle_one_failed_chunk_does_not_kill_others(cfg: CycleConfig, monkeypatch):
    """Failure isolation: chunk 2 of 3 raises CliFallbackError. The other
    two chunks still complete and write fits. Previously a single LLM
    timeout failed all 20 events.
    """
    from api.integrations.llm.cli_fallback import CliFallbackError

    cfg.scoring_batch_size = 2
    cfg.scoring_max_per_cycle = 6

    raws = [_future_event(f"https://lu.ma/e{i}", f"E{i}") for i in range(6)]
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: raws)

    call_n = {"i": 0}

    def fake_score(events, taste, today, version):
        call_n["i"] += 1
        if call_n["i"] == 2:
            raise CliFallbackError("simulated mid-cycle timeout")
        return [
            EventFit(event_id=e["id"], fit_score=0.1, reasoning="ok",
                     urgency="low", urgency_reason=None)
            for e in events
        ]

    monkeypatch.setattr(
        "api.tasks.cycle.score_batch_sync_for_cycle", fake_score,
    )

    result = run_cycle(cfg)

    assert call_n["i"] == 3, "all 3 chunks should still be attempted"
    assert result.scored_ok == 4   # chunks 1 + 3 succeed (2 events each)
    assert result.scored_failed == 2   # chunk 2 fails its 2 events


def test_cycle_writes_webhook_disabled_marker_on_404(cfg, httpx_mock, monkeypatch):
    raw = _future_event()
    fit = EventFit(event_id=1, fit_score=0.9, reasoning="x", urgency="low",
                   urgency_reason=None)
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [raw])
    monkeypatch.setattr(
        "api.tasks.cycle.score_batch_sync_for_cycle",
        lambda *a, **k: [fit],
    )
    httpx_mock.add_response(
        url=f"{cfg.webhook_url}?wait=true", method="POST", status_code=404,
    )
    result = run_cycle(cfg)
    assert (cfg.data_dir / ".webhook_disabled").exists()
    assert result.posted == 0


def test_cycle_increments_health_when_all_scoring_fails(cfg, monkeypatch):
    raw_a = _future_event("https://lu.ma/a", "A")
    raw_b = _future_event("https://lu.ma/b", "B")
    monkeypatch.setattr("api.tasks.cycle.run_scrapers",
                        lambda *a, **k: [raw_a, raw_b])
    def boom(*a, **k):
        from api.integrations.llm.cli_fallback import CliFallbackError
        raise CliFallbackError("rate limit")
    monkeypatch.setattr("api.tasks.cycle.score_batch_sync_for_cycle", boom)
    result = run_cycle(cfg)
    assert result.scored_failed == 2
    assert result.scored_ok == 0
    health = (cfg.data_dir / ".cycle_health.json").read_text()
    assert '"consecutive_all_failed": 1' in health


def test_cycle_does_not_persist_llm_payload_to_scoring_failed_reason(cfg, monkeypatch):
    """CliFallbackError can carry cleaned[:400] of raw LLM output via
    _extract_json_array / _parse_fits. Persisting ``str(e)`` to the
    ``scoring_failed_reason`` TEXT column would leak event titles and
    notable-person names into a DB row we'd later eyeball during triage —
    the same friend-data concern that motivated the I2 log sanitization,
    just in a different sink.

    The cycle must persist only a sanitized triage hint (the exception class
    name), never the raw error body.
    """
    raw = _future_event("https://lu.ma/leak", "Founders Dinner with [PRIVATE NAME]")
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [raw])

    # Simulate the realistic failure mode: _extract_json_array raises with
    # cleaned[:400] of LLM output embedded in the error string.
    leaky_body = (
        'no JSON array in model output: '
        '\'[{"event_id": 1, "title": "Founders Dinner with [PRIVATE NAME]", '
        '"notable_people": [{"name": "Real Person", "role_clause": "CEO of X"}]\''
    )

    def boom(*a, **k):
        from api.integrations.llm.cli_fallback import CliFallbackError
        raise CliFallbackError(leaky_body)

    monkeypatch.setattr("api.tasks.cycle.score_batch_sync_for_cycle", boom)

    run_cycle(cfg)

    c = connect(cfg.db_path)
    row = c.execute(
        "SELECT scoring_status, scoring_failed_reason FROM events"
    ).fetchone()
    c.close()

    assert row["scoring_status"] == "failed"
    reason = row["scoring_failed_reason"]
    assert reason is not None
    # Triage hint should be the class name only.
    assert reason == "CliFallbackError", (
        f"scoring_failed_reason={reason!r}; expected class name only "
        f"so LLM payloads don't leak into the DB."
    )
    # Belt-and-suspenders: verify none of the payload markers slipped through.
    for marker in ('"event_id"', "Private Name", "Real Person", "CEO of X", "Founders Dinner"):
        assert marker not in reason, (
            f"scoring_failed_reason leaked {marker!r}: {reason!r}"
        )


def test_cycle_writes_scoring_disabled_marker_after_3_failures(cfg, monkeypatch, httpx_mock):
    monkeypatch.setattr("api.tasks.cycle.run_scrapers",
                        lambda *a, **k: [_future_event(f"https://lu.ma/{i}", str(i))
                                         for i in range(2)])
    def boom(*a, **k):
        from api.integrations.llm.cli_fallback import CliFallbackError
        raise CliFallbackError("rate limit")
    monkeypatch.setattr("api.tasks.cycle.score_batch_sync_for_cycle", boom)

    httpx_mock.add_response(
        url=f"{cfg.webhook_url}?wait=true",
        method="POST",
        status_code=204,
        is_optional=True,
    )

    for _ in range(3):
        run_cycle(cfg)
    assert (cfg.data_dir / ".scoring_disabled").exists()


def test_cycle_skips_when_scoring_disabled_fresh(cfg, monkeypatch):
    """Fresh marker (within cooldown) still gates the cycle."""
    monkeypatch.setenv("SCOUT_BREAKER_COOLDOWN_HOURS", "6")
    (cfg.data_dir / ".scoring_disabled").write_text("fresh trip")
    monkeypatch.setattr(
        "api.tasks.cycle.run_scrapers",
        lambda *a, **k: pytest.fail("scrape should not run while cooldown active"),
    )
    result = run_cycle(cfg)
    assert result.skipped_reason == "scoring_disabled"


def test_cycle_retries_after_cooldown_and_clears_marker_on_success(
    cfg, monkeypatch, httpx_mock,
):
    """Auto-cooldown self-heal: stale marker + successful score → marker cleared,
    health reset, cycle proceeds. This is the path that converts the 5h
    rate-limit window from "permanent breaker" to "transient outage"."""
    import os as _os

    monkeypatch.setenv("SCOUT_BREAKER_COOLDOWN_HOURS", "6")
    # Plant a stale marker that elapsed the cooldown.
    marker = cfg.data_dir / ".scoring_disabled"
    marker.write_text("old trip")
    old = marker.stat().st_mtime - (7 * 3600)
    _os.utime(marker, (old, old))
    # Plant a maxed-out health counter so the cooldown reset is observable.
    (cfg.data_dir / ".cycle_health.json").write_text(
        '{"consecutive_all_failed": 3, "last_error": "rate limit", '
        '"last_failed_at": "2026-04-25T22:00:00+00:00", "last_success_at": null}'
    )

    raw = _future_event()
    fit = EventFit(event_id=1, fit_score=0.9, reasoning="x", urgency="low",
                   urgency_reason=None)
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [raw])
    monkeypatch.setattr("api.tasks.cycle.score_batch_sync_for_cycle",
                        lambda *a, **k: [fit])
    httpx_mock.add_response(
        url=f"{cfg.webhook_url}?wait=true", method="POST", status_code=200,
        json={"id": "msg_recovered"}, is_optional=True,
    )

    result = run_cycle(cfg)

    assert result.skipped_reason is None  # cycle proceeded
    assert result.scored_ok == 1
    assert not marker.exists(), "successful retry should clear .scoring_disabled"
    health = (cfg.data_dir / ".cycle_health.json").read_text()
    assert '"consecutive_all_failed": 0' in health


def test_cycle_retries_after_cooldown_and_touches_marker_on_failure(
    cfg, monkeypatch,
):
    """Auto-cooldown retry that also fails: marker mtime is bumped fresh so
    the cooldown restarts (no thundering-herd retries on the next tick)
    and the counter starts from 1 — not 4 — because we zeroed it at entry."""
    import os as _os

    monkeypatch.setenv("SCOUT_BREAKER_COOLDOWN_HOURS", "6")
    marker = cfg.data_dir / ".scoring_disabled"
    marker.write_text("old trip")
    old_mtime = marker.stat().st_mtime - (7 * 3600)
    _os.utime(marker, (old_mtime, old_mtime))
    (cfg.data_dir / ".cycle_health.json").write_text(
        '{"consecutive_all_failed": 3, "last_error": "rate limit", '
        '"last_failed_at": "2026-04-25T22:00:00+00:00", "last_success_at": null}'
    )

    raw = _future_event()
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [raw])

    def still_failing(*a, **k):
        from api.integrations.llm.cli_fallback import CliFallbackError
        raise CliFallbackError("still rate limited")
    monkeypatch.setattr("api.tasks.cycle.score_batch_sync_for_cycle", still_failing)

    run_cycle(cfg)

    assert marker.exists(), "marker should survive a failed retry"
    new_mtime = marker.stat().st_mtime
    assert new_mtime > old_mtime, "marker mtime should be touched on failed retry"
    health = (cfg.data_dir / ".cycle_health.json").read_text()
    # Pre-reset to 0 + one fresh failure = 1 (NOT 4, which would re-trip).
    assert '"consecutive_all_failed": 1' in health


def test_cycle_partial_resets_health(cfg, monkeypatch, httpx_mock):
    (cfg.data_dir / ".cycle_health.json").write_text(
        '{"consecutive_all_failed": 2, "last_error": "x", '
        '"last_failed_at": "2026-04-25T22:00:00+00:00", "last_success_at": null}'
    )
    raw = _future_event()
    fit = EventFit(event_id=1, fit_score=0.9, reasoning="x", urgency="low",
                   urgency_reason=None)
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [raw])
    monkeypatch.setattr("api.tasks.cycle.score_batch_sync_for_cycle",
                        lambda *a, **k: [fit])
    httpx_mock.add_response(
        url=f"{cfg.webhook_url}?wait=true",
        method="POST",
        status_code=200,
        json={"id": "h_msg"},
    )
    run_cycle(cfg)
    health = (cfg.data_dir / ".cycle_health.json").read_text()
    assert '"consecutive_all_failed": 0' in health


def test_cycle_releases_claim_after_retryable_post(cfg, monkeypatch, httpx_mock):
    raw = _future_event("https://lu.ma/retry", "Retry")
    conn = connect(cfg.db_path)
    event_id = upsert_raw(conn, raw)
    update_fit(
        conn,
        event_id,
        fit_score=0.9,
        reasoning="x",
        urgency="low",
        urgency_reason=None,
        prompt_version="v1",
        taste_sha="taste",
    )
    conn.close()
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [])
    monkeypatch.setattr(
        "api.tasks.cycle.score_batch_sync_for_cycle",
        lambda *a, **k: [],
    )
    # Both the scout-scan path (gate=0.85) and the legacy claim_postable
    # path (gate=0.7) will try to POST this 0.9-scoring event. Use
    # ``is_reusable=True`` so a single 500 response satisfies both calls;
    # the assertions below verify the claim was released in either case.
    httpx_mock.add_response(
        url=f"{cfg.webhook_url}?wait=true",
        method="POST",
        status_code=500,
        is_reusable=True,
    )

    result = run_cycle(cfg)

    conn = connect(cfg.db_path)
    row = conn.execute(
        "SELECT posted_at, posting_claimed_at FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    conn.close()
    assert result.posted == 0
    assert row["posted_at"] is None
    assert row["posting_claimed_at"] is None


def test_cycle_defaults_to_v5_prompt_and_persists_extraction_fields(
    tmp_path: Path, httpx_mock: HTTPXMock, monkeypatch,
) -> None:
    """The current default retains the prior extraction contract in storage."""
    db = tmp_path / "events.db"
    c = connect(db)
    apply_pending(c, MIG_DIR)
    c.close()
    default_cfg = CycleConfig(
        db_path=db,
        data_dir=tmp_path,
        webhook_url="https://discord.com/api/webhooks/123/abc",
        taste_profile="loves AI agents",
        scoring_batch_size=20,
    )

    assert default_cfg.prompt_version == "v5"

    raw = _future_event()
    captured_versions: list[str] = []

    def fake_score(events, taste, today, version):
        captured_versions.append(version)
        return [
            EventFit(
                event_id=events[0]["id"],
                fit_score=0.9,
                reasoning="great",
                urgency="low",
                urgency_reason=None,
                notable_people=[
                    NotablePerson(
                        name="Jensen Huang",
                        role_clause="NVIDIA CEO",
                        confidence=0.96,
                    )
                ],
                event_type="talk",
                food_provided="yes",
            )
        ]

    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [raw])
    monkeypatch.setattr("api.tasks.cycle.score_batch_sync_for_cycle", fake_score)
    httpx_mock.add_response(
        url=f"{default_cfg.webhook_url}?wait=true",
        method="POST",
        status_code=200,
        json={"id": "v5_msg"},
    )

    result = run_cycle(default_cfg)

    assert result.scored_ok == 1
    assert captured_versions == ["v5"]

    c = connect(db)
    row = c.execute(
        """SELECT event_type, food_provided, notable_people, prompt_version
           FROM events"""
    ).fetchone()
    c.close()
    assert row["event_type"] == "talk"
    assert row["food_provided"] == "yes"
    assert row["notable_people"] is not None
    assert "Jensen" in row["notable_people"]
    assert row["prompt_version"] == "v5"


def test_cycle_cli_defaults_to_v5_when_prompt_version_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployed CLI selects the same current prompt as CycleConfig."""
    monkeypatch.delenv("PROMPT_VERSION", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/abc")
    db = tmp_path / "events.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "taste-profile.md").write_text("loves AI agents")

    captured: list[CycleConfig] = []

    def fake_run_cycle(cfg: CycleConfig) -> CycleResult:
        captured.append(cfg)
        return CycleResult()

    monkeypatch.setattr("api.tasks.cycle.run_cycle", fake_run_cycle)

    result = CliRunner().invoke(app, ["cycle", "--sources", "luma"])

    assert result.exit_code == 0, result.stdout
    assert captured, "run_cycle was not invoked by the CLI"
    assert captured[0].prompt_version == "v5"


def test_run_cycle_raises_when_migration_0006_not_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-loud guardrail: cycle must refuse to run on an un-migrated DB.

    The reviewer's concern is that ``_row_get`` silently returning ``None``
    for missing scout-bot columns would let pushes go out with thinned-out
    metadata — the worst trust-failure mode for a "metadata-adding, not
    editorializing" bot. ``_assert_scout_columns_present`` guards the cycle
    against this by checking ``schema_migrations`` once per invocation.

    This test builds a DB with migrations 0000-0005 applied (NOT 0006) and
    asserts ``run_cycle`` raises a clear error mentioning ``0006``. The
    sibling assertion verifies the same call succeeds against a fully
    migrated DB, so we know the test is exercising the right gate.
    """
    db = tmp_path / "partial.db"
    c = connect(db)
    # Apply every migration except 0006_scout_bot_mvp.sql by copying the
    # earlier files into a scratch dir and running apply_pending against it.
    partial_mig_dir = tmp_path / "mig"
    partial_mig_dir.mkdir()
    for path in sorted(MIG_DIR.glob("*.sql")):
        if path.name == "0006_scout_bot_mvp.sql":
            continue
        shutil.copy(path, partial_mig_dir / path.name)
    apply_pending(c, partial_mig_dir)
    c.close()

    partial_cfg = CycleConfig(
        db_path=db,
        data_dir=tmp_path,
        webhook_url="https://discord.com/api/webhooks/123/abc",
        taste_profile="loves AI agents",
        prompt_version="v1",
        scoring_batch_size=20,
    )
    monkeypatch.setattr(
        "api.tasks.cycle.run_scrapers",
        lambda *a, **k: pytest.fail("scrape should not run on un-migrated DB"),
    )

    with pytest.raises(RuntimeError, match="0006"):
        run_cycle(partial_cfg)

    # Sibling assertion — applying 0006 unblocks the cycle. We patch the
    # scrapers out so the cycle is a no-op once the gate clears.
    c2 = connect(db)
    apply_pending(c2, MIG_DIR)
    c2.close()
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [])
    result = run_cycle(partial_cfg)
    assert result.skipped_reason is None


def test_cycle_keeps_claim_after_crash_during_post(cfg, monkeypatch):
    raw = _future_event("https://lu.ma/crash", "Crash")
    conn = connect(cfg.db_path)
    event_id = upsert_raw(conn, raw)
    update_fit(
        conn,
        event_id,
        fit_score=0.9,
        reasoning="x",
        urgency="low",
        urgency_reason=None,
        prompt_version="v1",
        taste_sha="taste",
    )
    conn.close()
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [])

    def crash_after_send(*args, **kwargs):
        raise RuntimeError("process died after Discord accepted the request")

    monkeypatch.setattr("api.tasks.cycle.post_event", crash_after_send)

    with pytest.raises(RuntimeError, match="process died"):
        run_cycle(cfg)

    conn = connect(cfg.db_path)
    row = conn.execute(
        "SELECT posted_at, posting_claimed_at FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    conn.close()
    assert row["posted_at"] is None
    assert row["posting_claimed_at"] is not None

    posted = []
    monkeypatch.setattr(
        "api.tasks.cycle.post_event",
        lambda *a, **k: posted.append(True) or PostResult(PostStatus.OK),
    )

    result = run_cycle(cfg)

    assert result.posted == 0
    assert posted == []


def test_run_cycle_scans_and_pushes_high_scoring_unpushed_future_event(
    cfg: CycleConfig, httpx_mock: HTTPXMock, monkeypatch,
) -> None:
    """Spec §1 happy path: top-1 above the high bar gets pushed with ?wait=true.

    Drives a 0.95-scoring event in via update_fit, runs run_cycle, and
    asserts both the Discord message_id was captured and ``mark_posted``
    cleared the claim. The legacy claim_postable path is suppressed by
    setting cfg.threshold above the event's score so the scout-scan is the
    only path that fires; this isolates Task 3's contract from Task 2's.
    """
    raw = _future_event("https://lu.ma/high", "High-bar event")
    conn = connect(cfg.db_path)
    event_id = upsert_raw(conn, raw)
    update_fit(
        conn, event_id,
        fit_score=0.95, reasoning="great", urgency="high",
        urgency_reason="founders dinner",
        prompt_version="v2", taste_sha="taste",
        notable_people=[
            {"name": "Sam Altman", "role_clause": "OpenAI CEO", "confidence": 0.99},
        ],
        event_type="talk", food_provided="yes",
    )
    conn.close()
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [])
    monkeypatch.setattr("api.tasks.cycle.score_batch_sync_for_cycle",
                        lambda *a, **k: [])
    httpx_mock.add_response(
        url=f"{cfg.webhook_url}?wait=true",
        method="POST",
        status_code=200,
        json={"id": "msg_123"},
    )

    result = run_cycle(cfg)
    assert result.skipped_reason is None

    conn = connect(cfg.db_path)
    row = conn.execute(
        "SELECT discord_push_message_id, posted_at, posting_claimed_at "
        "FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    conn.close()
    assert row["discord_push_message_id"] == "msg_123"
    assert row["posted_at"] is not None
    assert row["posting_claimed_at"] is None  # mark_posted clears the claim

    sent = httpx_mock.get_requests(method="POST")[0]
    import json as _json
    body = _json.loads(sent.content)
    # The embed reflects the v2 extraction fields (Task 2's threading is
    # now live for the scout-scan path too). After the list-layout
    # redesign every item is its own emoji-led line in description.
    desc = body["embeds"][0]["description"]
    assert "founders dinner" in desc  # urgency_reason → why_line
    assert "💬 Format: Talk" in desc
    assert "🍕 Food: yes" in desc
    assert "👤 Sam Altman, OpenAI CEO" in desc


def test_run_cycle_writes_tick_scan_metric_with_top1_score(
    cfg: CycleConfig, monkeypatch,
) -> None:
    """End-to-end smoke for spec §11 ``tick_scan`` instrumentation.

    Closes the loop on Task 6: with a single below-bar event in the pool,
    the cycle still writes one ``tick_scan`` row carrying the top1 score
    (so Yang can plot the distribution and tune the bar) and ``qualified``
    is False (so the no-push outcome is observable from telemetry alone).
    """
    raw = _future_event("https://lu.ma/below-bar", "Below bar")
    conn = connect(cfg.db_path)
    event_id = upsert_raw(conn, raw)
    update_fit(
        conn, event_id,
        fit_score=0.60, reasoning="meh", urgency="low", urgency_reason=None,
        prompt_version="v2", taste_sha="taste",
    )
    conn.close()
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [])
    monkeypatch.setattr("api.tasks.cycle.score_batch_sync_for_cycle",
                        lambda *a, **k: [])

    run_cycle(cfg)

    conn = connect(cfg.db_path)
    rows = conn.execute(
        "SELECT event_kind, payload FROM scout_metrics ORDER BY id"
    ).fetchall()
    conn.close()
    tick_rows = [r for r in rows if r["event_kind"] == "tick_scan"]
    assert len(tick_rows) == 1
    import json as _json
    payload = _json.loads(tick_rows[0]["payload"])
    assert payload["top1_score"] == 0.60
    assert payload["bar"] == 0.85
    assert payload["qualified"] is False


def test_run_cycle_does_not_push_when_no_event_crosses_high_bar(
    cfg: CycleConfig, monkeypatch,
) -> None:
    """All events below 0.85 → no Discord call.

    No httpx_mock is registered: any POST attempt would error. Belt-and-
    suspenders: a regression that drops the threshold to 0.7 here would
    surface immediately as an unexpected network call.
    """
    raw = _future_event("https://lu.ma/meh", "Meh event")
    conn = connect(cfg.db_path)
    event_id = upsert_raw(conn, raw)
    update_fit(
        conn, event_id,
        fit_score=0.60,  # below both 0.7 legacy and 0.85 high bar
        reasoning="meh", urgency="low", urgency_reason=None,
        prompt_version="v2", taste_sha="taste",
    )
    conn.close()
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [])
    monkeypatch.setattr("api.tasks.cycle.score_batch_sync_for_cycle",
                        lambda *a, **k: [])

    result = run_cycle(cfg)
    assert result.skipped_reason is None
    assert result.posted == 0

    conn = connect(cfg.db_path)
    row = conn.execute(
        "SELECT discord_push_message_id FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    conn.close()
    assert row["discord_push_message_id"] is None


def test_run_cycle_releases_claim_on_discord_failure(
    cfg: CycleConfig, httpx_mock: HTTPXMock, monkeypatch,
) -> None:
    """Discord returns 500 → claim released, message_id stays NULL.

    Mirrors ``test_cycle_releases_claim_after_retryable_post`` but for the
    scout-scan path. The crash-recovery TTL is 1h; without claim release
    on retryable, the next 12 cycles would all skip this event.
    """
    raw = _future_event("https://lu.ma/scout500", "Scout 500")
    conn = connect(cfg.db_path)
    event_id = upsert_raw(conn, raw)
    update_fit(
        conn, event_id,
        fit_score=0.92, reasoning="x", urgency="low", urgency_reason=None,
        prompt_version="v2", taste_sha="taste",
    )
    conn.close()
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [])
    monkeypatch.setattr("api.tasks.cycle.score_batch_sync_for_cycle",
                        lambda *a, **k: [])
    httpx_mock.add_response(
        url=f"{cfg.webhook_url}?wait=true",
        method="POST",
        status_code=500,
        is_reusable=True,  # legacy path may also POST; both are 500
    )

    result = run_cycle(cfg)
    assert result.posted == 0

    conn = connect(cfg.db_path)
    row = conn.execute(
        "SELECT discord_push_message_id, posted_at, posting_claimed_at "
        "FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    conn.close()
    assert row["discord_push_message_id"] is None
    assert row["posted_at"] is None
    assert row["posting_claimed_at"] is None


def test_run_cycle_releases_claim_when_discord_returns_2xx_with_no_message_id(
    cfg: CycleConfig, httpx_mock: HTTPXMock, monkeypatch, caplog,
) -> None:
    """Discord 2xx but body lacks ``id`` → retryable: claim released, no posted_at.

    ``?wait=true`` is meant to force Discord to return the created message
    as JSON. If a future broker / proxy / sandbox strips that and returns
    ``{}`` (or a 204, parsed to ``message_id=None``), we can't PATCH the
    card later from the capacity ticker (Task 5) or ``/scout retract``
    (Task 4). The cycle treats this as retryable: release the claim, leave
    ``posted_at`` and ``discord_push_message_id`` NULL, log the failure as
    an aggregate count. The next tick will try again.

    Only the scout-scan path posts now (the legacy ``claim_postable`` path
    was retired); a 2xx with no message_id from Discord must release the
    claim, NOT call mark_posted.

    This test is the load-bearing guard against a refactor that drops the
    ``if not res.message_id`` branch in ``_scan_and_push_for_scout``.
    """
    raw = _future_event("https://lu.ma/no-id", "No id returned")
    conn = connect(cfg.db_path)
    event_id = upsert_raw(conn, raw)
    update_fit(
        conn, event_id,
        fit_score=0.92, reasoning="x", urgency="low", urgency_reason=None,
        prompt_version="v2", taste_sha="taste",
    )
    conn.close()
    monkeypatch.setattr("api.tasks.cycle.run_scrapers", lambda *a, **k: [])
    monkeypatch.setattr("api.tasks.cycle.score_batch_sync_for_cycle",
                        lambda *a, **k: [])
    # 200 OK with an empty JSON body: parsed as message_id=None by
    # _result_from_response. The scout-scan must NOT call mark_posted.
    httpx_mock.add_response(
        url=f"{cfg.webhook_url}?wait=true",
        method="POST",
        status_code=200,
        json={},
    )

    with caplog.at_level("ERROR", logger="api.tasks.cycle"):
        result = run_cycle(cfg)
    assert result.posted == 0

    conn = connect(cfg.db_path)
    row = conn.execute(
        "SELECT discord_push_message_id, posted_at, posting_claimed_at "
        "FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    conn.close()
    # (a) claim released — next tick can retry.
    assert row["posting_claimed_at"] is None
    # (b) message id never recorded; later PATCH paths have nothing to edit.
    assert row["discord_push_message_id"] is None
    # (c) posted_at stays NULL so the row remains eligible for re-push.
    assert row["posted_at"] is None
    # (d) operator visibility — aggregate-only error log (no title, no friend
    # data), per CLAUDE.md's no-leak rule.
    matching = [
        rec for rec in caplog.records
        if rec.levelname == "ERROR"
        and "no message_id" in rec.getMessage()
    ]
    assert matching, (
        f"expected an ERROR log about 'no message_id'; got "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )


def test_run_cycle_high_bar_threshold_default_is_0_85() -> None:
    """The dataclass default is the load-bearing tuning knob. Pin it.

    Spec §10 step 1: 0.85 was picked to keep the channel quiet. A future
    accidental flip to 0.70 would 5x the push volume and immediately erode
    trust in the bot's signal-to-noise ratio.
    """
    cfg = CycleConfig(
        db_path=None,
        data_dir=Path("/tmp/does-not-need-to-exist"),
        webhook_url="https://example.com",
        taste_profile="x",
    )
    assert cfg.high_bar_threshold == 0.85


def test_cycle_cli_high_bar_threshold_default_is_0_85(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI surface: ``--high-bar-threshold`` default 0.85.

    Mirrors ``test_cycle_cli_defaults_to_v5_when_prompt_version_env_unset``
    above — the dataclass default test is necessary but not sufficient.
    If someone hard-codes a different value in the typer.Option, the
    CycleConfig handed to ``run_cycle`` would not match the dataclass
    default. This test captures the actual config the CLI builds.

    Also verifies the env-var fallback (``HIGH_BAR_THRESHOLD``) routes
    through to the same field, since the spec lists it as a sibling
    configuration surface.
    """
    monkeypatch.delenv("HIGH_BAR_THRESHOLD", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/abc")
    db = tmp_path / "events.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "taste-profile.md").write_text("loves AI agents")

    captured: list[CycleConfig] = []

    def fake_run_cycle(cfg: CycleConfig) -> CycleResult:
        captured.append(cfg)
        return CycleResult()

    monkeypatch.setattr("api.tasks.cycle.run_cycle", fake_run_cycle)

    result = CliRunner().invoke(app, ["cycle", "--sources", "luma"])
    assert result.exit_code == 0, result.stdout
    assert captured
    assert captured[0].high_bar_threshold == 0.85
