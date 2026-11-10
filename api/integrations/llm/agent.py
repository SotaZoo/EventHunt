"""Cloud-compatible Anthropic API scoring adapter."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from . import CURRENT_SCORING_PROMPT, EventFit
from .cli_fallback import (
    DEFAULT_TIMEOUT_SEC,
    CliFallbackError,
    _parse_fits,
    prompt_path_for_version,
    render_prompt,
)

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY_SEC = 1.0
DEFAULT_RETRY_MAX_DELAY_SEC = 30.0
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


class AgentScoringError(CliFallbackError):
    pass


def api_key_auth_headers(api_key: str | None = None) -> dict[str, str]:
    key = (api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")).strip()
    if not key:
        raise AgentScoringError("ANTHROPIC_API_KEY is required for LLM_SCORING_MODE=api")
    return {"x-api-key": key}


def bearer_auth_headers(token: str) -> dict[str, str]:
    value = token.strip()
    if not value:
        raise AgentScoringError("empty Anthropic bearer token")
    return {"authorization": f"Bearer {value}"}


def _extract_text(payload: dict[str, Any]) -> str:
    parts = payload.get("content")
    if not isinstance(parts, list):
        raise AgentScoringError("Anthropic response missing content array")
    text_parts = [
        item.get("text")
        for item in parts
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    ]
    if not text_parts:
        raise AgentScoringError("Anthropic response had no text content")
    return "\n".join(text_parts)


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _retry_delay(
    response: httpx.Response,
    *,
    attempt: int,
    base_delay: float,
    max_delay: float,
) -> float:
    retry_after = _retry_after_seconds(response.headers.get("retry-after"))
    fallback = min(max_delay, max(0.0, base_delay) * (2 ** max(0, attempt - 1)))
    if retry_after is None:
        return fallback
    return min(max_delay, retry_after)


async def score_batch(
    events: list[dict[str, Any]],
    *,
    taste_profile: str,
    today: str,
    prompt_version: str = CURRENT_SCORING_PROMPT,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    model: str | None = None,
    auth_headers: dict[str, str] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY_SEC,
    retry_max_delay: float = DEFAULT_RETRY_MAX_DELAY_SEC,
) -> list[EventFit]:
    if not events:
        return []
    valid_ids = {e["id"] for e in events}
    if len(valid_ids) != len(events) or any(not isinstance(i, int) for i in valid_ids):
        raise AgentScoringError("every event needs a unique integer 'id' field")

    prompt = render_prompt(
        events,
        taste_profile=taste_profile,
        today=today,
        template_path=prompt_path_for_version(prompt_version),
    )
    selected_model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
    headers = {
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    headers.update(auth_headers or api_key_auth_headers())
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, max(1, max_attempts) + 1):
            try:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json={
                        "model": selected_model,
                        "max_tokens": 2000,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
            except httpx.TransportError as exc:
                if attempt >= max(1, max_attempts):
                    raise AgentScoringError(f"Anthropic API transport error: {exc}") from exc
                await asyncio.sleep(
                    min(retry_max_delay, max(0.0, retry_base_delay) * (2 ** (attempt - 1)))
                )
                continue

            if response.status_code < 400:
                break
            detail = response.text[:400]
            if (
                response.status_code not in RETRYABLE_STATUS_CODES
                or attempt >= max(1, max_attempts)
            ):
                raise AgentScoringError(f"Anthropic API error {response.status_code}: {detail}")
            await asyncio.sleep(
                _retry_delay(
                    response,
                    attempt=attempt,
                    base_delay=retry_base_delay,
                    max_delay=retry_max_delay,
                )
            )
    return _parse_fits(_extract_text(response.json()), valid_ids=valid_ids)


__all__ = ["AgentScoringError", "api_key_auth_headers", "bearer_auth_headers", "score_batch"]
