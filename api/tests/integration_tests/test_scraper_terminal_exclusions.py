from contextlib import closing

import pytest
from freezegun import freeze_time

from api.repositories.events import connect
from api.tests.fixtures.catalog_helpers import observed_listing
from api.tests.fixtures.scraper_browser import ScraperPage, playwright_factory
from api.tests.fixtures.web_helpers import migrated_db


@freeze_time("2026-10-06T12:00:00Z")
@pytest.mark.parametrize("source, terminal_url, alias_url, open_url", [
    ("luma", "https://lu.ma/closed?utm_source=friend", "https://luma.com/closed", "https://luma.com/open-event"),
    ("betaworks", "https://www.betaworks.com/event/closed", "https://betaworks.com/event/closed/", "https://www.betaworks.com/event/open-event"),
    ("cerebral_valley", "https://lu.ma/closed?utm_source=friend", "https://luma.com/closed", "https://luma.com/open-event"),
])
def test_detail_scrape_skips_persisted_terminal_aliases_before_limit(
    monkeypatch, tmp_path, source, terminal_url, alias_url, open_url,
):
    from api.repositories.availability import terminal_source_identities
    from api.tasks.cycle import run_scrapers

    db = migrated_db(tmp_path)
    observed_listing(db, terminal_url, "closed")
    page = ScraperPage([alias_url, open_url])
    monkeypatch.setattr(f"api.integrations.scrapers.{source}.sync_playwright", playwright_factory(page))
    with closing(connect(db)) as conn:
        exclusions = terminal_source_identities(conn)
    events = run_scrapers((source,), limit=1, excluded_sources=exclusions)
    assert alias_url not in page.visited
    assert [event.url for event in events] == [open_url]
