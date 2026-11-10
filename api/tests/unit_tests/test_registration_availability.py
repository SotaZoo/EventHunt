"""External HTTP is mocked; source classification runs as it will in the worker."""
import json

import httpx
import pytest

from api.tests.fixtures.registration_helpers import registration_form

URL = "https://lu.ma/builders"


def event_html(**fields):
    """Describe the current synthetic event in standard structured metadata."""
    data = {"@type": "Event", "url": URL, "eventStatus": "https://schema.org/EventScheduled", **fields}
    return '<script type="application/ld+json">' + json.dumps(data) + '</script>'


def offer(availability):
    """Describe a synthetic ticket offer using the source's availability vocabulary."""
    return {"@type": "Offer", "availability": f"https://schema.org/{availability}"}


@pytest.mark.parametrize(("html", "expected"), [
    (event_html(eventStatus="https://schema.org/EventCancelled"), "canceled"),
    (event_html(eventStatus="https://schema.org/EventPostponed"), "unknown"),
    (event_html(), "unknown"),
    (event_html(offers=offer("InStock")), "open"),
    (event_html(offers=offer("SoldOut")), "full"),
    (event_html(offers=[offer("SoldOut"), offer("InStock")]), "open"),
    (event_html(offers=[offer("SoldOut"), {"@type": "Offer"}]), "unknown"),
    (event_html(offers=offer("SoldOut")) + registration_form(URL, '<button>Join Waitlist</button>'), "waitlist"),
    (event_html(eventStatus="https://schema.org/EventCancelled") + registration_form(URL, '<button>Join Waitlist</button>'), "canceled"),
    (event_html(offers=offer("SoldOut")) + registration_form(URL, '<button disabled>Join Waitlist</button>'), "full"),
    (event_html(offers=offer("SoldOut")) + registration_form(URL, '<div hidden><button>Join Waitlist</button></div>'), "full"),
    (registration_form(URL, '<button>Register</button>'), "open"),
    (registration_form(URL, '<button disabled>Registration closed</button>'), "closed"),
    ('<button>Join Waitlist</button>', "unknown"),
    ('<button disabled>Registration closed</button>', "unknown"),
    ('<div role="status">Sold out</div>', "unknown"),
    ('<p>Last year sold out. This event is not canceled.</p>', "unknown"),
    ('<p>Zero spots remain.</p>', "unknown"),
    (event_html(url="https://lu.ma/unrelated", offers=offer("SoldOut")), "unknown"),
    ('<script type="application/ld+json">{"@type":"Product","offers":{"availability":"https://schema.org/SoldOut"}}</script>', "unknown"),
])
def test_source_facts_and_controls_not_description_keywords(html, expected):
    from api.integrations.registration import fetch_registration_availability

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))) as client:
        observation = fetch_registration_availability(URL, client=client)
    assert observation.state == expected
    if expected != "unknown":
        assert observation.evidence and len(observation.evidence) <= 240
        assert observation.checked_at.tzinfo is not None


def test_eventbrite_observed_event_signal_badge_is_closed():
    from api.integrations.registration import fetch_registration_availability

    # Minimal public markup observed on the reported Eventbrite listing.
    html = '''<div class="EventSignalsBar-module-scss-module__nYcTyq__container">
        <div class="EventSignalsBar-module-scss-module__nYcTyq__innerWrapper">
            <div class="EventSignalsBar-module-scss-module__nYcTyq__signals">
                <span class="SignalBadge-module-scss-module__L1lgJW__eventEnded">Sales ended</span>
            </div><div class="EventSignalsBar-module-scss-module__nYcTyq__actionButtons"></div>
        </div></div>'''
    url = "https://www.eventbrite.com/e/builders-tickets-1234567890"
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))) as client:
        assert fetch_registration_availability(url, client=client).state == "closed"


