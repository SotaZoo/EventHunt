from __future__ import annotations

import logging

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from api.models.events import RawEvent

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
    "https://www.meetup.com/find/?keywords=AI&location=us--ny--New%20York&source=EVENTS",
    "https://www.meetup.com/find/?keywords=machine%20learning&location=us--ny--New%20York&source=EVENTS",
    "https://www.meetup.com/find/?keywords=LLM&location=us--ny--New%20York&source=EVENTS",
]


class MeetupScraper(BaseScraper):
    name = "meetup"

    def scrape(self, limit: int = 60) -> list[RawEvent]:
        out: list[RawEvent] = []
        seen: set[str] = set()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            for list_url in configured_listing_urls("EVENT_HUNT_MEETUP_LIST_URLS", LIST_URLS):
                try:
                    log.info("fetching %s", list_url)
                    page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)
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
                    if not url or url in seen:
                        continue
                    if "meetup.com" not in url:
                        continue
                    name = data.get("name")
                    if not name:
                        continue
                    seen.add(url)
                    venue, locality = get_location_fields(data)
                    out.append(
                        RawEvent(
                            source="meetup",
                            attendance_mode=get_attendance_mode(data),
                            url=url,
                            title=name,
                            starts_at=parse_dt(data.get("startDate")),
                            ends_at=parse_dt(data.get("endDate")),
                            venue=venue,
                            neighborhood=locality,
                            description=data.get("description"),
                            host=get_organizer(data),
                            cover_image=get_image(data),
                        )
                    )
                    if len(out) >= limit:
                        break
                if len(out) >= limit:
                    break
            browser.close()
        log.info("scraped %d events from meetup", len(out))
        return out[:limit]
