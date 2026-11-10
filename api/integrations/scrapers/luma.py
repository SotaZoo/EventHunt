from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

import httpx
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

from api.models.events import RawEvent

from ._util import configured_listing_urls
from .base import BaseScraper

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Luma event slug pattern: https://luma.com/<slug>
EVENT_URL_RE = re.compile(r"^https://luma\.com/[a-z0-9][a-z0-9_-]{2,}/?$", re.I)
SKIP_PATHS = {"/discover", "/create", "/login", "/signup", "/nyc", "/sf", "/la"}

LIST_URLS = [
    "https://luma.com/nyc",
    "https://luma.com/fdotinc",  # Founders Inc hosts events on Luma
]

# Inline JSON-LD blocks on a Luma event page. The capacity poller fetches
# a single page over plain HTTP (no headless browser) — Luma server-renders
# the JSON-LD into the initial HTML for SEO, so a static fetch is enough
# to read ``remainingAttendeeCapacity`` and ``eventStatus``. Using a regex
# (instead of an HTML parser) keeps the polling task dependency-free.
JSONLD_SCRIPT_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
# schema.org eventStatus values. Luma writes the full URL form
# (``https://schema.org/EventCancelled``); we tolerate the short form too
# in case a future page revision strips the namespace.
CANCELLED_STATUSES = frozenset(
    {
        "https://schema.org/EventCancelled",
        "http://schema.org/EventCancelled",
        "EventCancelled",
    }
)


@dataclass(frozen=True)
class CapacitySnapshot:
    """One capacity poll's reading of a single Luma event page.

    ``capacity_remaining`` is ``None`` when the page does not expose
    ``remainingAttendeeCapacity`` (e.g., the host disabled the spot count,
    or the event is free-RSVP with no cap). The capacity poller treats
    ``None`` as "no information this tick" — no threshold crossings fire
    and ``last_known_capacity`` is left unchanged.

    ``cancelled`` is True iff ``eventStatus`` is one of the schema.org
    cancellation tokens. The poller fires a single cancellation PATCH
    and sets ``events.cancelled_at`` so subsequent polls skip the row.
    """

    capacity_remaining: int | None
    cancelled: bool


def _parse_capacity_value(value: object) -> int | None:
    """Best-effort coercion of ``remainingAttendeeCapacity`` to a non-negative int.

    Luma typically writes a JSON integer, but we tolerate stringified
    numbers in case the page contract drifts. Negative / unparseable
    values clamp to ``None`` so the renderer never sees ``-1 spots``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # ``bool`` is an ``int`` subclass in Python; reject explicitly to
        # avoid silently treating ``True`` as ``1 spot remaining``.
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        try:
            ivalue = int(value)
        except (ValueError, OverflowError):
            return None
        return ivalue if ivalue >= 0 else None
    if isinstance(value, str):
        try:
            ivalue = int(value.strip())
        except ValueError:
            return None
        return ivalue if ivalue >= 0 else None
    return None


def _extract_event_node(html: str) -> dict | None:
    """Return the first JSON-LD object with ``@type == Event`` from ``html``.

    Scans inline ``<script type="application/ld+json">`` blocks. Tolerates
    list payloads (``[ {...}, ... ]``) and missing / malformed JSON — a
    bad block returns ``None`` so the capacity poller skips this tick
    instead of crashing the whole sweep.
    """
    for match in JSONLD_SCRIPT_RE.finditer(html):
        try:
            data = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "Event":
                return item
    return None


class LumaCapacityFetchError(Exception):
    """Raised when a single-event capacity fetch fails transiently.

    The capacity poller catches this per-event so other events in the same
    poll cycle continue. Carries ``retry_after`` (seconds) when Luma
    returned a 429 with a ``Retry-After`` header so the per-host backoff
    can be honoured by the caller.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def fetch_capacity_for_event(
    event_url: str,
    *,
    timeout: float = 10.0,
    client: httpx.Client | None = None,
) -> CapacitySnapshot:
    """Fetch a single Luma event page and read capacity + cancellation status.

    Spec §4: the hourly capacity poller calls this for each pushed-and-
    future event. Issues ONE plain HTTP GET (no Playwright) — Luma
    server-renders the JSON-LD into the initial HTML so a static fetch is
    enough. This keeps the polling task cheap and lets ``pytest-httpx``
    mock the response directly in tests.

    Raises ``LumaCapacityFetchError`` on 429 / 5xx / timeouts so the
    caller can apply per-host exponential backoff and skip this event for
    the current poll cycle. Returns ``CapacitySnapshot(None, False)`` when
    the page has no parseable JSON-LD Event node (treated as "no info
    this tick" — same as ``None`` capacity).
    """
    headers = {"User-Agent": UA, "Accept": "text/html,*/*"}
    try:
        if client is not None:
            resp = client.get(event_url, headers=headers, timeout=timeout)
        else:
            resp = httpx.get(event_url, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise LumaCapacityFetchError(f"timeout: {exc}") from exc
    except httpx.RequestError as exc:
        raise LumaCapacityFetchError(f"network: {exc}") from exc

    if resp.status_code == 429:
        try:
            retry_after = float(resp.headers.get("Retry-After", "1"))
        except ValueError:
            retry_after = 1.0
        raise LumaCapacityFetchError(
            f"rate-limited: 429 retry_after={retry_after}",
            retry_after=retry_after,
        )
    if 500 <= resp.status_code < 600:
        raise LumaCapacityFetchError(f"server error: {resp.status_code}")
    if resp.status_code >= 400:
        # 4xx (other than 429) usually means the event was deleted or the
        # host made it private; surface as cancelled-equivalent so the
        # poller stops polling without firing an apology (cancellation
        # PATCH only fires when the host explicitly marked the event
        # cancelled via Luma's status field).
        raise LumaCapacityFetchError(f"client error: {resp.status_code}")

    event = _extract_event_node(resp.text)
    if event is None:
        return CapacitySnapshot(capacity_remaining=None, cancelled=False)

    status = event.get("eventStatus")
    cancelled = isinstance(status, str) and status in CANCELLED_STATUSES
    capacity = _parse_capacity_value(event.get("remainingAttendeeCapacity"))
    return CapacitySnapshot(capacity_remaining=capacity, cancelled=cancelled)


def _collect_event_urls(page: Page, max_scrolls: int = 6) -> list[str]:
    urls: set[str] = set()
    for _ in range(max_scrolls):
        found: list[str] = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => e.href)",
        )
        for href in found:
            if not EVENT_URL_RE.match(href):
                continue
            # strip trailing slash
            href = href.rstrip("/")
            # skip known non-event pages
            path = href.replace("https://luma.com", "")
            if path in SKIP_PATHS or len(path) < 4:
                continue
            urls.add(href)
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(1500)
    return sorted(urls)


