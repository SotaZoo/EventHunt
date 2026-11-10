from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from pytest_httpx import HTTPXMock

from api.integrations.discord.webhook import (
    PostStatus,
    patch_event,
    post_event,
)

WEBHOOK = "https://discord.com/api/webhooks/123/abc"
# ``?wait=true`` is REQUIRED on every POST (spec §1); without it Discord
# returns 204 No Content and the capacity ticker / retract paths have no
# message id to PATCH. Tests target the final URL so a regression that
# drops the query param is caught here instead of in production.
WEBHOOK_WITH_WAIT = f"{WEBHOOK}?wait=true"
EMBED = {"title": "x", "description": "y"}


def test_post_success(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=WEBHOOK_WITH_WAIT, method="POST", status_code=204)
    result = post_event(WEBHOOK, EMBED)
    assert result.status == PostStatus.OK
    assert result.retry_after is None


def test_post_429_returns_retryable_with_retry_after(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=WEBHOOK_WITH_WAIT, method="POST", status_code=429,
        headers={"Retry-After": "2.5"},
    )
    result = post_event(WEBHOOK, EMBED)
    assert result.status == PostStatus.RETRYABLE
    assert result.retry_after == pytest.approx(2.5)


def test_post_5xx_retryable(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=WEBHOOK_WITH_WAIT, method="POST", status_code=503)
    result = post_event(WEBHOOK, EMBED)
    assert result.status == PostStatus.RETRYABLE


def test_post_404_fatal(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=WEBHOOK_WITH_WAIT, method="POST", status_code=404)
    result = post_event(WEBHOOK, EMBED)
    assert result.status == PostStatus.FATAL
    assert "404" in (result.detail or "")


def test_post_400_fatal(httpx_mock: HTTPXMock):
    """Bad payload — don't blindly retry."""
    httpx_mock.add_response(
        url=WEBHOOK_WITH_WAIT, method="POST", status_code=400,
        json={"message": "Embeds invalid"},
    )
    result = post_event(WEBHOOK, EMBED)
    assert result.status == PostStatus.FATAL


def test_post_sends_embed_in_payload(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=WEBHOOK_WITH_WAIT, method="POST", status_code=204)
    post_event(WEBHOOK, EMBED)
    sent = httpx_mock.get_requests()[0]
    import json as _json
    body = _json.loads(sent.content)
    assert body["embeds"] == [EMBED]


def test_post_event_appends_wait_query_param(httpx_mock: HTTPXMock):
    """The capacity ticker / retract paths need ``id`` from the response."""
    httpx_mock.add_response(
        url=WEBHOOK_WITH_WAIT, method="POST", status_code=204,
    )
    post_event(WEBHOOK, EMBED)
    sent = httpx_mock.get_requests()[0]
    parsed = urlsplit(str(sent.url))
    qs = parse_qs(parsed.query)
    assert qs.get("wait") == ["true"], (
        f"wait=true must be on the POST URL; got query={parsed.query!r}"
    )


def test_post_event_captures_message_id_on_2xx(httpx_mock: HTTPXMock):
    """Discord returns the created message JSON when ``?wait=true`` is set."""
    httpx_mock.add_response(
        url=WEBHOOK_WITH_WAIT,
        method="POST",
        status_code=200,
        json={"id": "1234567890", "channel_id": "999"},
    )
    result = post_event(WEBHOOK, EMBED)
    assert result.status == PostStatus.OK
    assert result.message_id == "1234567890"


def test_post_event_message_id_none_on_retryable(httpx_mock: HTTPXMock):
    """Retryable failures never carry a Discord message id."""
    httpx_mock.add_response(url=WEBHOOK_WITH_WAIT, method="POST", status_code=503)
    result = post_event(WEBHOOK, EMBED)
    assert result.status == PostStatus.RETRYABLE
    assert result.message_id is None


def test_post_event_url_already_has_query_string_appends_wait_correctly(
    httpx_mock: HTTPXMock,
):
    """Some webhook configs carry ``thread_id`` etc.; ``wait`` must coexist."""
    base = "https://discord.com/api/webhooks/123/abc?thread_id=42"
    final = f"{base}&wait=true"
    httpx_mock.add_response(
        url=final, method="POST", status_code=200, json={"id": "msg_thr"},
    )
    result = post_event(base, EMBED)
    assert result.status == PostStatus.OK
    assert result.message_id == "msg_thr"
    sent = httpx_mock.get_requests()[0]
    qs = parse_qs(urlsplit(str(sent.url)).query)
    assert qs.get("wait") == ["true"]
    assert qs.get("thread_id") == ["42"]


def test_patch_event_targets_messages_endpoint(httpx_mock: HTTPXMock):
    """PATCH must target ``{webhook}/messages/{message_id}`` per Discord."""
    expected = "https://discord.com/api/webhooks/123/abc/messages/msg_42"
    httpx_mock.add_response(
        url=expected, method="PATCH", status_code=200, json={"id": "msg_42"},
    )
    result = patch_event(WEBHOOK, "msg_42", EMBED)
    assert result.status == PostStatus.OK
    sent = httpx_mock.get_requests()[0]
    assert str(sent.url) == expected
    import json as _json
    body = _json.loads(sent.content)
    assert body["embeds"] == [EMBED]


def test_patch_event_preserves_existing_query_string(httpx_mock: HTTPXMock):
    """PATCH must keep any pre-existing query params (e.g. ``thread_id``)."""
    base = "https://discord.com/api/webhooks/123/abc?thread_id=42"
    expected = "https://discord.com/api/webhooks/123/abc/messages/msg_99?thread_id=42"
    httpx_mock.add_response(
        url=expected, method="PATCH", status_code=200, json={"id": "msg_99"},
    )
    result = patch_event(base, "msg_99", EMBED)
    assert result.status == PostStatus.OK
