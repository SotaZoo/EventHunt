"""Real persistence, real refresh task, synthetic external provider responses."""
from contextlib import closing
from datetime import datetime
from shutil import copy

import httpx
import pytest
from freezegun import freeze_time

from api.repositories.events import connect
from api.tests.fixtures.catalog_helpers import (
    START,
    agent_events,
    observed_listing,
    web_events,
)
from api.tests.fixtures.registration_helpers import registration_form
from api.tests.fixtures.web_helpers import add_user, migrated_db


def connection_factory(db):
    """Use a fresh, always-closed connection for each refresh transaction."""
    return lambda: closing(connect(db))


@freeze_time("2026-10-06T12:00:00Z")
def test_refresh_waitlist_and_skip_confirmed_terminals_across_restart(tmp_path):
    from api.tasks.availability import refresh_availability

    db = migrated_db(tmp_path)
    add_user(db)
    for state in ("closed", "full", "canceled", "waitlist"):
        observed_listing(db, f"https://lu.ma/{state}", state)
        with closing(connect(db)) as conn:
            conn.execute("UPDATE events SET title = ? WHERE url = ?", (state, f"https://lu.ma/{state}"))
    requested = []

    def source(request):
        requested.append(str(request.url))
        return httpx.Response(200, text=registration_form(str(request.url), '<button>Join Waitlist</button>'))

    with httpx.Client(transport=httpx.MockTransport(source)) as client:
        first = refresh_availability(connection_factory(db), client=client)
        # New connections/task invocation simulate a worker restart, not in-memory suppression.
        second = refresh_availability(connection_factory(db), client=client)
    assert first.attempted == second.attempted == 1
    assert requested == ["https://lu.ma/waitlist"] * 2
    assert web_events(db)[0]["registrationAvailability"] == "waitlist"
    assert agent_events(db)[0].registration_availability == "waitlist"


@freeze_time("2026-10-06T12:00:00Z")
def test_timeout_and_unknown_do_not_erase_waitlist_then_closure_stops_refresh(tmp_path):
    from api.tasks.availability import refresh_availability

    db = migrated_db(tmp_path)
    add_user(db)
    observed_listing(db, "https://lu.ma/waitlist", "waitlist")

    def timeout(request):
        raise httpx.ReadTimeout("provider did not respond", request=request)

    for transport in (httpx.MockTransport(timeout), httpx.MockTransport(lambda request: httpx.Response(200, text='<html></html>'))):
        with httpx.Client(transport=transport) as client:
            refresh_availability(connection_factory(db), client=client)
        assert web_events(db)[0]["registrationAvailability"] == "waitlist"
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=registration_form(str(request.url), '<button disabled>Registration closed</button>')))) as client:
        refresh_availability(connection_factory(db), client=client)
        assert refresh_availability(connection_factory(db), client=client).attempted == 0
    assert web_events(db) == []
    assert agent_events(db) == []


@freeze_time("2026-10-06T12:00:00Z")
def test_bounded_refresh_rotates_stale_listings_and_does_not_starve_unknowns(tmp_path):
    from api.tasks.availability import refresh_availability

    db = migrated_db(tmp_path)
    for state in ("waitlist", "unknown"):
        observed_listing(db, f"https://lu.ma/{state}", state)
        with closing(connect(db)) as conn:
            conn.execute("UPDATE events SET title = ? WHERE url = ?", (state, f"https://lu.ma/{state}"))
    requested = []

    def source(request):
        requested.append(str(request.url))
        return httpx.Response(200, text='<html></html>')

    with httpx.Client(transport=httpx.MockTransport(source)) as client:
        refresh_availability(connection_factory(db), limit=1, client=client)
        with freeze_time("2026-10-06T18:00:00Z"):
            refresh_availability(connection_factory(db), limit=1, client=client)
    assert set(requested) == {"https://lu.ma/waitlist", "https://lu.ma/unknown"}