def _extract_jsonld(page: Page) -> dict | None:
    try:
        scripts = page.eval_on_selector_all(
            "script[type='application/ld+json']",
            "els => els.map(e => e.textContent)",
        )
    except PWTimeout:
        return None
    for raw in scripts:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "Event":
                return item
    return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _jsonld_to_raw(url: str, data: dict) -> RawEvent | None:
    from ._util import get_attendance_mode

    title = data.get("name")
    if not title:
        return None
    loc = data.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get("address") or {}
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    venue = loc.get("name") if isinstance(loc, dict) else None
    neighborhood = None
    if isinstance(addr, dict):
        neighborhood = addr.get("addressLocality") or addr.get("addressRegion")
    organizer = data.get("organizer")
    host = None
    if isinstance(organizer, list) and organizer:
        host = organizer[0].get("name") if isinstance(organizer[0], dict) else None
    elif isinstance(organizer, dict):
        host = organizer.get("name")
    images = data.get("image")
    cover = None
    if isinstance(images, list) and images:
        cover = images[0]
    elif isinstance(images, str):
        cover = images
    return RawEvent(
        source="luma",
        attendance_mode=get_attendance_mode(data),
        url=url,
        title=title,
        starts_at=_parse_dt(data.get("startDate")),
        ends_at=_parse_dt(data.get("endDate")),
        venue=venue,
        neighborhood=neighborhood,
        description=data.get("description"),
        host=host,
        cover_image=cover,
    )


def registration_url_from_meta(contents: list[str]) -> str | None:
    """Retain the public Luma event ID only when app metadata is unambiguous."""
    ids = {match[1] for content in contents
           if (match := re.search(r"(?:^|,\s*)app-argument=luma://event/(evt-[A-Za-z0-9]+)\s*$", content))}
    return f"https://lu.ma/event/{ids.pop()}" if len(ids) == 1 else None


class LumaScraper(BaseScraper):
    name = "luma"

    def __init__(self, headless: bool = True, per_event_delay_ms: int = 800):
        self.headless = headless
        self.per_event_delay_ms = per_event_delay_ms

    def scrape(self, limit: int = 50) -> list[RawEvent]:
        out: list[RawEvent] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            event_urls: list[str] = []
            listing_urls = configured_listing_urls("EVENT_HUNT_LUMA_LIST_URLS", LIST_URLS)
            listing_pages = {url.rstrip("/") for url in listing_urls}
            for list_url in listing_urls:
                try:
                    log.info("fetching list page %s", list_url)
                    page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)
                    event_urls.extend(_collect_event_urls(page))
                except PWTimeout:
                    log.warning("timeout on list %s", list_url)
            event_urls = sorted(
                url for url in set(event_urls)
                if url.rstrip("/") not in listing_pages and not self.should_skip(url)
            )[:limit]
            log.info("found %d candidate event urls", len(event_urls))
            for i, url in enumerate(event_urls, 1):
                try:
                    log.info("[%d/%d] %s", i, len(event_urls), url)
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(self.per_event_delay_ms)
                    data = _extract_jsonld(page)
                    if not data:
                        log.warning("no JSON-LD on %s", url)
                        continue
                    raw = _jsonld_to_raw(url, data)
                    if raw:
                        raw.registration_url = registration_url_from_meta(page.eval_on_selector_all(
                            "meta[name='apple-itunes-app']", "els => els.map(e => e.content)",
                        ))
                        out.append(raw)
                except PWTimeout:
                    log.warning("timeout on event %s", url)
                except Exception as e:
                    log.warning("error on event %s: %s", url, e)
            browser.close()
        log.info("scraped %d events from luma", len(out))
        return out
