"""Eventbrite search dates are discovery hints, never exact event times."""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from playwright.sync_api import TimeoutError as PWTimeout

from api.integrations.scrapers.eventbrite import EventbriteScraper
from api.tests.fixtures.scraper_browser import ScraperPage, playwright_factory

LIST_URL = "https://www.eventbrite.com/d/ny--new-york/ai/"
EVENT_URL = "https://www.eventbrite.com/e/old-title-tickets-123456789"
OTHER_URL = "https://www.eventbrite.com/e/another-event-tickets-987654321"


def listing(url=EVENT_URL):
    return {"@type": "Event", "url": url, "name": "Old listing title",
            "startDate": "2026-11-21", "endDate": "2026-11-21"}


def detail(**changes):
    return {"@type": "SocialEvent", "url": EVENT_URL, "name": "Current event title",
            "startDate": "2026-12-11T14:00:00-05:00", "endDate": "2026-12-11T16:00:00-05:00",
            "location": {"@type": "Place", "name": "Example venue",
                         "address": {"addressLocality": "New York"}}, **changes}


class EventbritePage(ScraperPage):
    def __init__(self, listings, details):
        super().__init__([])
        self.pages = {LIST_URL: {"@type": "ItemList", "itemListElement": listings}, **details}
        self.url = "about:blank"

    def goto(self, url, **kwargs):
        self.visited.append(url)
        self.url = url
        if isinstance(self.pages.get(url), Exception):
            raise self.pages[url]
        return SimpleNamespace(status=200)

    def eval_on_selector_all(self, selector, expression):
        return [json.dumps(self.pages.get(self.url, {}))]


def scraper(monkeypatch, listings, details):
    page = EventbritePage(listings, details)
    monkeypatch.setattr("api.integrations.scrapers.eventbrite.LIST_URLS", [LIST_URL])
    monkeypatch.setattr("api.integrations.scrapers.eventbrite.sync_playwright", playwright_factory(page))
    return EventbriteScraper(), page


def test_uses_current_detail_time_and_preserves_listing_identity(monkeypatch):
    current = detail(url="https://www.eventbrite.com/e/current-title-tickets-123456789")
    source, page = scraper(monkeypatch, [listing()], {EVENT_URL: current})

    event, = source.scrape(limit=1)

    assert event.url == EVENT_URL
    assert event.title == "Current event title"
    assert event.starts_at == datetime.fromisoformat("2026-12-11T14:00:00-05:00")
    assert event.ends_at == datetime.fromisoformat("2026-12-11T16:00:00-05:00")
    assert event.venue == "Example venue"
    assert page.visited.count(EVENT_URL) == 1


def test_configured_listing_can_collect_events_in_another_region(monkeypatch):
    london_feed = "https://www.eventbrite.co.uk/d/united-kingdom--london/technology/"
    london_url = EVENT_URL.replace("eventbrite.com", "eventbrite.co.uk")
    london_event = detail(
        url=london_url,
        startDate="2026-12-11T14:00:00+00:00",
        endDate="2026-12-11T16:00:00+00:00",
        location={
            "@type": "Place",
            "name": "Example venue",
            "address": {"addressLocality": "London"},
        },
    )
    source, page = scraper(monkeypatch, [], {
        london_feed: {"@type": "ItemList", "itemListElement": [listing(london_url)]},
        london_url: london_event,
    })
    monkeypatch.setenv("EVENT_HUNT_EVENTBRITE_LIST_URLS", london_feed)

    event, = source.scrape(limit=1)

    assert page.visited == [london_feed, london_url]
    assert event.neighborhood == "London"
    assert event.starts_at == datetime.fromisoformat("2026-12-11T14:00:00+00:00")


@pytest.mark.parametrize("start", ["2026-11-21", "2026-11-21T14:00:00", None, "invalid", 123])
def test_does_not_invent_time_when_detail_has_no_exact_timestamp(monkeypatch, start):
    source, _ = scraper(monkeypatch, [listing()], {EVENT_URL: detail(startDate=start)})
    assert source.scrape(limit=1) == []


@pytest.mark.parametrize("end", ["2026-12-11", "2026-12-11T16:00:00", "2026-12-11T12:00:00-05:00", None])
def test_omits_unverified_or_inverted_end_time(monkeypatch, end):
    source, _ = scraper(monkeypatch, [listing()], {EVENT_URL: detail(endDate=end)})
    event, = source.scrape(limit=1)
    assert event.ends_at is None