@freeze_time("2026-10-06T12:00:00Z")
def test_new_explicit_occurrence_does_not_inherit_old_terminal_fact(tmp_path):
    from api.models.events import RawEvent
    from api.repositories.events import upsert_raw

    db = migrated_db(tmp_path)
    add_user(db)
    observed_listing(db, "https://lu.ma/old-occurrence", "canceled")
    with closing(connect(db)) as conn:
        upsert_raw(conn, RawEvent(source="luma", url="https://lu.ma/new-occurrence", title="New occurrence",
                                  starts_at=datetime.fromisoformat("2026-10-08T18:00:00-04:00")))
    assert web_events(db)[0]["registrationAvailability"] == "unknown"


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("state", ["closed", "full", "canceled"])
def test_time_correction_is_not_evidence_of_a_new_occurrence(tmp_path, state):
    from api.models.events import RawEvent
    from api.repositories.events import upsert_raw
    from api.tasks.availability import refresh_availability

    db = migrated_db(tmp_path)
    add_user(db)
    observed_listing(db, "https://lu.ma/terminal", state)
    with closing(connect(db)) as conn:
        upsert_raw(conn, RawEvent(source="luma", url="https://lu.ma/terminal", title="AI Builders",
                                  starts_at=datetime.fromisoformat("2026-10-07T18:30:00-04:00")))
        row = conn.execute("SELECT * FROM events WHERE url = 'https://lu.ma/terminal'").fetchone()
        assert row["registration_availability"] == state
        assert row["availability_evidence"] == f"source: {state}"
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("terminal source revisited"))) as client:
        assert refresh_availability(connection_factory(db), client=client).attempted == 0
    assert web_events(db) == agent_events(db) == []


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("state", ["closed", "full", "canceled"])
@pytest.mark.parametrize("terminal_start", [START, "2026-10-05T18:00:00-04:00"])
def test_clock_drift_alias_cannot_reopen_or_recheck_terminal_source(tmp_path, state, terminal_start):
    from api.models.occurrences import provider_identity
    from api.repositories.availability import terminal_source_identities
    from api.tasks.availability import refresh_availability

    db = migrated_db(tmp_path)
    add_user(db)
    terminal = observed_listing(db, "https://lu.ma/terminal", state)
    alias = observed_listing(db, "https://luma.com/terminal?utm_source=friend", "unknown")
    with closing(connect(db)) as conn:
        conn.execute("UPDATE events SET start_at = ? WHERE id = ?", (terminal_start, terminal))
        conn.execute("UPDATE events SET start_at = '2026-10-07T18:30:00-04:00' WHERE id = ?", (alias,))
        assert provider_identity("https://lu.ma/terminal") in terminal_source_identities(conn)
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("terminal alias revisited"))) as client:
        assert refresh_availability(connection_factory(db), client=client).attempted == 0
    assert web_events(db) == agent_events(db) == []


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("historical", [False, True])
def test_cancellation_follows_corroborated_source_alias_chain_before_date_windows(tmp_path, historical):
    from api.models.occurrences import provider_identity
    from api.repositories.availability import terminal_source_identities
    from api.tasks.availability import refresh_availability

    db = migrated_db(tmp_path)
    add_user(db)
    when = "2026-10-05T23:30:00-04:00" if historical else START
    canceled = observed_listing(db, "https://www.eventbrite.com/e/builders-1234567890", "canceled")
    matched = observed_listing(db, "https://lu.ma/builders", "open")
    alias = observed_listing(db, "https://luma.com/builders?utm_source=friend", "open")
    alternate = observed_listing(db, "https://www.meetup.com/builders/events/12345/", "open")
    with closing(connect(db)) as conn:
        for event_id in (canceled, matched):
            conn.execute("UPDATE events SET start_at = ? WHERE id = ?", (when, event_id))
        for event_id in (alias, alternate):
            conn.execute("UPDATE events SET start_at = '2026-10-07T18:30:00-04:00' WHERE id = ?", (event_id,))
        assert provider_identity("https://www.meetup.com/builders/events/12345/") in terminal_source_identities(conn)
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("canceled alias chain revisited"))) as client:
        assert refresh_availability(connection_factory(db), client=client).attempted == 0
    assert web_events(db) == agent_events(db) == []


@freeze_time("2026-10-06T12:00:00Z")
def test_regular_cycle_verifies_waitlist_closes_it_and_never_rechecks(httpx_mock, tmp_path):
    from api.tasks.cycle import CycleConfig, run_cycle

    db = migrated_db(tmp_path)
    add_user(db)
    observed_listing(db, "https://lu.ma/waitlist", "waitlist")
    cfg = CycleConfig(db_path=db, data_dir=tmp_path, webhook_url="https://example.test/unused",
                      taste_profile="Shared internal profile", scrape_sources=(),
                      scoring_max_per_cycle=0, high_bar_threshold=1.01)
    httpx_mock.add_response(url="https://lu.ma/waitlist", text=registration_form("https://lu.ma/waitlist", '<button>Join Waitlist</button>'))
    assert run_cycle(cfg).availability_checked == 1
    assert web_events(db)[0]["registrationAvailability"] == "waitlist"
    httpx_mock.add_response(url="https://lu.ma/waitlist", text=registration_form("https://lu.ma/waitlist", '<button disabled>Registration closed</button>'))
    assert run_cycle(cfg).availability_confirmed == 1
    assert run_cycle(cfg).availability_checked == 0
    assert len(httpx_mock.get_requests()) == 2
    assert web_events(db) == []
    assert agent_events(db) == []


@freeze_time("2026-10-06T12:00:00Z")
def test_scoring_breaker_does_not_pause_availability_refresh(httpx_mock, tmp_path):
    from api.tasks.cycle import CycleConfig, run_cycle
    from api.tasks.markers import Marker, write_marker

    db = migrated_db(tmp_path)
    add_user(db)
    observed_listing(db, "https://lu.ma/waitlist", "waitlist")
    with closing(connect(db)) as conn:
        write_marker(tmp_path, Marker.SCORING_DISABLED, "scoring paused", conn=conn)
    cfg = CycleConfig(db_path=db, data_dir=tmp_path, webhook_url="https://example.test/unused",
                      taste_profile="Shared internal profile", scrape_sources=(), high_bar_threshold=1.01)
    httpx_mock.add_response(url="https://lu.ma/waitlist", text=registration_form("https://lu.ma/waitlist", '<button>Register</button>'))
    result = run_cycle(cfg)
    assert result.skipped_reason == "scoring_disabled"
    assert result.availability_confirmed == 1
    assert web_events(db)[0]["registrationAvailability"] == "open"