@pytest.mark.parametrize("url", [
    "http://lu.ma/builders", "https://localhost/builders", "https://127.0.0.1/",
    "https://lu.ma.evil.test/builders", "https://user:dummy-password@lu.ma/builders",
    "https://lu.ma:8888/builders", "https://www.eventbrite.com/login", "https://example.test/event",
    "https://lu.ma:0/builders",
    "https://voxel51.com/events/synthetic-physical-ai-meetup",
])
def test_unsupported_or_unsafe_urls_are_not_requested(url):
    from api.integrations.registration import fetch_registration_availability

    def unexpected(request):
        pytest.fail("unsafe request was sent")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        assert fetch_registration_availability(url, client=client).state == "unknown"


def test_redirect_to_private_or_unrelated_destination_is_not_followed():
    from api.integrations.registration import fetch_registration_availability

    requests = []

    def source(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:8888/private"})

    with httpx.Client(transport=httpx.MockTransport(source)) as client:
        assert fetch_registration_availability(URL, client=client).state == "unknown"
    assert requests == [URL]


@pytest.mark.parametrize("status", [403, 404, 429, 500])
def test_failed_requests_do_not_prove_registration_closed(status):
    from api.integrations.registration import fetch_registration_availability

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, text="Canceled"))) as client:
        assert fetch_registration_availability(URL, client=client).state == "unknown"


@pytest.mark.parametrize("other", [
    '<section><h2>VIP ticket</h2><div role="status">Sold out</div></section>',
    '<button disabled>Sold out</button>',
    '<aside><a href="https://lu.ma/different-event">Sold out</a></aside>',
    '<aside><button>Event canceled</button></aside>',
    '<button>Join Waitlist</button>',
])
def test_ticket_categories_and_unrelated_controls_cannot_override_event_open(other):
    from api.integrations.registration import extract_registration_observation

    html = event_html(offers=offer("InStock")) + other
    assert extract_registration_observation(html, URL).state == "open"


def test_controls_for_another_event_do_not_classify_the_target():
    from api.integrations.registration import extract_registration_observation

    html = '<form action="https://lu.ma/different-event"><button disabled>Registration closed</button></form>'
    assert extract_registration_observation(html, URL).state == "unknown"


def test_same_provider_redirect_to_different_event_is_not_followed():
    from api.integrations.registration import fetch_registration_availability

    requests = []

    def source(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://lu.ma/different-event"})

    with httpx.Client(transport=httpx.MockTransport(source)) as client:
        assert fetch_registration_availability(URL, client=client).state == "unknown"
    assert requests == [URL]


def test_canonical_provider_alias_redirect_preserves_event_identity():
    from api.integrations.registration import fetch_registration_availability

    requests = []

    def source(request):
        requests.append(str(request.url))
        if len(requests) == 1:
            return httpx.Response(302, headers={"Location": "https://luma.com/builders"})
        return httpx.Response(200, text=event_html(offers=offer("InStock")))

    with httpx.Client(transport=httpx.MockTransport(source)) as client:
        assert fetch_registration_availability(URL, client=client).state == "open"
    assert requests == [URL, "https://luma.com/builders"]


def test_redirect_loop_is_bounded():
    from api.integrations.registration import fetch_registration_availability

    requests = []

    def source(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": URL})

    with httpx.Client(transport=httpx.MockTransport(source)) as client:
        assert fetch_registration_availability(URL, client=client).state == "unknown"
    assert len(requests) == 4


def test_oversized_source_response_is_unknown():
    from api.integrations.registration import MAX_PAGE_BYTES, fetch_registration_availability

    html = event_html(offers=offer("InStock")) + ' ' * MAX_PAGE_BYTES
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))) as client:
        assert fetch_registration_availability(URL, client=client).state == "unknown"


def test_source_response_after_deadline_is_unknown(monkeypatch):
    from api.integrations.registration import fetch_registration_availability

    clock = iter([0, 0, 21])
    monkeypatch.setattr("api.integrations.registration.monotonic", lambda: next(clock))
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=event_html(offers=offer("InStock"))))) as client:
        assert fetch_registration_availability(URL, client=client).state == "unknown"


