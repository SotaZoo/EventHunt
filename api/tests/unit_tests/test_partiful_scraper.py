from __future__ import annotations

import json

from api.integrations.scrapers import SCRAPERS, PartifulScraper
from api.integrations.scrapers.partiful import extract_events_from_next_data


def _html(next_data: dict) -> str:
    payload = json.dumps(next_data)
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{payload}</script></body></html>'


def test_partiful_is_registered() -> None:
    assert SCRAPERS["partiful"] is PartifulScraper


def test_extract_events_from_next_data_maps_public_events() -> None:
    doc = {
        "props": {
            "pageProps": {
                "sections": [
                    {
                        "items": [
                            {
                                "type": "event",
                                "event": {
                                    "id": "abc123",
                                    "title": "AI Builders Coffee",
                                    "description": "Meet technical founders.",
                                    "locationInfo": {
                                        "type": "structured",
                                        "displayName": "Cafe Example",
                                        "neighborhood": "NoHo",
                                        "mapsInfo": {
                                            "name": "Fallback Cafe",
                                            "approximateLocation": "New York, NY",
                                        },
                                    },
                                    "startDate": "2026-06-20T14:00:00.000Z",
                                    "endDate": "2026-06-20T16:00:00.000Z",
                                    "image": {"url": "https://images.example/cover.png"},
                                    "isPublic": True,
                                    "status": "PUBLISHED",
                                },
                            }
                        ]
                    }
                ]
            }
        }
    }

    events = extract_events_from_next_data(_html(doc), limit=10)

    assert len(events) == 1
    event = events[0]
    assert event.source == "partiful"
    assert event.url == "https://partiful.com/e/abc123"
    assert event.title == "AI Builders Coffee"
    assert event.starts_at is not None
    assert event.venue == "Cafe Example"
    assert event.neighborhood == "NoHo"
    assert event.description == "Meet technical founders."
    assert event.host == "Partiful"
    assert event.cover_image == "https://images.example/cover.png"


def test_extract_events_from_next_data_skips_duplicates_and_private_events() -> None:
    public_event = {
        "id": "same",
        "title": "Public Event",
        "locationInfo": {"type": "freeform", "value": "Brooklyn"},
        "startDate": "2026-06-20T14:00:00.000Z",
        "isPublic": True,
        "status": "PUBLISHED",
    }
    private_event = {
        "id": "private",
        "title": "Private Event",
        "isPublic": False,
        "status": "PUBLISHED",
    }
    draft_event = {
        "id": "draft",
        "title": "Draft Event",
        "isPublic": True,
        "status": "DRAFT",
    }
    doc = {
        "items": [
            {"type": "event", "event": public_event},
            {"type": "event", "event": public_event},
            {"type": "event", "event": private_event},
            {"type": "event", "event": draft_event},
        ]
    }

    events = extract_events_from_next_data(_html(doc), limit=10)

    assert len(events) == 1
    assert events[0].url == "https://partiful.com/e/same"
    assert events[0].venue == "Brooklyn"
