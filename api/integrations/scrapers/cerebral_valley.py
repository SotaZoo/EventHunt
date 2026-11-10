"""Cerebral Valley is an aggregator — it links out to Luma/Meetup/etc.

This scraper harvests the Luma URLs from the CV events page and resolves each
one through the same JSON-LD path the Luma scraper uses, marking the source as
'cerebral_valley' so we preserve provenance (though URL-level dedup will keep
the first-seen row intact).
"""
from __future__ import annotations

import logging
import re

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from api.models.events import RawEvent

from ._util import (
    UA,
    extract_jsonld,
    find_events,
    get_attendance_mode,
    get_image,
    get_location_fields,
    get_organizer,
    parse_dt,
)
from .base import BaseScraper
from .luma import registration_url_from_meta

log = logging.getLogger(__name__)

LIST_URL = "https://cerebralvalley.ai/events"
LUMA_HREF_RE = re.compile(r"https://luma\.com/[a-z0-9][a-z0-9_-]{2,}(?:\?|$)", re.I)


def _clean(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/")


class CerebralValleyScraper(BaseScraper):
    name = "cerebral_valley"

    def scrape(self, limit: int = 40) -> list[RawEvent]:
        out: list[RawEvent] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            try:
                page.goto(LIST_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2500)
                # scroll to load more
                for _ in range(4):
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(1200)
            except PWTimeout:
                log.warning("timeout on CV list")
                browser.close()
                return out
            hrefs: list[str] = page.eval_on_selector_all(
                "a[href*='luma.com/']", "els => [...new Set(els.map(e => e.href))]"
            )
            luma_urls = sorted({_clean(h) for h in hrefs if LUMA_HREF_RE.match(h)})
            # filter out profile pages
            luma_urls = [
                u for u in luma_urls
                if "/signin" not in u and "/discover" not in u and "/user/" not in u and not self.should_skip(u)
            ]
            log.info("found %d luma urls on CV", len(luma_urls))
            for i, url in enumerate(luma_urls[:limit], 1):
                try:
                    log.info("[%d/%d] CV->Luma %s", i, min(limit, len(luma_urls)), url)
                    page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    page.wait_for_timeout(800)
                except PWTimeout:
                    log.warning("timeout %s", url)
                    continue
                events = find_events(extract_jsonld(page))
                if not events:
                    continue
                data = events[0]
                name = data.get("name")
                if not name:
                    continue
                venue, locality = get_location_fields(data)
                out.append(
                    RawEvent(
                        source="cerebral_valley",
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
                        registration_url=registration_url_from_meta(page.eval_on_selector_all(
                            "meta[name='apple-itunes-app']", "els => els.map(e => e.content)",
                        )),
                    )
                )
            browser.close()
        log.info("scraped %d events via cerebral_valley", len(out))
        return out
