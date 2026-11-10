from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from api.models.events import RawEvent
from api.models.occurrences import registration_identity

from ._util import UA
from .base import BaseScraper

log = logging.getLogger(__name__)

LIST_URL = "https://www.betaworks.com/events"

# og:description format: "Apr 30, 2026 | Betaworks • 29 Little West 12th Street, New York, NY 10014"
DATE_RE = re.compile(r"([A-Z][a-z]{2,8}\s+\d{1,2},?\s+20\d{2})")
TIME_RE = re.compile(r"(\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM))")


def _parse_start(og_desc: str | None, body_text: str | None) -> datetime | None:
    if not og_desc:
        return None
    m = DATE_RE.search(og_desc)
    if not m:
        return None
    date_str = m.group(1)
    # Try formats
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            d = datetime.strptime(date_str, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    # Look for time in body
    if body_text:
        tm = TIME_RE.search(body_text)
        if tm:
            raw = tm.group(1).replace(" ", "").lower()
            for fmt in ("%I:%M%p", "%I%p"):
                try:
                    t = datetime.strptime(raw, fmt)
                    return d.replace(hour=t.hour, minute=t.minute, tzinfo=ZoneInfo("America/New_York"))
                except ValueError:
                    continue
    return None


def detail_metadata(html: str) -> tuple[str | None, str | None]:
    """Read the event's About text and unambiguous embedded Luma registration ID."""
    soup = BeautifulSoup(html, "html.parser")
    about = soup.select_one(".w-richtext")
    description = about.get_text("\n", strip=True) if about else None
    identities = {identity for anchor in soup.select(".luma-html-embed a[href]")
                  if (identity := registration_identity(anchor["href"])) and identity[0] == "luma-id"}
    registration_url = f"https://lu.ma/event/{identities.pop()[1]}" if len(identities) == 1 else None
    return description, registration_url


class BetaworksScraper(BaseScraper):
    name = "betaworks"

    def scrape(self, limit: int = 30) -> list[RawEvent]:
        out: list[RawEvent] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            try:
                page.goto(LIST_URL, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
            except PWTimeout:
                log.warning("timeout on list")
                browser.close()
                return out
            event_urls: list[str] = page.eval_on_selector_all(
                "a[href*='/event/']",
                "els => [...new Set(els.map(e => e.href).filter(h => /betaworks\\.com\\/event\\//.test(h)))]",
            )
            event_urls = [url for url in event_urls if not self.should_skip(url)]
            log.info("found %d betaworks event urls", len(event_urls))
            for i, url in enumerate(event_urls[:limit], 1):
                try:
                    log.info("[%d/%d] %s", i, min(limit, len(event_urls)), url)
                    page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    page.wait_for_timeout(1000)
                except PWTimeout:
                    log.warning("timeout on %s", url)
                    continue
                title = page.eval_on_selector("meta[property='og:title']", "e => e.content") or ""
                desc = (
                    page.eval_on_selector("meta[property='og:description']", "e => e.content")
                    or ""
                )
                image = (
                    page.eval_on_selector("meta[property='og:image']", "e => e.content") or None
                )
                body_text = page.evaluate("document.body.innerText")[:3000]
                about, registration_url = detail_metadata(page.content())
                starts_at = _parse_start(desc, body_text)
                # parse venue from desc: "<date> | <venue> • <address>"
                venue = None
                neighborhood = None
                if "|" in desc:
                    try:
                        tail = desc.split("|", 1)[1].strip()
                        if "•" in tail:
                            venue = tail.split("•", 1)[0].strip()
                            addr = tail.split("•", 1)[1].strip()
                            # crude: extract "New York" or "Brooklyn" etc.
                            if "New York" in addr:
                                neighborhood = "New York"
                            elif "Brooklyn" in addr:
                                neighborhood = "Brooklyn"
                    except (IndexError, ValueError):
                        pass
                if not title:
                    continue
                out.append(
                    RawEvent(
                        source="betaworks",
                        url=url,
                        title=title,
                        starts_at=starts_at,
                        venue=venue,
                        neighborhood=neighborhood,
                        description=about or desc,
                        registration_url=registration_url,
                        host="Betaworks",
                        cover_image=image,
                    )
                )
            browser.close()
        log.info("scraped %d events from betaworks", len(out))
        return out
