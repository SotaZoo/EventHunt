"""Tests for the dispatch in ``api.integrations.llm`` (M0.6).

Covers all three branches of ``score_batch``:
- mock     → returns canned EventFit from fixture.
- codex-cli → delegates to ``llm.codex_cli.score_batch``.
- cli      → delegates to ``llm.cli_fallback.score_batch``.
- pi-auth  → delegates to ``llm.pi_auth.score_batch``.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from api.integrations.llm import EventFit, score_batch
from api.integrations.llm.pi_auth import AnthropicCredential


async def test_mock_dispatch_with_dict_events(mock_modes: None) -> None:
    fits = await score_batch(
        [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
        taste_profile="ignored",
    )
    assert [f.event_id for f in fits] == [1, 2]


async def test_mock_dispatch_with_bare_ids(mock_modes: None) -> None:
    fits = await score_batch([1, 2, 3], taste_profile="ignored")
    assert [f.event_id for f in fits] == [1, 2, 3]


async def test_cli_dispatch_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_OAUTH_MODE", "cli")
    captured: dict = {}

    async def fake_cli_score(events, *, taste_profile, today, **kw):
        captured["events"] = events
        captured["taste_profile"] = taste_profile
        captured["today"] = today
        return [
            EventFit(
                event_id=events[0]["id"], fit_score=0.42, reasoning="stub",
                urgency="low", urgency_reason=None,
            )
        ]

    monkeypatch.setattr("api.integrations.llm.cli_fallback.score_batch", fake_cli_score)

    fits = await score_batch(
        [{"id": 7, "title": "X"}],
        taste_profile="loves agents",
        today="2026-04-25",
    )
    assert fits[0].event_id == 7
    assert captured["taste_profile"] == "loves agents"
    assert captured["today"] == "2026-04-25"


async def test_cli_dispatch_rejects_bare_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_OAUTH_MODE", "cli")
    with pytest.raises(ValueError, match="event dicts"):
        await score_batch([1, 2], taste_profile="x")


async def test_codex_cli_dispatch_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_SCORING_MODE", "codex-cli")
    captured: dict = {}

    async def fake_codex_score(events, *, taste_profile, today, **kw):
        captured["events"] = events
        captured["taste_profile"] = taste_profile
        captured["today"] = today
        return [
            EventFit(
                event_id=events[0]["id"], fit_score=0.66, reasoning="stub",
                urgency="low", urgency_reason=None,
            )
        ]

    monkeypatch.setattr("api.integrations.llm.codex_cli.score_batch", fake_codex_score)

    fits = await score_batch(
        [{"id": 8, "title": "Y"}],
        taste_profile="loves agents",
        today="2026-04-25",
    )
    assert fits[0].event_id == 8
    assert captured["taste_profile"] == "loves agents"
    assert captured["today"] == "2026-04-25"


async def test_pi_auth_dispatch_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_SCORING_MODE", "pi-auth")
    captured: dict = {}

    async def fake_pi_auth_score(events, *, taste_profile, today, **kw):
        captured["events"] = events
        captured["taste_profile"] = taste_profile
        captured["today"] = today
        return [
            EventFit(
                event_id=events[0]["id"], fit_score=0.77, reasoning="stub",
                urgency="low", urgency_reason=None,
            )
        ]

    monkeypatch.setattr("api.integrations.llm.pi_auth.score_batch", fake_pi_auth_score)

    fits = await score_batch(
        [{"id": 10, "title": "Z"}],
        taste_profile="loves agents",
        today="2026-04-25",
    )
    assert fits[0].event_id == 10
    assert captured["taste_profile"] == "loves agents"
    assert captured["today"] == "2026-04-25"


async def test_api_dispatch_posts_to_anthropic(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
) -> None:
    monkeypatch.setenv("CLAUDE_OAUTH_MODE", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
    httpx_mock.add_response(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        json={
            "content": [
                {
                    "type": "text",
                    "text": '[{"event_id": 7, "fit_score": 0.82, "reasoning": "Relevant", "urgency": "low", "urgency_reason": null}]',
                }
            ]
        },
    )

    fits = await score_batch(
        [{"id": 7, "title": "X"}],
        taste_profile="loves agents",
        today="2026-04-25",
    )

    request = httpx_mock.get_request()
    assert request is not None
    assert request.headers["x-api-key"] == "test-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.read()
    assert fits == [
        EventFit(
            event_id=7,
            fit_score=0.82,
            reasoning="Relevant",
            urgency="low",
            urgency_reason=None,
        )
    ]


async def test_pi_auth_dispatch_posts_with_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
) -> None:
    monkeypatch.setenv("LLM_SCORING_MODE", "pi-auth")
    monkeypatch.setattr(
        "api.integrations.llm.pi_auth.get_anthropic_credential",
        lambda: AnthropicCredential(kind="oauth", value="oauth-token"),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        json={
            "content": [
                {
                    "type": "text",
                    "text": '[{"event_id": 11, "fit_score": 0.74, "reasoning": "Relevant", "urgency": "low", "urgency_reason": null}]',
                }
            ]
        },
    )

    fits = await score_batch(
        [{"id": 11, "title": "OAuth event"}],
        taste_profile="loves agents",
        today="2026-04-25",
    )

    request = httpx_mock.get_request()
    assert request is not None
    assert request.headers["authorization"] == "Bearer oauth-token"
    assert "x-api-key" not in request.headers
    assert fits[0].event_id == 11


async def test_pi_auth_dispatch_retries_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
) -> None:
    monkeypatch.setenv("LLM_SCORING_MODE", "pi-auth")
    monkeypatch.setattr(
        "api.integrations.llm.pi_auth.get_anthropic_credential",
        lambda: AnthropicCredential(kind="oauth", value="oauth-token"),
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        status_code=429,
        headers={"retry-after": "0"},
        json={"type": "error", "error": {"type": "rate_limit_error", "message": "Error"}},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        json={
            "content": [
                {
                    "type": "text",
                    "text": '[{"event_id": 12, "fit_score": 0.79, "reasoning": "Relevant", "urgency": "low", "urgency_reason": null}]',
                }
            ]
        },
    )

    fits = await score_batch(
        [{"id": 12, "title": "OAuth event"}],
        taste_profile="loves agents",
        today="2026-04-25",
    )

    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    assert all(request.headers["authorization"] == "Bearer oauth-token" for request in requests)
    assert fits[0].event_id == 12


async def test_api_dispatch_round_trips_v2_extraction_fields(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
) -> None:
    """The score_batch boundary must surface notable_people / event_type /
    food_provided so the cycle pipeline can persist them. The previous
    test still pins backward compatibility for v1-shaped payloads."""
    monkeypatch.setenv("CLAUDE_OAUTH_MODE", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    httpx_mock.add_response(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        json={
            "content": [
                {
                    "type": "text",
                    "text": (
                        '[{"event_id": 9, "fit_score": 0.91, "reasoning": "speaker is a perfect match",'
                        ' "urgency": "high", "urgency_reason": "in 2 days",'
                        ' "notable_people": [{"name": "Jensen Huang",'
                        ' "role_clause": "NVIDIA CEO", "confidence": 0.97}],'
                        ' "event_type": "talk", "food_provided": "yes"}]'
                    ),
                }
            ]
        },
    )

    fits = await score_batch(
        [{"id": 9, "title": "AI talk"}],
        taste_profile="loves agents",
        today="2026-04-25",
    )

    assert len(fits) == 1
    fit = fits[0]
    assert fit.event_type == "talk"
    assert fit.food_provided == "yes"
    assert len(fit.notable_people) == 1
    assert fit.notable_people[0].name == "Jensen Huang"
    assert fit.notable_people[0].confidence == 0.97


async def test_api_dispatch_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_OAUTH_MODE", "api")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        await score_batch([{"id": 7, "title": "X"}], taste_profile="x", today="2026-04-25")


async def test_pi_auth_dispatch_rejects_bare_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_OAUTH_MODE", "pi-auth")
    with pytest.raises(ValueError, match="event dicts"):
        await score_batch([1], taste_profile="x")