@freeze_time("2026-10-06T12:00:00Z")
def test_unsupported_sources_do_not_use_up_refresh_budget(tmp_path):
    from api.tasks.availability import refresh_availability

    db = migrated_db(tmp_path)
    for index in range(3):
        observed_listing(db, f"https://example.test/{index}", "unknown")
    observed_listing(db, "https://lu.ma/waitlist", "waitlist")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=registration_form(str(request.url), '<button>Join Waitlist</button>')))) as client:
        assert refresh_availability(connection_factory(db), limit=1, client=client).attempted == 1


@freeze_time("2026-10-06T12:00:00Z")
def test_historical_and_undated_listings_do_not_consume_refresh_or_discovery_budget(tmp_path):
    from api.tasks.availability import refresh_availability

    db = migrated_db(tmp_path)
    add_user(db)
    for slug, start in (("past", "2026-10-05T23:30:00-04:00"), ("undated", None)):
        event_id = observed_listing(db, f"https://lu.ma/{slug}", "waitlist")
        with closing(connect(db)) as conn:
            conn.execute("UPDATE events SET start_at = ?, title = ? WHERE id = ?", (start, slug, event_id))
    observed_listing(db, "https://lu.ma/upcoming", "waitlist")
    requested = []

    def source(request):
        requested.append(str(request.url))
        return httpx.Response(200, text=registration_form(str(request.url), '<button>Join Waitlist</button>'))

    with httpx.Client(transport=httpx.MockTransport(source)) as client:
        assert refresh_availability(connection_factory(db), limit=1, client=client).attempted == 1
    assert requested == ["https://lu.ma/upcoming"]
    assert len(web_events(db)) == len(agent_events(db)) == 1


@freeze_time("2026-10-06T12:00:00Z")
def test_older_observation_does_not_replace_current_source_evidence(tmp_path):
    from api.models.availability import AvailabilityObservation
    from api.repositories.availability import record_availability

    db = migrated_db(tmp_path)
    event_id = observed_listing(db, "https://lu.ma/waitlist", "waitlist")
    with closing(connect(db)) as conn:
        before = tuple(conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())
        observation = AvailabilityObservation(
            state="closed", evidence="older source observation",
            checked_at=datetime.fromisoformat("2026-10-06T11:00:00+00:00"),
        )
        assert not record_availability(conn, event_id, observation)
        assert tuple(conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()) == before


@freeze_time("2026-10-06T12:00:00Z")
def test_in_flight_refresh_does_not_apply_observation_after_occurrence_time_changes(tmp_path):
    from api.tasks.availability import refresh_availability

    db = migrated_db(tmp_path)
    event_id = observed_listing(db, "https://lu.ma/waitlist", "waitlist")
    with closing(connect(db)) as conn:
        before = dict(conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())
    updated_start = "2026-10-08T18:00:00-04:00"

    def source(request):
        # A concurrent ingest updates the real row while the outbound request is in flight.
        with closing(connect(db)) as conn:
            conn.execute("UPDATE events SET start_at = ? WHERE id = ?", (updated_start, event_id))
        return httpx.Response(200, text=registration_form(str(request.url), '<button disabled>Registration closed</button>'))

    with httpx.Client(transport=httpx.MockTransport(source)) as client:
        result = refresh_availability(connection_factory(db), client=client)
    assert result.attempted == 1
    assert result.confirmed == 0
    with closing(connect(db)) as conn:
        after = dict(conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone())
    assert after == {**before, "start_at": updated_start}


def test_cycle_requires_registration_availability_migration_before_any_network_work(tmp_path):
    from api.repositories.migrate import apply_pending, migration_dir_for_dialect
    from api.tasks.cycle import CycleConfig, run_cycle

    partial_dir = tmp_path / "migrations-through-0010"
    partial_dir.mkdir()
    for migration in migration_dir_for_dialect("sqlite").glob("*.sql"):
        if migration.name < "0011_registration_availability.sql":
            copy(migration, partial_dir / migration.name)
    db = tmp_path / "partial.db"
    with closing(connect(db)) as conn:
        apply_pending(conn, partial_dir)
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE filename = '0006_scout_bot_mvp.sql'").fetchone()
    cfg = CycleConfig(db_path=db, data_dir=tmp_path, webhook_url="https://example.test/unused",
                      taste_profile="Shared internal profile", scrape_sources=(),
                      scoring_max_per_cycle=0, high_bar_threshold=1.01)
    with pytest.raises(RuntimeError, match=r"0011_registration_availability\.sql"):
        run_cycle(cfg)
