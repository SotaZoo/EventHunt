"""Source adapters retain explicit event IDs, not unrelated page links."""
import pytest

from api.integrations.scrapers.betaworks import BetaworksScraper
from api.integrations.scrapers.cerebral_valley import CerebralValleyScraper
from api.integrations.scrapers.luma import LumaScraper
from api.tests.fixtures.scraper_browser import ScraperPage, playwright_factory


def test_betaworks_retains_about_and_current_luma_embed_not_template_rsvp(monkeypatch):
    html = '''<a href="https://www.eventbrite.com/e/stale-template-tickets-">RSVP</a>
    <div class="luma-html-embed"><a href="https://lu.ma/event/evt-Example123">Register for Event</a></div>
    <div class="w-richtext"><p>Build and demo agents using the Ophelia API.</p></div>
    <div class="w-richtext"><p>6pm: Doors open</p></div>'''
    page = ScraperPage(["https://www.betaworks.com/event/builders"], html=html)
    monkeypatch.setattr("api.integrations.scrapers.betaworks.sync_playwright", playwright_factory(page))
    event = BetaworksScraper().scrape()[0]
    assert event.registration_url == "https://lu.ma/event/evt-Example123"
    assert event.description == "Build and demo agents using the Ophelia API."


@pytest.mark.parametrize("embeds", [
    '<a href="https://evil.test/event/evt-Example123">Register for Event</a>',
    '<a href="https://lu.ma/event/evt-Example123">Register for Event</a>'
    '<a href="https://lu.ma/event/evt-Other456">Register for Event</a>',
])
def test_unsafe_or_ambiguous_betaworks_embeds_remain_unknown(monkeypatch, embeds):
    page = ScraperPage(["https://www.betaworks.com/event/builders"],
                       html=f'<div class="luma-html-embed">{embeds}</div>')
    monkeypatch.setattr("api.integrations.scrapers.betaworks.sync_playwright", playwright_factory(page))
    assert BetaworksScraper().scrape()[0].registration_url is None


@pytest.mark.parametrize(("meta", "expected"), [
    (["app-id=1546150895, app-argument=luma://event/evt-Example123"], "https://lu.ma/event/evt-Example123"),
    ([], None),
    (["app-argument=luma://event/../../evil"], None),
    (["app-argument=luma://event/evt-First", "app-argument=luma://event/evt-Second"], None),
])
@pytest.mark.parametrize(("source", "scraper_class"), [
    ("luma", LumaScraper),
    ("cerebral_valley", CerebralValleyScraper),
])
def test_luma_detail_adapters_retain_embedded_event_id_without_extra_requests(
    monkeypatch, meta, expected, source, scraper_class,
):
    page = ScraperPage(["https://luma.com/builders"], app_meta=meta)
    monkeypatch.setattr(f"api.integrations.scrapers.{source}.sync_playwright", playwright_factory(page))
    event = scraper_class().scrape(limit=1)[0]
    assert event.registration_url == expected
    assert page.visited.count(event.url) == 1
