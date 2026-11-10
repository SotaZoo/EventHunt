"""Spend the scoring budget on usable sources, not terminal provider aliases."""
from contextlib import closing
from datetime import datetime

import pytest
from freezegun import freeze_time

from api.models.availability import RegistrationAvailability
from api.models.events import RawEvent
from api.repositories.events import connect, fetch_pending_scoring, upsert_raw
from api.tests.fixtures.catalog_helpers import START
from api.tests.fixtures.web_helpers import migrated_db


def pending_listing(conn, url, state: RegistrationAvailability = "unknown", *, start=START,
                    title="AI Builders"):
    """Insert an unscored source through the real ingestion seam."""
    return upsert_raw(conn, RawEvent(
        source="luma", url=url, title=title, starts_at=datetime.fromisoformat(start),
        host="AI Builders", venue="Builders Hall",
        registration_availability=state, availability_evidence=f"source: {state}",
    ))


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("state", ["closed", "full", "canceled"])
@pytest.mark.parametrize("alias_start", [START, "2026-10-07T18:30:00-04:00"])
def test_terminal_provider_alias_cannot_consume_the_pending_scoring_limit(tmp_path, state, alias_start):
    db = migrated_db(tmp_path)
    with closing(connect(db)) as conn:
        eligible = pending_listing(conn, "https://lu.ma/eligible", title="Different event")
        pending_listing(conn, "https://lu.ma/terminal", state)
        pending_listing(conn, "https://luma.com/terminal?utm_source=friend", start=alias_start)

        assert [row["id"] for row in fetch_pending_scoring(conn, limit=1)] == [eligible]


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("terminal", ["closed", "full"])
@pytest.mark.parametrize("available", ["open", "waitlist"])
def test_listing_closure_preserves_an_independent_usable_source_for_scoring(tmp_path, terminal, available):
    db = migrated_db(tmp_path)
    with closing(connect(db)) as conn:
        pending_listing(conn, "https://lu.ma/terminal", terminal)
        eligible = pending_listing(conn, "https://www.eventbrite.com/e/builders-1234567890", available)

        assert [row["id"] for row in fetch_pending_scoring(conn, limit=1)] == [eligible]


@freeze_time("2026-10-06T12:00:00Z")
def test_cancellation_context_excludes_correlated_historical_sources_and_clock_drift_aliases(tmp_path):
    db = migrated_db(tmp_path)
    with closing(connect(db)) as conn:
        eligible = pending_listing(conn, "https://lu.ma/eligible", title="Different event")
        historical = "2026-10-05T18:00:00-04:00"
        pending_listing(conn, "https://www.eventbrite.com/e/builders-1234567890", "canceled", start=historical)
        pending_listing(conn, "https://lu.ma/corroborated", start=historical)
        pending_listing(conn, "https://luma.com/corroborated?utm_source=friend")

        assert [row["id"] for row in fetch_pending_scoring(conn, limit=1)] == [eligible]


@freeze_time("2026-10-06T12:00:00Z")
def test_cancellation_precedes_a_correlated_open_alternative_in_the_scoring_budget(tmp_path):
    db = migrated_db(tmp_path)
    with closing(connect(db)) as conn:
        eligible = pending_listing(conn, "https://lu.ma/eligible", title="Different event")
        pending_listing(conn, "https://lu.ma/canceled", "canceled")
        pending_listing(conn, "https://www.meetup.com/builders/events/12345/", "open")

        assert [row["id"] for row in fetch_pending_scoring(conn, limit=1)] == [eligible]


@freeze_time("2026-10-06T12:00:00Z")
def test_a_distinct_recurring_occurrence_keeps_its_scoring_place(tmp_path):
    db = migrated_db(tmp_path)
    with closing(connect(db)) as conn:
        pending_listing(conn, "https://lu.ma/first-occurrence", "canceled")
        eligible = pending_listing(conn, "https://lu.ma/next-occurrence", start="2026-10-08T18:00:00-04:00")

        assert [row["id"] for row in fetch_pending_scoring(conn, limit=1)] == [eligible]
