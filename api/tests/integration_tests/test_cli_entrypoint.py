from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from api.commands.main import app
from api.repositories.events import connect, mark_scoring_failed
from api.tests.fixtures.web_helpers import add_scored_event, migrated_db


def test_cli_help_loads():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Usage" in result.output


def test_reset_breaker_help_lists_subcommand():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "reset-breaker" in result.output


@pytest.mark.parametrize(("source", "exit_code"), [("broken", 1), ("all", 0)])
def test_scrape_fails_only_when_no_source_completes(tmp_path, monkeypatch, source, exit_code):
    db = migrated_db(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(db))

    class BrokenScraper:
        def scrape(self, *, limit):
            raise RuntimeError("collector unavailable")

    class EmptyScraper:
        def scrape(self, *, limit):
            return []

    monkeypatch.setattr("api.commands.main.SCRAPERS", {
        "broken": BrokenScraper, "empty": EmptyScraper,
    })

    result = CliRunner().invoke(app, ["scrape", "--source", source])

    assert result.exit_code == exit_code, result.output
    assert "broken failed: collector unavailable" in result.output


def test_reset_breaker_clears_marker_and_zeros_health(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "events.db"))
    marker = tmp_path / ".scoring_disabled"
    marker.write_text("3 cycles\nrate limit")
    health = tmp_path / ".cycle_health.json"
    health.write_text(json.dumps({
        "consecutive_all_failed": 3,
        "last_error": "rate limit",
        "last_failed_at": "2026-05-14T01:00:00+00:00",
        "last_success_at": None,
    }))

    result = CliRunner().invoke(app, ["reset-breaker", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert not marker.exists()
    assert json.loads(health.read_text())["consecutive_all_failed"] == 0
    assert "cleared" in result.output


def test_reset_breaker_idempotent_when_breaker_not_engaged(tmp_path: Path, monkeypatch):
    """Running with nothing to clear must not crash. In sqlite mode the
    health-file write still happens (cheap idempotent zero), so the output
    reports at least one item; the marker is absent and not in the report."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "events.db"))
    result = CliRunner().invoke(app, ["reset-breaker", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    # Marker was never present → never listed as cleared.
    assert ".scoring_disabled" not in result.output
    assert (tmp_path / ".cycle_health.json").exists()
    assert json.loads((tmp_path / ".cycle_health.json").read_text())["consecutive_all_failed"] == 0


def _add_failed_event(db: Path) -> int:
    event_id = add_scored_event(db, source="luma", days_from_now=3)
    conn = connect(db)
    try:
        mark_scoring_failed(conn, event_id, "CliFallbackError")
    finally:
        conn.close()
    return event_id


def _scoring_status(db: Path, event_id: int) -> str:
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT scoring_status FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        return str(row["scoring_status"])
    finally:
        conn.close()


def test_requeue_scoring_failures_dry_run_does_not_mutate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = migrated_db(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(db))
    event_id = _add_failed_event(db)

    result = CliRunner().invoke(app, ["requeue-scoring-failures"])

    assert result.exit_code == 0, result.output
    assert "DRY RUN: 1 row(s)" in result.output
    assert _scoring_status(db, event_id) == "failed"


def test_requeue_scoring_failures_apply_mutates_safe_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = migrated_db(tmp_path)
    monkeypatch.setenv("DATABASE_PATH", str(db))
    event_id = _add_failed_event(db)

    result = CliRunner().invoke(app, ["requeue-scoring-failures", "--apply"])

    assert result.exit_code == 0, result.output
    assert "requeued 1 scoring failure row" in result.output
    assert _scoring_status(db, event_id) == "pending"