@pytest.mark.parametrize("node", [
    detail(url=OTHER_URL),
    [detail(), detail(name="Conflicting occurrence")],
    {"@type": "WebPage", "url": EVENT_URL, "startDate": "2026-12-11T14:00:00-05:00"},
])
def test_rejects_unrelated_or_ambiguous_detail_events(monkeypatch, node):
    source, _ = scraper(monkeypatch, [listing()], {EVENT_URL: node})
    assert source.scrape(limit=1) == []


def test_detail_timeout_does_not_fall_back_to_search_date_or_stop_other_events(monkeypatch):
    source, page = scraper(monkeypatch, [listing(), listing(OTHER_URL)], {
        EVENT_URL: PWTimeout("synthetic timeout"), OTHER_URL: detail(url=OTHER_URL),
    })
    event, = source.scrape(limit=1)
    assert event.url == OTHER_URL
    assert event.starts_at == datetime.fromisoformat("2026-12-11T14:00:00-05:00")
    assert page.visited.count(EVENT_URL) == 1


def test_skips_terminal_and_duplicate_provider_ids_before_detail_fetch(monkeypatch):
    alias = "https://www.eventbrite.com/e/new-slug-tickets-123456789?aff=example"
    source, page = scraper(monkeypatch, [listing(), listing(alias), listing(OTHER_URL)], {
        OTHER_URL: detail(url=OTHER_URL),
    })
    source.excluded_sources = frozenset({("eventbrite", "123456789")})
    event, = source.scrape(limit=1)
    assert event.url == OTHER_URL
    assert EVENT_URL not in page.visited
    assert alias not in page.visited


def test_rejects_external_detail_urls(monkeypatch):
    external = "https://example.invalid/e/event-tickets-123456789"
    source, page = scraper(monkeypatch, [listing(external)], {})
    assert source.scrape(limit=1) == []
    assert external not in page.visited


@pytest.mark.parametrize("status", [403, 404, 429, 500])
def test_rejects_error_pages_even_if_they_contain_event_metadata(monkeypatch, status):
    source, page = scraper(monkeypatch, [listing()], {EVENT_URL: detail()})
    goto = page.goto

    def error_page(url, **kwargs):
        goto(url, **kwargs)
        return SimpleNamespace(status=status)

    page.goto = error_page
    assert source.scrape(limit=1) == []


def test_rejects_redirect_to_a_different_provider_identity(monkeypatch):
    source, page = scraper(monkeypatch, [listing()], {EVENT_URL: detail()})
    goto = page.goto

    def redirected_page(url, **kwargs):
        response = goto(url, **kwargs)
        if url == EVENT_URL:
            page.url = OTHER_URL
        return response

    page.goto = redirected_page
    assert source.scrape(limit=1) == []


def test_deduplicates_open_aliases_before_fetching_details(monkeypatch):
    alias = "https://www.eventbrite.com/e/new-slug-tickets-123456789?aff=example"
    source, page = scraper(monkeypatch, [listing(), listing(alias), listing(OTHER_URL)], {
        EVENT_URL: detail(), OTHER_URL: detail(url=OTHER_URL),
    })
    assert len(source.scrape(limit=2)) == 2
    assert page.visited.count(EVENT_URL) == 1
    assert alias not in page.visited


def test_bounds_detail_visits_when_provider_is_unavailable(monkeypatch):
    third = "https://www.eventbrite.com/e/third-event-tickets-555555555"
    source, page = scraper(monkeypatch, [listing(), listing(OTHER_URL), listing(third)], {
        EVENT_URL: PWTimeout("synthetic timeout"), OTHER_URL: PWTimeout("synthetic timeout"),
        third: detail(url=third),
    })
    assert source.scrape(limit=1) == []
    assert third not in page.visited


@pytest.mark.parametrize("malformed", [
    {"description": {"unexpected": "object"}}, {"name": {"text": "bad title"}},
    {"image": [None]}, {"image": [123]},
])
def test_malformed_detail_does_not_discard_earlier_events_or_stop_later_events(monkeypatch, malformed):
    bad_url = "https://www.eventbrite.com/e/bad-metadata-tickets-555555555"
    source, _ = scraper(monkeypatch, [listing(), listing(bad_url), listing(OTHER_URL)], {
        EVENT_URL: detail(), bad_url: detail(url=bad_url, **malformed), OTHER_URL: detail(url=OTHER_URL),
    })
    assert [event.url for event in source.scrape(limit=3)] == [EVENT_URL, OTHER_URL]