def test_expired_budget_prevents_another_request(monkeypatch):
    from api.integrations.registration import fetch_registration_availability

    clock = iter([0, 21])
    monkeypatch.setattr("api.integrations.registration.monotonic", lambda: next(clock))
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("expired request budget"))) as client:
        assert fetch_registration_availability(URL, client=client).state == "unknown"


@pytest.mark.parametrize("controls, expected", [
    ('<button disabled>Sold out</button><button>Register</button>', "open"),
    ('<button>Join Waitlist</button><button>Register</button>', "open"),
    ('<button disabled>Sold out</button>', "unknown"),
    ('<a href="https://lu.ma/different-event">Registration closed</a>', "unknown"),
    ('<a href="https://lu.ma/different-event">Join Waitlist</a>', "unknown"),
])
def test_registration_form_does_not_hide_available_tickets_or_follow_other_events(controls, expected):
    from api.integrations.registration import extract_registration_observation

    assert extract_registration_observation(registration_form(URL, controls), URL).state == expected


@pytest.mark.parametrize("controls", [
    '<button formaction="https://lu.ma/different-event">Join Waitlist</button>',
    '<button formaction="https://lu.ma/different-event">Register</button>',
    '<button form="other">Join Waitlist</button>',
    '<button type="button">Join Waitlist</button>',
    '<button type="reset">Register</button>',
    '<button formmethod="dialog">Register</button>',
    '<fieldset disabled><button>Join Waitlist</button></fieldset>',
])
def test_registration_controls_need_their_actual_event_owner_and_action(controls):
    from api.integrations.registration import extract_registration_observation

    html = registration_form(URL, controls) + '<form id="other" action="https://lu.ma/different-event"></form>'
    assert extract_registration_observation(html, URL).state == "unknown"


def test_explicit_form_owner_and_canonical_action_for_current_event_are_accepted():
    from api.integrations.registration import extract_registration_observation

    html = '<form id="current" action="https://luma.com/builders"></form><button form="current" formaction="https://lu.ma/builders">Join Waitlist</button>'
    assert extract_registration_observation(html, URL).state == "waitlist"


@pytest.mark.parametrize("controls, expected", [
    ('<fieldset disabled><legend><button>Join Waitlist</button></legend></fieldset>', "waitlist"),
    ('<fieldset disabled><legend>Tickets</legend><legend><button>Join Waitlist</button></legend></fieldset>', "full"),
    ('<fieldset disabled><fieldset disabled><legend><button>Join Waitlist</button></legend></fieldset></fieldset>', "full"),
    ('<fieldset disabled><legend><fieldset disabled><legend><button>Join Waitlist</button></legend></fieldset></legend></fieldset>', "waitlist"),
    ('<fieldset disabled><legend><button disabled>Join Waitlist</button></legend></fieldset>', "full"),
])
def test_native_first_legend_exception_applies_to_each_disabled_fieldset(controls, expected):
    from api.integrations.registration import extract_registration_observation

    html = event_html(offers=offer("SoldOut")) + registration_form(URL, controls)
    assert extract_registration_observation(html, URL).state == expected


@pytest.mark.parametrize("controls", [
    '<button type="button">Join Waitlist</button>',
    registration_form(URL, '<button type="button">Join Waitlist</button>'),
    '<a href="#waitlist">Join Waitlist</a>',
    registration_form(URL, '<a href="#waitlist">Join Waitlist</a>'),
    registration_form(URL, '<fieldset disabled><a href="#waitlist">Join Waitlist</a></fieldset>'),
    '<a disabled href="#waitlist">Join Waitlist</a>',
])
def test_unproven_possible_waitlist_is_not_proof_that_the_event_is_terminal(controls):
    from api.integrations.registration import extract_registration_observation

    assert extract_registration_observation(event_html(offers=offer("SoldOut")) + controls, URL).state == "unknown"
