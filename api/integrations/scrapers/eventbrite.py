from __future__ import annotations

import logging
from datetime import datetime

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout
from pydantic import ValidationError

from api.models.events import RawEvent
from api.models.occurrences import provider_identity, supported_registration_url

from ._util import (
    UA,
    configured_listing_urls,
    extract_jsonld,
    find_events,
    get_attendance_mode,
    get_image,
    get_location_fields,
    get_organizer,
    parse_dt,
)
from .base import BaseScraper

log = logging.getLogger(__name__)

LIST_URLS = [
    "https://www.eventbrite.com/d/ny--new-york/ai/",
    "https://www.eventbrite.com/d/ny--new-york/machine-learning/",
]


def _exact_time(value: object) -> datetime | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    parsed = parse_dt(value)
    return parsed if parsed and parsed.tzinfo and parsed.utcoffset() is not None else None


def _read_detail(page: Page, url: str) -> RawEvent | None:
    """Read one matching occurrence; search pages omit the actual local time."""
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=15000)
    except PWError as exc:
        log.warning("eventbrite detail unavailable: kind=%s", type(exc).__name__)
        return None
    identity = provider_identity(url)
    if (response is None or response.status >= 400 or not supported_registration_url(page.url)
            or provider_identity(page.url) != identity):
        return None
    matching = []
    for node in extract_jsonld(page):
        types = node.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if (isinstance(types, list) and any(isinstance(t, str) and t.endswith("Event") for t in types)
                and isinstance(node.get("url"), str) and provider_identity(node["url"]) == identity):
            matching.append(node)
    if len(matching) != 1:
        return None
    data = matching[0]
    start = _exact_time(data.get("startDate"))
    title = data.get("name")
    if start is None or not isinstance(title, str) or not title.strip():
        return None
    end = _exact_time(data.get("endDate"))
    venue, locality = get_location_fields(data)
    try:
        return RawEvent(
            source="eventbrite", url=url, title=title, starts_at=start,
            ends_at=end if end is not None and end > start else None,
            attendance_mode=get_attendance_mode(data), venue=venue, neighborhood=locality,
            description=data.get("description"), host=get_organizer(data), cover_image=get_image(data),
        )
    except (ValidationError, AttributeError, TypeError):
        # Existing field extractors also reject malformed nested image data.
        # One bad detail must not discard this source's other valid events.
        log.warning("eventbrite detail has invalid event fields")
        return None


class EventbriteScraper(BaseScraper):
    name = "eventbrite"

    def scrape(self, limit: int = 60) -> list[RawEvent]:
        out: list[RawEvent] = []
        seen: set[tuple[str, str]] = set()
        if limit <= 0:
            return out
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            detail_page = ctx.new_page()
            for list_url in configured_listing_urls("EVENT_HUNT_EVENTBRITE_LIST_URLS", LIST_URLS):
                try:
                    log.info("fetching %s", list_url)
                    page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2500)
                except PWTimeout:
                    log.warning("timeout on %s", list_url)
                    continue
                ld_objs = extract_jsonld(page)
                events = find_events(ld_objs)
                log.info("  %d event ld blocks on %s", len(events), list_url)
                for data in events:
                    url = data.get("url") or data.get("@id")
                    if isinstance(url, dict):
                        url = url.get("@id")
                    if not isinstance(url, str) or not supported_registration_url(url):
                        continue
                    identity = provider_identity(url)
                    if identity[0] != "eventbrite" or identity in seen or self.should_skip(url):
                        continue
                    # Allow one failed candidate per requested event, then stop.
                    # A blocked provider must not cause an unbounded detail crawl.
                    if len(seen) >= limit * 2:
                        break
                    seen.add(identity)
                    event = _read_detail(detail_page, url)
                    if event is not None:
                        out.append(event)
                    if len(out) >= limit:
                        break
                if len(out) >= limit or len(seen) >= limit * 2:
                    break
            browser.close()
        log.info("scraped %d events from eventbrite", len(out))
        return out[:limit]
