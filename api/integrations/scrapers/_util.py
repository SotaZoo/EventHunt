"""Shared scraping helpers."""
from __future__ import annotations

import json
import os
from datetime import datetime

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout

from api.models.events import AttendanceMode

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def configured_listing_urls(setting: str, defaults: list[str]) -> list[str]:
    """Read whitespace-separated public feed URLs or use the bundled feeds."""
    return os.environ.get(setting, "").split() or defaults


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_jsonld(page: Page) -> list[dict]:
    """Return all JSON-LD blocks as parsed dicts (flattening @graph and lists)."""
    try:
        scripts = page.eval_on_selector_all(
            "script[type='application/ld+json']",
            "els => els.map(e => e.textContent)",
        )
    except PWTimeout:
        return []
    out: list[dict] = []
    for raw in scripts:
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                out.extend(x for x in graph if isinstance(x, dict))
            else:
                out.append(item)
    return out


def find_events(ld_objs: list[dict]) -> list[dict]:
    """Return every object with @type == 'Event'."""
    events: list[dict] = []
    for obj in ld_objs:
        t = obj.get("@type")
        if t == "Event" or (isinstance(t, list) and "Event" in t):
            events.append(obj)
        # ItemList form (Eventbrite)
        items = obj.get("itemListElement")
        if isinstance(items, list):
            for li in items:
                if isinstance(li, dict):
                    inner = li.get("item") if isinstance(li.get("item"), dict) else li
                    if isinstance(inner, dict):
                        it = inner.get("@type")
                        if it == "Event" or (isinstance(it, list) and "Event" in it):
                            events.append(inner)
                        elif inner.get("startDate"):
                            # Eventbrite search items have startDate w/ no @type
                            events.append(inner)
    return events


def get_location_fields(data: dict) -> tuple[str | None, str | None]:
    """Return (venue_name, locality)."""
    loc = data.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    venue = None
    locality = None
    if isinstance(loc, dict):
        venue = loc.get("name")
        addr = loc.get("address")
        if isinstance(addr, list):
            addr = addr[0] if addr else {}
        if isinstance(addr, dict):
            locality = addr.get("addressLocality") or addr.get("addressRegion")
    return venue, locality


def get_attendance_mode(data: dict) -> AttendanceMode | None:
    """Keep explicit schema.org evidence, never guess from a venue's name."""
    modes: dict[str, AttendanceMode] = {
        "OfflineEventAttendanceMode": "in-person",
        "OnlineEventAttendanceMode": "online",
        "MixedEventAttendanceMode": "mixed",
    }
    value = data.get("eventAttendanceMode")
    if isinstance(value, str):
        for name, mode in modes.items():
            if value in {name, f"https://schema.org/{name}", f"http://schema.org/{name}"}:
                return mode
    locations = data.get("location") or []
    if isinstance(locations, dict):
        locations = [locations]
    if not isinstance(locations, list):
        return None
    types = set()
    for location in locations:
        if isinstance(location, dict):
            kind = location.get("@type")
            if isinstance(kind, str):
                types.add(kind)
    if {"Place", "VirtualLocation"} <= types:
        return "mixed"
    if "VirtualLocation" in types:
        return "online"
    if "Place" in types:
        return "in-person"
    return None


def get_organizer(data: dict) -> str | None:
    org = data.get("organizer")
    if isinstance(org, list) and org:
        org = org[0]
    if isinstance(org, dict):
        return org.get("name")
    return None


def get_image(data: dict) -> str | None:
    img = data.get("image")
    if isinstance(img, list) and img:
        return img[0] if isinstance(img[0], str) else img[0].get("url")
    if isinstance(img, str):
        return img
    if isinstance(img, dict):
        return img.get("url")
    return None
