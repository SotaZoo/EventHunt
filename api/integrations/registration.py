"""Read explicit public registration facts without logging page content.

Structured status vocabulary: https://schema.org/EventCancelled and
https://schema.org/ItemAvailability. Controls complement offers, because
regular tickets can be sold out while a joinable waitlist remains available.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from time import monotonic
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from api.models.availability import AvailabilityObservation, RegistrationAvailability
from api.models.occurrences import provider_identity, supported_registration_url

MAX_PAGE_BYTES = 4 * 1024 * 1024
# A small per-source budget bounds memory, network time and redirect work.
REQUEST_BUDGET_SECONDS = 20
REQUEST_TIMEOUT_SECONDS = 8
MAX_REQUESTS = 4
_CONTROLS: dict[str, RegistrationAvailability] = {
    "join waitlist": "waitlist", "join the waitlist": "waitlist", "join wait list": "waitlist",
    "registration closed": "closed", "registration has closed": "closed", "sales ended": "closed",
    "sold out": "full", "event full": "full", "registration full": "full",
    "event canceled": "canceled", "event cancelled": "canceled", "canceled": "canceled", "cancelled": "canceled",
    "register": "open", "register now": "open", "get tickets": "open", "reserve a spot": "open",
    "rsvp": "open", "rsvp now": "open", "request to join": "open",
}


def _event_nodes(value: object) -> Iterator[dict]:
    if isinstance(value, list):
        for child in value:
            yield from _event_nodes(child)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if isinstance(types, list) and any(isinstance(t, str) and t.endswith("Event") for t in types):
            yield value
        else:
            for child in value.values():
                yield from _event_nodes(child)


def _schema_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    for prefix in ("https://schema.org/", "http://schema.org/"):
        if value.startswith(prefix):
            return value.removeprefix(prefix)
    return value if "/" not in value else ""


def _structured_observation(soup: BeautifulSoup, url: str) -> AvailabilityObservation:
    events: list[dict] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            events.extend(_event_nodes(json.loads(script.get_text())))
        except (json.JSONDecodeError, TypeError):
            continue
    matching = [event for event in events if isinstance(event.get("url"), str)
                and provider_identity(event["url"]) == provider_identity(url)]
    if not matching and len(events) == 1 and not events[0].get("url"):
        matching = events
    if len(matching) != 1:
        return AvailabilityObservation()
    event = matching[0]
    if _schema_name(event.get("eventStatus")) == "EventCancelled":
        return AvailabilityObservation(state="canceled", evidence="schema.org eventStatus: EventCancelled")
    offers = event.get("offers", [])
    offers = [offers] if isinstance(offers, dict) else offers
    if not isinstance(offers, list) or not offers:
        return AvailabilityObservation()
    states = [_schema_name(offer.get("availability")) if isinstance(offer, dict) else ""
              for offer in offers]
    if "InStock" in states or "LimitedAvailability" in states:
        return AvailabilityObservation(state="open", evidence="schema.org offers: InStock/LimitedAvailability")
    if all(state in {"SoldOut", "OutOfStock"} for state in states):
        return AvailabilityObservation(state="full", evidence="schema.org offers: all SoldOut/OutOfStock")
    if all(state == "Discontinued" for state in states):
        return AvailabilityObservation(state="closed", evidence="schema.org offers: all Discontinued")
    return AvailabilityObservation()


def _visible_control(element: Tag) -> bool:
    for node in (element, *element.parents):
        if not isinstance(node, Tag):
            continue
        style = str(node.get("style", "")).replace(" ", "").casefold()
        if node.has_attr("hidden") or node.get("aria-hidden") == "true" or "display:none" in style or "visibility:hidden" in style:
            return False
        if node.name in {"nav", "footer"}:
            return False
    return True


def _control_label(element: Tag) -> str:
    return " ".join(element.get_text(" ", strip=True).casefold().split())


def _enabled_control(element: Tag) -> bool:
    if element.get("aria-disabled") == "true":
        return False
    # Anchors do not inherit native form-control disabling.
    if element.name != "button":
        return True
    if element.has_attr("disabled"):
        return False
    for fieldset in element.find_parents("fieldset", disabled=True):
        # Native HTML exempts descendants of each fieldset's first legend child.
        legend = fieldset.find("legend", recursive=False)
        if legend is None or not any(parent is legend for parent in element.parents):
            return False
    return True


def _form_owner(soup: BeautifulSoup, element: Tag) -> Tag | None:
    owner_id = element.get("form") if element.name == "button" else None
    return soup.find("form", id=owner_id) if owner_id is not None else element.find_parent("form")


def _registration_controls(soup: BeautifulSoup, url: str) -> Iterator[Tag]:
    # Respect native form ownership and per-button overrides, not just ancestry.
    for element in soup.select('button'):
        form = _form_owner(soup, element)
        if form is None:
            continue
        if str(element.get("type", "submit")).casefold() != "submit":
            continue
        if str(element.get("formmethod", form.get("method", "get"))).casefold() not in {"get", "post"}:
            continue
        target = str(element.get("formaction", form.get("action", ""))).strip()
        action = urljoin(url, target)
        if target and provider_identity(action) == provider_identity(url):
            yield element


def _possible_waitlist(soup: BeautifulSoup, url: str) -> bool:
    # A JavaScript-only waitlist cannot prove joinability, but also cannot prove
    # that sold-out regular tickets make the whole event permanently unavailable.
    for element in soup.select('button, a[href]'):
        if _CONTROLS.get(_control_label(element)) != "waitlist" or not _visible_control(element) or not _enabled_control(element):
            continue
        form = _form_owner(soup, element)
        if str(element.get("type", "")).casefold() == "reset":
            continue
        if form is not None and str(element.get("formmethod", form.get("method", "get"))).casefold() == "dialog":
            continue
        target = element.get("href") if element.name == "a" else element.get("formaction", form.get("action", "") if form else "")
        destination = urljoin(url, str(target or ""))
        if supported_registration_url(destination) and provider_identity(destination) != provider_identity(url):
            continue
        return True
    return False


def extract_registration_observation(html: str, url: str) -> AvailabilityObservation:
    """Extract event-owned evidence without mistaking one ticket for the whole event."""
    soup = BeautifulSoup(html, "html.parser")
    structured = _structured_observation(soup, url)
    signals: dict[RegistrationAvailability, AvailabilityObservation] = {}
    # This is the whole-event status bar observed on Eventbrite's public event page.
    bars = [bar for bar in soup.select('[class*="EventSignalsBar-"][class*="__signals"]') if _visible_control(bar)]
    if provider_identity(url)[0] == "eventbrite" and len(bars) == 1:
        for element in bars[0].select('[class*="SignalBadge-"]'):
            label = _control_label(element)
            state = _CONTROLS.get(label)
            if state and _visible_control(element):
                signals[state] = AvailabilityObservation(state=state, evidence=f"eventbrite event signal: {label}")
    if structured.state == "canceled":
        return structured
    if "canceled" in signals:
        return signals["canceled"]
    controls: dict[RegistrationAvailability, AvailabilityObservation] = {}
    for element in _registration_controls(soup, url):
        if not _visible_control(element):
            continue
        label = _control_label(element)
        state = _CONTROLS.get(label)
        if not state:
            continue
        # A canceled/sold-out ticket action is not a whole-event status.
        if state == "canceled" or label in {"sold out", "sales ended"}:
            continue
        if state in {"open", "waitlist"} and not _enabled_control(element):
            continue
        controls[state] = AvailabilityObservation(state=state, evidence=f"event registration control: {label}")
    terminal_signal = next((signals[state] for state in ("closed", "full") if state in signals), None)
    event_open = "open" in controls or structured.state == "open"
    if "waitlist" in controls and "open" not in controls and (not event_open or terminal_signal):
        return controls["waitlist"]
    if (terminal_signal or structured.state in {"closed", "full"}) and "open" not in controls and _possible_waitlist(soup, url):
        return AvailabilityObservation()
    if terminal_signal:
        return terminal_signal
    if structured.state == "open":
        return structured
    for state in ("open", "waitlist"):
        if state in controls:
            return controls[state]
    if structured.state != "unknown":
        return structured
    for state in ("closed", "full"):
        if state in controls:
            return controls[state]
    return AvailabilityObservation()


def _fetch(url: str, client: httpx.Client) -> AvailabilityObservation:
    deadline = monotonic() + REQUEST_BUDGET_SECONDS
    original_identity = provider_identity(url)
    current = url
    try:
        for _ in range(MAX_REQUESTS):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            with client.stream("GET", current, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining), follow_redirects=False) as response:
                if response.is_redirect:
                    destination = urljoin(current, response.headers.get("Location", ""))
                    if not supported_registration_url(destination) or provider_identity(destination) != original_identity:
                        break
                    current = destination
                    continue
                if response.status_code != 200:
                    break
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > MAX_PAGE_BYTES or monotonic() > deadline:
                        return AvailabilityObservation()
                    body.extend(chunk)
                return extract_registration_observation(body.decode("utf-8", errors="replace"), current)
    except (httpx.HTTPError, ValueError):
        pass
    return AvailabilityObservation()


def fetch_registration_availability(url: str, *, client: httpx.Client | None = None) -> AvailabilityObservation:
    """Check a supported public source with bounded, validated outbound requests."""
    if not supported_registration_url(url):
        return AvailabilityObservation()
    if client is not None:
        return _fetch(url, client)
    with httpx.Client(headers={"User-Agent": "EventHunt/0.3 public-event-availability"}, trust_env=False) as owned:
        return _fetch(url, owned)
