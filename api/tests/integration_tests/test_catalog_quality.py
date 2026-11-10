"""Catalog cleanup through the same public web and agent query seams."""
from contextlib import closing
from datetime import datetime

from freezegun import freeze_time

from api.models.events import RawEvent
from api.repositories.events import connect, fetch_pending_scoring, upsert_raw
from api.services.event_catalog import find_events
from api.tests.fixtures.catalog_helpers import START, agent_events, observed_listing, web_events
from api.tests.fixtures.web_helpers import add_scored_event, add_user, migrated_db


def listing(db, url, *, title="AI Builders", venue="Builders Hall", score=.8, start=START):
    """Insert a scored listing with explicit synthetic occurrence facts."""
    return add_scored_event(db, url=url, title=title, venue=venue, score=score,
                            starts_at=datetime.fromisoformat(start))


@freeze_time("2026-10-06T12:00:00Z")
def test_terminal_availability_hidden_and_waitlist_visible_in_web_and_mcp(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    for state in ("closed", "full", "canceled", "waitlist", "unknown"):
        observed_listing(db, f"https://lu.ma/{state}", state)
        with closing(connect(db)) as conn:
            # These are different events, not conflicting listings of one occurrence.
            conn.execute("UPDATE events SET title = ? WHERE url = ?", (state, f"https://lu.ma/{state}"))
    web = web_events(db)
    assert {event["title"] for event in web} == {"waitlist", "unknown"}
    assert next(event for event in web if event["title"] == "waitlist")["registrationAvailability"] == "waitlist"
    agent = agent_events(db)
    assert {event.title for event in agent} == {"waitlist", "unknown"}
    assert next(event for event in agent if event.title == "waitlist").availability_checked_at == "2026-10-06T12:00:00Z"
    with closing(connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 5


@freeze_time("2026-10-06T12:00:00Z")
def test_closed_alias_cannot_resurrect_but_verified_alternative_can(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    observed_listing(db, "https://lu.ma/same?utm_source=friend", "closed", score=.95)
    observed_listing(db, "https://luma.com/same", "unknown")
    assert web_events(db) == []
    assert agent_events(db) == []
    observed_listing(db, "https://www.eventbrite.com/e/builders-1234567890", "open", score=.7)
    assert len(web_events(db)) == 1
    assert agent_events(db, limit=1)[0].url.endswith("1234567890")


@freeze_time("2026-10-06T12:00:00Z")
def test_cancellation_hides_matched_open_alternative_even_if_canceled_row_unscored(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    canceled = observed_listing(db, "https://lu.ma/canceled", "canceled")
    with closing(connect(db)) as conn:
        conn.execute("UPDATE events SET scoring_status = 'failed' WHERE id = ?", (canceled,))
    observed_listing(db, "https://www.eventbrite.com/e/builders-1234567890", "open")
    assert web_events(db) == []
    assert agent_events(db) == []


@freeze_time("2026-10-06T12:00:00Z")
def test_terminal_fact_survives_ingestion_and_unknown_does_not_erase_waitlist(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    observed_listing(db, "https://lu.ma/closed", "closed")
    observed_listing(db, "https://lu.ma/closed", "open")
    assert web_events(db) == []
    observed_listing(db, "https://lu.ma/waitlist", "waitlist")
    observed_listing(db, "https://lu.ma/waitlist", "unknown")
    # Cancellation/closure must not win here: the separate closed listing has the same facts.
    with closing(connect(db)) as conn:
        conn.execute("UPDATE events SET title = 'Waitlist event' WHERE url = 'https://lu.ma/waitlist'")
    assert web_events(db)[0]["registrationAvailability"] == "waitlist"


@freeze_time("2026-10-06T12:00:00Z")
def test_provider_aliases_do_not_consume_web_cards_or_mcp_limit(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    listing(db, "https://www.eventbrite.com/e/ai-builders-tickets-1234567890?aff=friend", score=.95)
    listing(db, "https://eventbrite.com/e/another-slug-1234567890", score=.9)
    listing(db, "https://lu.ma/another-event", title="Different event", score=.8)
    assert len(web_events(db)) == 2
    events = agent_events(db, limit=2)
    assert len(events) == 2
    assert {event.title for event in events} == {"AI Builders", "Different event"}


@freeze_time("2026-10-06T12:00:00Z")
def test_strong_same_occurrence_match_but_not_title_alone(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    listing(db, "https://lu.ma/builders", start="2026-10-07T22:00:00+00:00")
    listing(db, "https://www.meetup.com/builders/events/12345/", score=.75)
    listing(db, "https://lu.ma/other-venue", venue="Other Hall")
    listing(db, "https://lu.ma/next-occurrence", start="2026-10-08T18:00:00-04:00")
    assert len(web_events(db)) == 3
    assert len(agent_events(db)) == 3


@freeze_time("2026-10-06T12:00:00Z")
def test_aliases_on_distinct_recurring_dates_are_not_merged(tmp_path):
    db = migrated_db(tmp_path)
    add_user(db)
    listing(db, "https://lu.ma/series?occurrence=1")
    listing(db, "https://luma.com/series?occurrence=2", start="2026-10-08T18:00:00-04:00")
    assert len(web_events(db)) == 2
    assert len(agent_events(db)) == 2


@freeze_time("2026-10-06T12:00:00Z")
def test_mcp_time_window_compares_instants_not_offset_strings(tmp_path):
    db = migrated_db(tmp_path)
    listing(db, "https://lu.ma/offset-time")
    with closing(connect(db)) as conn:
        result = find_events(conn, start="2026-10-07T21:00:00Z",
                             end="2026-10-07T23:00:00Z", limit=1)
    assert result.count == 1
    assert result.events[0].start_at == "2026-10-07T22:00:00Z"


def test_terminal_listings_do_not_consume_scoring_budget(tmp_path):
    db = migrated_db(tmp_path)
    with closing(connect(db)) as conn:
        for state in ("unknown", "waitlist", "closed", "full", "canceled"):
            upsert_raw(conn, RawEvent(source="luma", url=f"https://lu.ma/{state}", title=state,
                                     starts_at=datetime.fromisoformat(START), registration_availability=state,
                                     availability_evidence=f"source: {state}"))
        assert {row["title"] for row in fetch_pending_scoring(conn, limit=2)} == {"unknown", "waitlist"}
