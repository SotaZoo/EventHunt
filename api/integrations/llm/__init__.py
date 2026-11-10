"""LLM scoring layer.

Public surface:

- ``EventFit`` — pydantic model returned by every scoring backend.
- ``Urgency`` — ``"low" | "medium" | "high"``.
- ``score_batch(events, *, taste_profile, today)`` — async dispatch by
  ``LLM_SCORING_MODE`` (or legacy ``CLAUDE_OAUTH_MODE``):

  - ``mock``    → reads ``tests/fixtures/scoring.json`` (M0 tests, eval dry runs).
  - ``codex-cli`` → ``llm.codex_cli.score_batch`` (subprocess to ``codex exec``).
  - ``cli``     → ``llm.cli_fallback.score_batch`` (subprocess to ``claude -p``).
  - ``api``     → ``llm.agent.score_batch`` (Anthropic API key, cloud worker).
  - ``pi-auth`` → ``llm.pi_auth.score_batch`` (central OAuth token via pi-auth).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from api.configs.settings import llm_scoring_mode
from api.models.event_annotations import EventHighlight, EventUrgencyNote

Urgency = Literal["low", "medium", "high"]
CURRENT_SCORING_PROMPT = "v5"

EventType = Literal["talk", "workshop", "hackathon", "demo", "networking", "other"]
"""6-bucket taxonomy populated by the scoring prompt (v2+).

Additive; does NOT replace the legacy 9-value ``Category`` enum. Falls to
``"other"`` when the LLM is uncertain.
"""

FoodProvided = Literal["yes", "no", "unknown"]
"""Tristate at the LLM boundary so information isn't destroyed.

The render layer collapses ``"no"`` → ``"unknown"`` (only ``"yes"`` carries
push-signal); the storage and Pydantic layers must preserve all three.
"""


class NotablePerson(BaseModel):
    """One speaker / host / featured guest extracted by the scoring prompt.

    ``confidence`` is a self-reported 0..1 from the LLM. The render layer
    drops entries with confidence < the threshold defined by
    ``NOTABLE_PERSON_CONFIDENCE_THRESHOLD`` in
    ``api/integrations/discord/webhook.py``; storage keeps the full list so
    a later sweep can re-tune the threshold. The two values (renderer
    constant + prompt instruction in ``prompts/scoring/v2.md``) are kept
    in sync by hand. The v2+ prompt is also instructed to OMIT entries
    below that bar outright (the "would you bet $100" rule); storage
    still accepts low-confidence rows from older prompt versions or
    pasted fixtures.
    """

    name: str = Field(min_length=1, max_length=120)
    role_clause: str = Field(min_length=1, max_length=120)
    confidence: float = Field(ge=0.0, le=1.0)


class EventFit(BaseModel):
    """Scoring output against the supplied shared internal preference profile.

    ``notable_people``, ``event_type``, and ``food_provided`` were added with
    prompt v2 for the scout-bot push gate / renderer. They default to
    permissive empty / ``None`` values so v1 outputs (and any persisted v1
    rows) keep round-tripping through the schema unchanged.
    """

    event_id: int
    fit_score: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=200)
    urgency: Urgency
    urgency_reason: str | None = None
    notable_people: list[NotablePerson] = Field(default_factory=list)
    event_type: EventType | None = None
    food_provided: FoodProvided | None = None
    highlight: EventHighlight | None = None
    urgency_note: EventUrgencyNote | None = None


# Resolves to <repo>/api/tests/fixtures/scoring.json.
_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "api" / "tests" / "fixtures" / "scoring.json"
)


def _load_mock_fits() -> dict[int, EventFit]:
    with _FIXTURE_PATH.open() as fh:
        rows = json.load(fh)
    return {row["event_id"]: EventFit.model_validate(row) for row in rows}


def _coerce_event_ids(events: list[dict[str, Any]] | list[int]) -> list[int]:
    """Accept either a list of int ids (legacy mock-mode shorthand) or a list
    of dicts with an ``id`` field."""
    if not events:
        return []
    first = events[0]
    if isinstance(first, int):
        return list(events)  # type: ignore[arg-type]
    if isinstance(first, dict):
        try:
            return [int(e["id"]) for e in events]  # type: ignore[index]
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError("each event dict needs an integer 'id' field") from e
    raise TypeError(f"unsupported event element type: {type(first).__name__}")


async def score_batch(
    events: list[dict[str, Any]] | list[int],
    *,
    taste_profile: str,
    today: str | None = None,
    prompt_version: str = CURRENT_SCORING_PROMPT,
) -> list[EventFit]:
    """Score a batch of events against ``taste_profile``.

    ``events`` is either a list of dicts (each with an integer ``id`` plus any
    fields the LLM should see) or a bare list of ints (mock mode only).

    ``today`` defaults to UTC today (``YYYY-MM-DD``); the prompt uses it for
    urgency anchoring.
    """
    mode = llm_scoring_mode()

    if mode == "mock":
        ids = _coerce_event_ids(events)
        fits = _load_mock_fits()
        missing = [eid for eid in ids if eid not in fits]
        if missing:
            raise KeyError(
                f"mock fixture has no EventFit for event_ids={missing}; "
                f"add them to {_FIXTURE_PATH}"
            )
        return [fits[eid] for eid in ids]

    if mode == "pi-auth":
        from .pi_auth import score_batch as pi_auth_score
    elif mode == "api":
        from .agent import score_batch as agent_score
    elif mode == "codex-cli":
        from .codex_cli import score_batch as codex_score
    elif mode != "cli":
        raise NotImplementedError(f"unknown LLM_SCORING_MODE={mode!r}")

    if today is None:
        from datetime import datetime

        today = datetime.now(UTC).strftime("%Y-%m-%d")

    if not events or isinstance(events[0], int):
        raise ValueError(
            f"mode={mode!r} needs full event dicts (with title/date/host/...), "
            f"not bare event_ids"
        )
    event_dicts = events  # type: ignore[assignment]

    if mode == "pi-auth":
        return await pi_auth_score(
            event_dicts,  # type: ignore[arg-type]
            taste_profile=taste_profile,
            today=today,
            prompt_version=prompt_version,
        )

    if mode == "codex-cli":
        return await codex_score(
            event_dicts,  # type: ignore[arg-type]
            taste_profile=taste_profile,
            today=today,
            prompt_version=prompt_version,
        )

    if mode == "cli":
        from .cli_fallback import score_batch as cli_score

        return await cli_score(
            event_dicts,  # type: ignore[arg-type]
            taste_profile=taste_profile,
            today=today,
            prompt_version=prompt_version,
        )

    return await agent_score(  # api path
        event_dicts,  # type: ignore[arg-type]
        taste_profile=taste_profile,
        today=today,
        prompt_version=prompt_version,
    )


def compute_taste_sha(taste_profile: str) -> str:
    """Short hex hash of the taste profile text — stamped on each scored row."""
    return hashlib.sha256(taste_profile.encode("utf-8")).hexdigest()[:12]


__all__ = [
    "EventFit",
    "EventType",
    "FoodProvided",
    "NotablePerson",
    "Urgency",
    "compute_taste_sha",
    "score_batch",
]
