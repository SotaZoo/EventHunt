"""Card/agent facts observed through migrated public discovery interfaces."""
import json
import shutil
import subprocess
from datetime import UTC, datetime

import pytest
from freezegun import freeze_time
from mcp.client import Client
from starlette.testclient import TestClient

from api.app import create_app
from api.mcp.server import create_event_scout_mcp
from api.models.events import RawEvent
from api.repositories.events import connect, fetch_pending_scoring, update_fit, upsert_raw
from api.repositories.migrate import apply_pending, migration_dir_for_dialect
from api.services.event_catalog import find_events
from api.tasks.cycle import CycleConfig, run_cycle
from api.tests.fixtures.web_helpers import add_user, migrated_db

HIGHLIGHT = {
    "text": "Panel with Dick Hardt, OAuth 2.0 co-author",
    "evidence": "Dick Hardt, co-author of OAuth 2.0",
}
DEADLINE = {
    "kind": "deadline", "text": "Register by Oct 9, 2026 at 6 PM EDT",
    "evidence": "RSVP closes October 9, 2026 at 6 PM EDT",
    "deadline_at": "2026-10-09T18:00:00-04:00",
    "deadline_evidence": "October 9, 2026 at 6 PM EDT",
}
SOURCE = "Dick Hardt, co-author of OAuth 2.0. RSVP closes October 9, 2026 at 6 PM EDT."


def annotated_event(db, *, description=SOURCE, highlight=HIGHLIGHT, urgency_note=DEADLINE):
    conn = connect(db)
    try:
        event_id = upsert_raw(conn, RawEvent(
            source="luma", url="https://example.test/secure-agents", title="Secure agents",
            starts_at=datetime(2026, 10, 10, 18, tzinfo=UTC),
            venue="A lab", host="Builders", description=description,
        ))
        update_fit(conn, event_id, fit_score=.91, reasoning="Honest shared explanation",
                   urgency="low", urgency_reason=None, prompt_version="v3", taste_sha="taste",
                   highlight=highlight, urgency_note=urgency_note)
        return event_id
    finally:
        conn.close()


@pytest.mark.parametrize("highlight,note", [
    ({"text": "A famous speaker", "evidence": "Famous speaker guaranteed"}, DEADLINE),
    ({"text": "", "evidence": SOURCE}, DEADLINE),
    ({"text": "x" * 201, "evidence": SOURCE}, DEADLINE),
    (HIGHLIGHT, {**DEADLINE, "evidence": "Registration ends soon"}),
    (HIGHLIGHT, {**DEADLINE, "kind": "event_date"}),
    (None, None),
])
@freeze_time("2026-10-06T20:00:00Z")
def test_invalid_or_unsupported_annotations_do_not_replace_honest_ranking(tmp_path, highlight, note):
    db = migrated_db(tmp_path)
    add_user(db)
    annotated_event(db, highlight=highlight, urgency_note=note)
    event = web_events(db)[0]
    assert event["score"] == .91
    assert event["highlight"] == (HIGHLIGHT["text"] if highlight == HIGHLIGHT else None)
    assert event["urgencyNote"] == (DEADLINE["text"] if note == DEADLINE else None)


@pytest.mark.parametrize("observed,deadline", [
    ("2026-10-05T20:00:00+00:00", "2026-10-09T18:00:00-04:00"),
    ("2026-10-05T19:59:59+00:00", "2026-10-09T18:00:00-04:00"),
    ("2026-10-06T20:01:00+00:00", "2026-10-09T18:00:00-04:00"),
])
@freeze_time("2026-10-06T20:00:00Z")
def test_stale_future_observation_and_non_actionable_deadlines_are_omitted(tmp_path, observed, deadline):
    db = migrated_db(tmp_path)
    add_user(db)
    event_id = annotated_event(db, urgency_note={**DEADLINE, "deadline_at": deadline})
    conn = connect(db)
    try:
        conn.execute("UPDATE events SET source_content_checked_at = ? WHERE id = ?", (observed, event_id))
    finally:
        conn.close()
    event = web_events(db)[0]
    assert event["highlight"] == HIGHLIGHT["text"]
    assert event["urgencyNote"] is None
    assert event["urgencyNoteExpiresAt"] is None


