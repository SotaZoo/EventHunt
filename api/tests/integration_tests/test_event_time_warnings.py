"""Reviewed source-time conflicts remain visible and explicit across interfaces."""
from contextlib import closing
from datetime import datetime

import pytest
from freezegun import freeze_time
from typer.testing import CliRunner

from api.commands.main import app
from api.models.events import RawEvent
from api.repositories.events import connect, update_fit, upsert_raw
from api.tests.fixtures.catalog_helpers import START, agent_events, observed_listing, web_events
from api.tests.fixtures.web_helpers import add_user, migrated_db


@freeze_time('2026-10-06T12:00:00Z')
def test_reviewed_time_conflict_stays_visible_without_changing_source_time(tmp_path, monkeypatch):
    db = migrated_db(tmp_path)
    add_user(db)
    url = 'https://luma.com/synthetic-time-conflict'
    event_id = observed_listing(db, url, 'open')
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('DATABASE_PATH', str(db))
    with closing(connect(db)) as conn:
        original = conn.execute('SELECT start_at FROM events WHERE id=?', (event_id,)).fetchone()[0]
    result = CliRunner().invoke(app, ['event', 'time-warning', '--url', url,
                                    '--evidence', 'Source metadata says 1 PM; organizer explicitly says 6 PM.'])
    assert result.exit_code == 0, result.output
    web = web_events(db)
    agent = agent_events(db)
    assert len(web) == len(agent) == 1
    assert web[0]['timeWarning'] == 'Date/time unconfirmed. Check with the organizer.'
    assert agent[0].time_warning == web[0]['timeWarning']
    with closing(connect(db)) as conn:
        assert conn.execute('SELECT start_at FROM events WHERE id=?', (event_id,)).fetchone()[0] == original

    with closing(connect(db)) as conn:
        upsert_raw(conn, RawEvent(source='luma', url=url, title='Updated source title',
                                 starts_at=datetime.fromisoformat(START), description='Refreshed source content.'))
    assert web_events(db)[0]['timeWarning'] == web[0]['timeWarning']
    with closing(connect(db)) as conn:
        update_fit(conn, event_id, fit_score=.9, reasoning='Shared fit', urgency='low', urgency_reason=None,
                   prompt_version='v3', taste_sha='synthetic')
    assert agent_events(db)[0].time_warning == web[0]['timeWarning']


@freeze_time('2026-10-06T12:00:00Z')
def test_warning_reaches_selected_cross_post_and_clear_requires_resolution_evidence(tmp_path, monkeypatch):
    db = migrated_db(tmp_path)
    add_user(db)
    url = 'https://luma.com/synthetic-time-conflict'
    observed_listing(db, url, 'open')
    selected = observed_listing(db, 'https://www.eventbrite.com/e/builders-1234567890', 'open', score=.95)
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('DATABASE_PATH', str(db))
    runner = CliRunner()
    args = ['event', 'time-warning', '--url', url]
    assert runner.invoke(app, [*args, '--evidence', 'Verified source disagreement.']).exit_code == 0
    assert web_events(db)[0]['id'] == selected
    assert web_events(db)[0]['timeWarning'] == agent_events(db)[0].time_warning
    assert runner.invoke(app, [*args, '--clear', '--evidence', '']).exit_code == 1
    assert agent_events(db)[0].time_warning is not None
    assert runner.invoke(app, [*args, '--clear', '--evidence', 'Organizer corrected the source date and time.']).exit_code == 0
    assert web_events(db)[0]['timeWarning'] is None
    assert agent_events(db)[0].time_warning is None


@freeze_time('2026-10-06T12:00:00Z')
def test_review_updates_provider_aliases_without_touching_unrelated_event(tmp_path, monkeypatch):
    db = migrated_db(tmp_path)
    add_user(db)
    for url in ['https://lu.ma/synthetic-time-conflict?utm_source=friend', 'https://luma.com/synthetic-time-conflict']:
        observed_listing(db, url, 'open')
    unrelated = observed_listing(db, 'https://luma.com/unrelated', 'open')
    with closing(connect(db)) as conn:
        conn.execute('UPDATE events SET title=? WHERE id=?', ('Unrelated event', unrelated))
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('DATABASE_PATH', str(db))
    result = CliRunner().invoke(app, ['event', 'time-warning', '--url', 'https://luma.com/synthetic-time-conflict',
                                    '--evidence', 'Verified source disagreement.'])
    assert result.exit_code == 0
    events = web_events(db)
    assert len(events) == 2
    assert next(event for event in events if event['id'] == unrelated)['timeWarning'] is None
    with closing(connect(db)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM events WHERE time_warning IS NOT NULL').fetchone()[0] == 2


@freeze_time('2026-10-06T12:00:00Z')
def test_new_provider_alias_with_different_time_keeps_warning_until_reviewed_clear(tmp_path, monkeypatch):
    db = migrated_db(tmp_path)
    add_user(db)
    url = 'https://lu.ma/synthetic-time-conflict'
    observed_listing(db, url, 'open')
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('DATABASE_PATH', str(db))
    args = ['event', 'time-warning', '--url', url, '--evidence', 'Verified source disagreement.']
    runner = CliRunner()
    assert runner.invoke(app, args).exit_code == 0
    with closing(connect(db)) as conn:
        upsert_raw(conn, RawEvent(source='luma', url='https://luma.com/synthetic-time-conflict?utm_source=latest',
                                 title='AI Builders', starts_at=datetime.fromisoformat('2026-10-08T18:00:00-04:00')))
    events = web_events(db)
    assert len(events) == 2
    assert all(event['timeWarning'] is not None for event in events)
    assert runner.invoke(app, [*args, '--clear']).exit_code == 0
    assert all(event['timeWarning'] is None for event in web_events(db))


@freeze_time('2026-10-06T12:00:00Z')
@pytest.mark.parametrize('state', ['closed', 'full', 'canceled'])
def test_warning_never_reopens_a_terminal_event(tmp_path, monkeypatch, state):
    db = migrated_db(tmp_path)
    add_user(db)
    url = 'https://luma.com/synthetic-closed'
    observed_listing(db, url, state)
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('DATABASE_PATH', str(db))
    assert CliRunner().invoke(app, ['event', 'time-warning', '--url', url, '--evidence', 'Conflicting clocks.']).exit_code == 0
    assert web_events(db) == []
    assert agent_events(db) == []


@pytest.mark.parametrize('missing_source,too_long', [(True, False), (False, True)])
def test_invalid_review_leaves_existing_source_facts_unchanged(tmp_path, monkeypatch, missing_source, too_long):
    db = migrated_db(tmp_path)
    url = 'https://luma.com/synthetic-time-conflict'
    observed_listing(db, url, 'open')
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('DATABASE_PATH', str(db))
    runner = CliRunner()
    assert runner.invoke(app, ['event', 'time-warning', '--url', url, '--evidence', 'Original review.']).exit_code == 0
    with closing(connect(db)) as conn:
        before = tuple(conn.execute('SELECT time_warning,time_warning_evidence,time_warning_reviewed_at,start_at FROM events').fetchone())
    result = runner.invoke(app, ['event', 'time-warning', '--url', 'https://luma.com/missing' if missing_source else url,
                                 '--evidence', 'x' * 2001 if too_long else 'New review.'])
    assert result.exit_code == 1
    with closing(connect(db)) as conn:
        after = tuple(conn.execute('SELECT time_warning,time_warning_evidence,time_warning_reviewed_at,start_at FROM events').fetchone())
    assert after == before
