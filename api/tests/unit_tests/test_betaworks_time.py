from datetime import UTC

import pytest

from api.integrations.scrapers.betaworks import BetaworksScraper
from api.tests.fixtures.scraper_browser import ScraperPage, playwright_factory


@pytest.mark.parametrize(("description", "body", "expected"), [
    ("Oct 7, 2026 | Betaworks • New York", "5:30pm", "2026-10-07T21:30:00+00:00"),
    ("Jan 7, 2027 | Betaworks • New York", "5:30pm", "2027-01-07T22:30:00+00:00"),
    ("Oct 7, 2026 | Betaworks • New York", "No published time", None),
])
def test_scraper_uses_explicit_new_york_time_not_naive_or_guessed_time(monkeypatch, description, body, expected):
    page = ScraperPage(["https://www.betaworks.com/event/builders"], description=description, body=body)
    monkeypatch.setattr("api.integrations.scrapers.betaworks.sync_playwright", playwright_factory(page))
    event = BetaworksScraper().scrape()[0]
    if expected is None:
        assert event.starts_at is None
    else:
        assert event.starts_at.tzinfo is not None
        assert event.starts_at.astimezone(UTC).isoformat() == expected