@freeze_time("2026-10-06T20:00:00Z")
def test_scarcity_is_an_observation_not_a_live_capacity_promise(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    annotated_event(db, description=SOURCE + " Limited spots available.", urgency_note={
        "kind": "scarcity", "text": "Limited spots", "evidence": "Limited spots available.",
    })
    event = web_events(db)[0]
    assert event["urgencyNote"] == "At last check: Limited spots"


@freeze_time("2026-10-06T20:00:00Z")
def test_explicit_future_registration_deadline_can_fall_after_event_start(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    note = {"kind": "deadline", "text": "Register by Oct 11, 2026 at 6 PM EDT",
            "evidence": "Late registration closes October 11, 2026 at 6 PM EDT",
            "deadline_at": "2026-10-11T18:00:00-04:00",
            "deadline_evidence": "October 11, 2026 at 6 PM EDT"}
    annotated_event(db, description="Late registration closes October 11, 2026 at 6 PM EDT.",
                    highlight=None, urgency_note=note)
    assert web_events(db)[0]["urgencyNote"] == note["text"]
    conn = connect(db)
    try:
        result = find_events(conn, start="2026-10-06T00:00:00Z", end="2026-10-13T00:00:00Z")
        assert result.events[0].urgency_note == note["text"]
    finally:
        conn.close()


@pytest.mark.parametrize("legacy_hash", [False, True])
@freeze_time("2026-10-06T20:00:00Z")
def test_late_score_cannot_overwrite_a_newer_source_revision(tmp_path, legacy_hash):
    db = migrated_db(tmp_path)
    add_user(db)
    event_id = annotated_event(db)
    conn = connect(db)
    try:
        if legacy_hash:
            conn.execute("UPDATE events SET fit_input_hash=NULL WHERE id=?", (event_id,))
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        upsert_raw(conn, RawEvent(source="luma", url=row["url"], title=row["title"],
                   starts_at=datetime.fromisoformat(row["start_at"]), host=row["host"], venue=row["venue"],
                   description=SOURCE + " New speakers confirmed."))
        assert not update_fit(conn, event_id, fit_score=.91, reasoning="Outdated result",
                              urgency="low", urgency_reason=None, prompt_version="v3", taste_sha="taste",
                              highlight=HIGHLIGHT, urgency_note=DEADLINE,
                              expected_input_hash=row["fit_input_hash"])
    finally:
        conn.close()
    event = web_events(db)[0]
    assert event["score"] is None
    assert event["highlight"] is None


@freeze_time("2026-10-06T20:00:00Z")
def test_partial_meetup_and_hash_upgrade_preserve_existing_score(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    conn = connect(db)
    raw = RawEvent(source="meetup", url="https://example.test/meetup", title="Secure agents",
                   starts_at=datetime(2026, 10, 10, 18, tzinfo=UTC), description=SOURCE)
    try:
        event_id = upsert_raw(conn, raw)
        update_fit(conn, event_id, fit_score=.91, reasoning="Shared fit", urgency="low",
                   urgency_reason=None, prompt_version="v3", taste_sha="taste", highlight=HIGHLIGHT)
        conn.execute("UPDATE events SET fit_input_hash = 'old-format' WHERE id = ?", (event_id,))
        upsert_raw(conn, raw.model_copy(update={"description": None}))
        assert not fetch_pending_scoring(conn)
    finally:
        conn.close()
    event = web_events(db)[0]
    assert event["score"] == .91
    assert event["highlight"] == HIGHLIGHT["text"]


@freeze_time("2026-10-06T20:00:00Z")
def test_partial_refresh_cannot_renew_a_stale_urgency_observation(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    conn = connect(db)
    raw = RawEvent(source="meetup", url="https://example.test/meetup", title="Secure agents",
                   starts_at=datetime(2026, 10, 10, 18, tzinfo=UTC), description=SOURCE)
    try:
        with freeze_time("2026-10-05T19:59:00Z"):
            event_id = upsert_raw(conn, raw)
            update_fit(conn, event_id, fit_score=.91, reasoning="Shared fit", urgency="low",
                       urgency_reason=None, prompt_version="v3", taste_sha="taste",
                       highlight=HIGHLIGHT, urgency_note=DEADLINE)
        upsert_raw(conn, raw.model_copy(update={"description": None}))
        assert web_events(db)[0]["urgencyNote"] is None
        upsert_raw(conn, raw)
        assert web_events(db)[0]["urgencyNote"] == DEADLINE["text"]
    finally:
        conn.close()


def web_events(db):
    client = TestClient(create_app(db_path=db, session_secret="test-secret"))
    client.post("/login", data={"invite_code": "yang-secret"})
    return client.get("/api/discovery").json()["events"]


@freeze_time("2026-10-07T12:00:00Z")
def test_observation_time_cannot_supply_missing_deadline_context(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    annotated_event(db, description="RSVP closes tomorrow at 6 PM EDT.", highlight=None,
                    urgency_note={"kind": "deadline", "text": "Register by October 8 at 6 PM EDT",
                                  "evidence": "RSVP closes tomorrow at 6 PM EDT",
                                  "deadline_at": "2026-10-08T18:00:00-04:00"})
    event = web_events(db)[0]
    assert event["score"] == .91
    assert event["urgencyNote"] is None
    assert event["urgencyNoteExpiresAt"] is None


@pytest.mark.parametrize("expression", ["tomorrow at 6 PM EDT", "October 8, 2026 at 6 PM EDT"])
@freeze_time("2026-10-07T12:00:00Z")
def test_relative_or_fabricated_time_evidence_cannot_establish_a_deadline(tmp_path, expression):
    db = migrated_db(tmp_path)
    add_user(db)
    annotated_event(db, description="RSVP closes tomorrow at 6 PM EDT.", highlight=None,
                    urgency_note={"kind": "deadline", "text": "Register by October 8",
                                  "evidence": "RSVP closes tomorrow at 6 PM EDT",
                                  "deadline_evidence": expression})
    assert web_events(db)[0]["urgencyNote"] is None


@freeze_time("2026-10-08T12:00:00Z")
def test_rechecking_unchanged_evidence_renews_freshness_not_deadline_meaning(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    with freeze_time("2026-10-06T20:00:00Z"):
        event_id = annotated_event(db)
    assert web_events(db)[0]["urgencyNote"] is None
    conn = connect(db)
    try:
        row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        upsert_raw(conn, RawEvent(source="luma", url=row["url"], title=row["title"],
                   starts_at=datetime.fromisoformat(row["start_at"]), host=row["host"], venue=row["venue"],
                   description=SOURCE))
        result = find_events(conn, start="2026-10-06T00:00:00Z", end="2026-10-13T00:00:00Z")
        assert result.events[0].urgency_note == DEADLINE["text"]
        assert result.events[0].urgency_note_expires_at == "2026-10-09T12:00:00+00:00"
        assert not fetch_pending_scoring(conn)
    finally:
        conn.close()


@freeze_time("2026-10-09T22:00:00Z")
def test_expired_source_deadline_stays_expired_after_rechecking(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    annotated_event(db)
    assert web_events(db)[0]["urgencyNote"] is None


@pytest.mark.asyncio
@freeze_time("2026-10-06T20:00:00Z", real_asyncio=True)
async def test_web_and_mcp_return_the_same_positive_highlight_and_action_deadline(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    event_id = annotated_event(db)
    web = web_events(db)[0]
    mcp = create_event_scout_mcp(db_path=db, token="a" * 32, resource_url="http://testserver/mcp")
    async with Client(mcp) as client:
        result = await client.call_tool("find_events", {
            "start": "2026-10-06T00:00:00-04:00",
            "end": "2026-10-13T00:00:00-04:00", "limit": 5,
        })
    machine = result.structured_content["events"][0]
    assert web["id"] == machine["id"] == event_id
    assert web["highlight"] == machine["highlight"] == HIGHLIGHT["text"]
    assert web["urgencyNote"] == machine["urgency_note"] == DEADLINE["text"]
    assert machine["fit_reasoning"] == "Honest shared explanation"
    assert machine["urgency"] == "low"
    assert machine["annotations_scored_at"] is not None
    assert web["urgencyNoteExpiresAt"] == machine["urgency_note_expires_at"] == "2026-10-07T20:00:00+00:00"
    assert result.structured_content["ranking"]["personalized"] is False


@pytest.mark.parametrize(("field", "value"), [
    ("description", SOURCE + " The program changed."),
    ("host", "A different organizer"),
    ("neighborhood", "London"),
    ("attendance_mode", "online"),
])
@freeze_time("2026-10-06T20:00:00Z")
def test_changed_scoring_input_removes_old_score_and_generated_card_facts(tmp_path, field, value):
    db = migrated_db(tmp_path)
    add_user(db)
    annotated_event(db)
    values = {"description": SOURCE, "host": "Builders"}
    values[field] = value
    conn = connect(db)
    try:
        upsert_raw(conn, RawEvent(
            source="luma", url="https://example.test/secure-agents", title="Secure agents",
            starts_at=datetime(2026, 10, 10, 18, tzinfo=UTC), venue="A lab", **values,
        ))
    finally:
        conn.close()
    event = web_events(db)[0]
    assert event["score"] is None
    assert event["highlight"] is None
    assert event["urgencyNote"] is None


@freeze_time("2026-10-06T20:00:00Z")
def test_existing_scores_refresh_through_bounded_real_scoring_boundary(tmp_path, monkeypatch):
    db = migrated_db(tmp_path)
    add_user(db)
    conn = connect(db)
    try:
        for title in ("Agents workshop", "Robotics workshop", "Data workshop"):
            event_id = upsert_raw(conn, RawEvent(
                source="luma", url=f"https://example.test/{title.replace(' ', '-')}",
                title=title, starts_at=datetime(2026, 10, 10, 18, tzinfo=UTC),
                neighborhood="London", attendance_mode="mixed",
                description="Mentors provide live debugging help.",
            ))
            update_fit(conn, event_id, fit_score=.91, reasoning="Original shared fit",
                       urgency="low", urgency_reason=None, prompt_version="v2", taste_sha="taste")
    finally:
        conn.close()


    def external_model(cmd, **kwargs):
        prompt = kwargs["input"]
        events = json.loads(prompt.split("## Events to score\n\n```json\n", 1)[1].split("```", 1)[0])
        assert all(event["neighborhood"] == "London" and event["attendance_mode"] == "mixed"
                   for event in events)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([{
            "event_id": event["id"], "fit_score": .91, "reasoning": "Refreshed shared fit",
            "urgency": "low", "urgency_reason": None,
            "highlight": {"text": "Get debugging help from mentors",
                          "evidence": "Mentors provide live debugging help."},
            "urgency_note": None,
        } for event in events]), stderr="")

    monkeypatch.setenv("LLM_SCORING_MODE", "codex-cli")
    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", external_model)
    cfg = CycleConfig(db_path=db, data_dir=tmp_path, webhook_url="https://example.invalid/disabled",
                      taste_profile="AI engineering", scrape_sources=(),
                      high_bar_threshold=1.01, scoring_batch_size=1, scoring_max_per_cycle=2)
    result = run_cycle(cfg)
    assert result.scored_ok == 2
    assert result.posted == 0
    conn = connect(db)
    try:
        catalog = find_events(conn, start="2026-10-06T00:00:00Z", end="2026-10-13T00:00:00Z")
        assert len(catalog.events) == 3
        assert sum(event.highlight is not None for event in catalog.events) == 2
        assert all(event.fit_score == .91 for event in catalog.events)
    finally:
        conn.close()


@freeze_time("2026-10-06T20:00:00Z")
def test_pending_event_gets_capacity_before_newer_old_prompt_refresh(tmp_path, monkeypatch):
    db = migrated_db(tmp_path)
    add_user(db)
    conn = connect(db)
    try:
        with freeze_time("2026-10-05T20:00:00Z"):
            upsert_raw(conn, RawEvent(source="luma", url="https://example.test/new-event",
                       title="New event", starts_at=datetime(2026, 10, 10, 18, tzinfo=UTC),
                       description="Mentors provide live debugging help."))
        refresh_id = upsert_raw(conn, RawEvent(source="luma", url="https://example.test/old-prompt",
                                title="Old prompt event", starts_at=datetime(2026, 10, 10, 18, tzinfo=UTC)))
        update_fit(conn, refresh_id, fit_score=.91, reasoning="Original shared fit", urgency="low",
                   urgency_reason=None, prompt_version="v2", taste_sha="taste")
    finally:
        conn.close()

    def external_model(cmd, **kwargs):
        prompt = kwargs["input"]
        inputs = json.loads(prompt.split("## Events to score\n\n```json\n", 1)[1].split("```", 1)[0])
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([{
            "event_id": event["id"], "fit_score": .82, "reasoning": "First score", "urgency": "low",
            "urgency_reason": None,
        } for event in inputs]), stderr="")

    monkeypatch.setenv("LLM_SCORING_MODE", "codex-cli")
    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", external_model)
    result = run_cycle(CycleConfig(db_path=db, data_dir=tmp_path, webhook_url="https://example.invalid/disabled",
                      taste_profile="AI engineering", scrape_sources=(), high_bar_threshold=1.01,
                      scoring_batch_size=1, scoring_max_per_cycle=1))
    assert result.scored_ok == 1
    assert {event["title"]: event["score"] for event in web_events(db)} == {
        "New event": .82, "Old prompt event": .91,
    }


@pytest.mark.parametrize("legacy_hash", [False, True])
@freeze_time("2026-10-06T20:00:00Z")
def test_failed_refresh_preserves_old_ranking_and_attempts_each_row_once(tmp_path, monkeypatch, legacy_hash):
    db = migrated_db(tmp_path)
    add_user(db)
    conn = connect(db)
    try:
        event_id = upsert_raw(conn, RawEvent(source="luma", url="https://example.test/workshop",
                             title="Agents workshop", starts_at=datetime(2026, 10, 10, 18, tzinfo=UTC),
                             description="Mentors provide live debugging help."))
        update_fit(conn, event_id, fit_score=.91, reasoning="Original shared fit", urgency="low",
                   urgency_reason=None, prompt_version="v2", taste_sha="taste")
        if legacy_hash:
            conn.execute("UPDATE events SET fit_input_hash=NULL WHERE id=?", (event_id,))
    finally:
        conn.close()
    calls = []

    def unavailable_model(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="temporary model failure")

    monkeypatch.setenv("LLM_SCORING_MODE", "codex-cli")
    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", unavailable_model)
    cfg = CycleConfig(db_path=db, data_dir=tmp_path, webhook_url="https://example.invalid/disabled",
                      taste_profile="AI engineering", scrape_sources=(), high_bar_threshold=1.01,
                      scoring_batch_size=1, scoring_max_per_cycle=5)
    result = run_cycle(cfg)
    assert result.scored_failed == 1
    assert len(calls) == 2  # One batch, with the scorer's existing two attempts.
    event = web_events(db)[0]
    assert event["score"] == .91
    assert event["highlight"] is None


@freeze_time("2026-10-06T20:00:00Z")
def test_partial_model_batch_preserves_omitted_old_score_without_repeat(tmp_path, monkeypatch):
    db = migrated_db(tmp_path)
    add_user(db)
    conn = connect(db)
    try:
        for title in ("Missing result", "Returned result"):
            event_id = upsert_raw(conn, RawEvent(source="luma", title=title,
                                 url=f"https://example.test/{title.replace(' ', '-')}",
                                 starts_at=datetime(2026, 10, 10, 18, tzinfo=UTC)))
            update_fit(conn, event_id, fit_score=.91, reasoning="Original fit", urgency="low",
                       urgency_reason=None, prompt_version="v2", taste_sha="taste")
    finally:
        conn.close()
    calls = []

    def partial_model(cmd, **kwargs):
        calls.append(cmd)
        inputs = json.loads(kwargs["input"].split("## Events to score\n\n```json\n", 1)[1].split("```", 1)[0])
        returned = next(event for event in inputs if event["title"] == "Returned result")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([{
            "event_id": returned["id"], "fit_score": .82, "reasoning": "Refreshed fit",
            "urgency": "low", "urgency_reason": None,
        }]), stderr="")

    monkeypatch.setenv("LLM_SCORING_MODE", "codex-cli")
    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", partial_model)
    result = run_cycle(CycleConfig(db_path=db, data_dir=tmp_path, webhook_url="https://example.invalid/disabled",
                      taste_profile="AI engineering", scrape_sources=(), high_bar_threshold=1.01,
                      scoring_batch_size=2, scoring_max_per_cycle=4))
    assert result.scored_ok == result.scored_failed == 1
    assert len(calls) == 1
    assert {event["title"]: event["score"] for event in web_events(db)} == {
        "Missing result": .91, "Returned result": .82,
    }


@freeze_time("2026-10-06T20:00:00Z")
def test_late_model_failure_keeps_changed_source_pending_for_retry(tmp_path, monkeypatch):
    db = migrated_db(tmp_path)
    add_user(db)
    raw = RawEvent(source="luma", title="Agents workshop", url="https://example.test/workshop",
                   starts_at=datetime(2026, 10, 10, 18, tzinfo=UTC), description="Original source.")
    conn = connect(db)
    try:
        upsert_raw(conn, raw)
    finally:
        conn.close()

    def late_failed_model(cmd, **kwargs):
        conn = connect(db)
        try:
            upsert_raw(conn, raw.model_copy(update={"description": "New program confirmed."}))
        finally:
            conn.close()
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="temporary model failure")

    monkeypatch.setenv("LLM_SCORING_MODE", "codex-cli")
    monkeypatch.setattr("api.integrations.llm.codex_cli._codex_path", lambda: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", late_failed_model)
    result = run_cycle(CycleConfig(db_path=db, data_dir=tmp_path, webhook_url="https://example.invalid/disabled",
                      taste_profile="AI engineering", scrape_sources=(), high_bar_threshold=1.01,
                      scoring_batch_size=1, scoring_max_per_cycle=1))
    assert result.scored_failed == 1
    assert web_events(db)[0]["score"] is None
    conn = connect(db)
    try:
        assert [row["title"] for row in fetch_pending_scoring(conn)] == ["Agents workshop"]
    finally:
        conn.close()


def test_worker_requires_annotation_migration_before_scoring(tmp_path):
    migrations = tmp_path / "old-migrations"
    migrations.mkdir()
    for migration in migration_dir_for_dialect("sqlite").glob("*.sql"):
        if migration.name < "0013":
            shutil.copyfile(migration, migrations / migration.name)
    db = tmp_path / "old-events.db"
    conn = connect(db)
    try:
        apply_pending(conn, migrations)
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match=r"0013_event_annotations\.sql not applied"):
        run_cycle(CycleConfig(db_path=db, data_dir=tmp_path, webhook_url="https://example.invalid/disabled",
                             taste_profile="AI engineering", scrape_sources=()))


@freeze_time("2026-10-09T20:00:00Z")
def test_deadline_expiry_is_earlier_than_source_observation_window(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    annotated_event(db)
    event = web_events(db)[0]
    assert event["urgencyNoteExpiresAt"] == "2026-10-09T22:00:00+00:00"


def test_refresh_selection_uses_the_supplied_utc_clock(tmp_path):
    db = migrated_db(tmp_path)
    with connect(db) as conn:
        for day in (4, 6):
            event_id = upsert_raw(conn, RawEvent(
                source="luma", title=f"January {day}",
                url=f"https://example.test/clock/{day}",
                starts_at=datetime(2000, 1, day, 18, tzinfo=UTC),
            ))
            update_fit(conn, event_id, fit_score=.91, reasoning="Original fit", urgency="low",
                       urgency_reason=None, prompt_version="v2", taste_sha="synthetic")
        rows = fetch_pending_scoring(
            conn, refresh_prompt_version="v3", now=datetime(2000, 1, 5, tzinfo=UTC),
        )
    assert [row["title"] for row in rows] == ["January 6"]
