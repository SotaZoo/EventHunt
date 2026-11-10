from __future__ import annotations

import html
import json
import logging
import os
import re
from collections.abc import Iterator

import httpx

from api.models.events import RawEvent

from ._util import UA, parse_dt
from .base import BaseScraper

log = logging.getLogger(__name__)

LIST_URL = "https://partiful.com/explore/nyc"
EVENT_URL_BASE = "https://partiful.com/e/"
NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _iter_partiful_items(value: object) -> Iterator[dict]:
    if isinstance(value, dict):
        event = value.get("event")
        if value.get("type") == "event" and isinstance(event, dict):
            yield event
        for child in value.values():
            yield from _iter_partiful_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_partiful_items(child)


def _extract_next_data(html_text: str) -> dict | None:
    match = NEXT_DATA_RE.search(html_text)
    if not match:
        return None
    try:
        data = json.loads(html.unescape(match.group(1)))
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _image_url(event: dict) -> str | None:
    image = event.get("image")
    if not isinstance(image, dict):
        return None
    url = image.get("url")
    if isinstance(url, str) and url:
        return url
    upload = image.get("upload")
    if isinstance(upload, dict):
        upload_url = upload.get("url")
        if isinstance(upload_url, str) and upload_url:
            return upload_url
    return None


def _location_fields(event: dict) -> tuple[str | None, str | None]:
    loc = event.get("locationInfo")
    if not isinstance(loc, dict):
        return None, None

    if loc.get("type") == "freeform":
        value = loc.get("value")
        return (value, None) if isinstance(value, str) and value else (None, None)

    maps = loc.get("mapsInfo") if isinstance(loc.get("mapsInfo"), dict) else {}
    venue = loc.get("displayName") or maps.get("name")
    if not isinstance(venue, str) or not venue:
        display_lines = loc.get("displayAddressLines")
        if isinstance(display_lines, list) and display_lines:
            first = display_lines[0]
            venue = first if isinstance(first, str) else None
    neighborhood = loc.get("neighborhood") or maps.get("approximateLocation")
    if not isinstance(neighborhood, str) or not neighborhood:
        neighborhood = None
    return venue if isinstance(venue, str) and venue else None, neighborhood


def _event_to_raw(event: dict) -> RawEvent | None:
    if event.get("isPublic") is not True or event.get("status") != "PUBLISHED":
        return None
    event_id = event.get("id")
    title = event.get("title")
    if not isinstance(event_id, str) or not event_id:
        return None
    if not isinstance(title, str) or not title:
        return None

    venue, neighborhood = _location_fields(event)
    description = event.get("description")
    return RawEvent(
        source="partiful",
        url=f"{EVENT_URL_BASE}{event_id}",
        title=title,
        starts_at=parse_dt(event.get("startDate")),
        ends_at=parse_dt(event.get("endDate")),
        venue=venue,
        neighborhood=neighborhood,
        description=description if isinstance(description, str) else None,
        host="Partiful",
        cover_image=_image_url(event),
    )


def extract_events_from_next_data(html_text: str, *, limit: int) -> list[RawEvent]:
    data = _extract_next_data(html_text)
    if data is None:
        return []

    out: list[RawEvent] = []
    seen: set[str] = set()
    for event in _iter_partiful_items(data):
        raw = _event_to_raw(event)
        if raw is None or raw.url in seen:
            continue
        seen.add(raw.url)
        out.append(raw)
        if len(out) >= limit:
            break
    return out


class PartifulScraper(BaseScraper):
    name = "partiful"

    def scrape(self, limit: int = 60) -> list[RawEvent]:
        list_url = os.environ.get("EVENT_HUNT_PARTIFUL_LIST_URL", "").strip() or LIST_URL
        headers = {"User-Agent": UA, "Accept": "text/html,*/*"}
        try:
            resp = httpx.get(list_url, headers=headers, timeout=20.0, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("failed to fetch %s: %s", list_url, exc)
            return []

        events = extract_events_from_next_data(resp.text, limit=limit)
        log.info("scraped %d events from partiful", len(events))
        return events
