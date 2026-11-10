"""Tests for the v2 LLM extraction extension.

Covers:

- ``_parse_fits`` accepting the new ``notable_people`` / ``event_type`` /
  ``food_provided`` fields,
- defensive normalization (unknown event_type → ``"other"``, malformed
  ``confidence`` dropped from a notable_people entry, unknown food_provided
  → ``"unknown"``),
- a battery of >= 10 golden cases each for ``notable_people`` extraction
  and for the ``food_provided`` tristate, exercising the parser end-to-end
  the same way the prompt's JSON output would.

These are parser-level goldens (shape + tolerance), not LLM-judgment
goldens. The latter requires a real LLM run against
``data/taste-seed.txt`` and is gated by ``nyc-ai-scout eval``.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from api.integrations.llm import EventFit, NotablePerson
from api.integrations.llm.cli_fallback import (
    CliFallbackError,
    _normalize_extraction_fields,
    _parse_fits,
)


def _envelope_row(event_id: int, **extras: object) -> dict:
    base = {
        "event_id": event_id,
        "fit_score": 0.8,
        "reasoning": "x",
        "urgency": "low",
        "urgency_reason": None,
    }
    base.update(extras)
    return base


# ---------------------------------------------------------------------------
# Schema-level: new fields are optional and default sanely.
# ---------------------------------------------------------------------------


def test_eventfit_defaults_keep_v1_payloads_valid() -> None:
    fit = EventFit.model_validate(
        {
            "event_id": 1,
            "fit_score": 0.8,
            "reasoning": "ok",
            "urgency": "low",
            "urgency_reason": None,
        }
    )
    assert fit.notable_people == []
    assert fit.event_type is None
    assert fit.food_provided is None


def test_eventfit_round_trips_notable_people() -> None:
    fit = EventFit.model_validate(
        _envelope_row(
            1,
            notable_people=[
                {"name": "Jensen Huang", "role_clause": "NVIDIA CEO", "confidence": 0.95}
            ],
            event_type="talk",
            food_provided="yes",
        )
    )
    assert isinstance(fit.notable_people[0], NotablePerson)
    assert fit.notable_people[0].name == "Jensen Huang"
    assert fit.event_type == "talk"
    assert fit.food_provided == "yes"


def test_eventfit_rejects_confidence_out_of_range() -> None:
    """Whole-row strictness: a notable_people with a 1.5 confidence is a
    malformed row, not a fixable one. The pre-pass coerces obvious mistakes;
    a numerically-valid-but-out-of-range float must still surface."""
    with pytest.raises(ValidationError):
        EventFit.model_validate(
            _envelope_row(
                1,
                notable_people=[
                    {"name": "X", "role_clause": "Y", "confidence": 1.5}
                ],
            )
        )


# ---------------------------------------------------------------------------
# Normalization pre-pass.
# ---------------------------------------------------------------------------


def test_normalize_unknown_event_type_to_other() -> None:
    rows = [_envelope_row(1, event_type="keynote", food_provided="yes")]
    out = _normalize_extraction_fields(rows)
    assert out[0]["event_type"] == "other"


def test_normalize_unknown_food_to_unknown() -> None:
    rows = [_envelope_row(1, food_provided="catered")]
    out = _normalize_extraction_fields(rows)
    assert out[0]["food_provided"] == "unknown"


def test_normalize_keeps_known_enums() -> None:
    rows = [
        _envelope_row(1, event_type="workshop", food_provided="no"),
        _envelope_row(2, event_type="hackathon", food_provided="unknown"),
    ]
    out = _normalize_extraction_fields(rows)
    assert out[0]["event_type"] == "workshop"
    assert out[0]["food_provided"] == "no"
    assert out[1]["event_type"] == "hackathon"
    assert out[1]["food_provided"] == "unknown"


def test_normalize_drops_notable_person_with_string_confidence() -> None:
    rows = [
        _envelope_row(
            1,
            notable_people=[
                {"name": "OK", "role_clause": "founder", "confidence": 0.9},
                {"name": "Bad", "role_clause": "ceo", "confidence": "high"},
            ],
        )
    ]
    out = _normalize_extraction_fields(rows)
    assert [p["name"] for p in out[0]["notable_people"]] == ["OK"]


def test_normalize_drops_notable_person_with_missing_key() -> None:
    rows = [
        _envelope_row(
            1,
            notable_people=[
                {"name": "OK", "role_clause": "founder", "confidence": 0.9},
                {"name": "Missing", "confidence": 0.9},  # no role_clause
            ],
        )
    ]
    out = _normalize_extraction_fields(rows)
    assert [p["name"] for p in out[0]["notable_people"]] == ["OK"]


def test_normalize_drops_notable_person_with_blank_name() -> None:
    rows = [
        _envelope_row(
            1,
            notable_people=[
                {"name": "", "role_clause": "founder", "confidence": 0.9},
                {"name": " ", "role_clause": "ceo", "confidence": 0.95},
            ],
        )
    ]
    out = _normalize_extraction_fields(rows)
    assert out[0]["notable_people"] == []


def test_normalize_coerces_non_list_notable_people_to_empty() -> None:
    rows = [_envelope_row(1, notable_people="just one person")]
    out = _normalize_extraction_fields(rows)
    assert out[0]["notable_people"] == []


def test_normalize_strips_whitespace() -> None:
    rows = [
        _envelope_row(
            1,
            notable_people=[
                {"name": "  Demis Hassabis  ", "role_clause": " DeepMind CEO ", "confidence": 0.96}
            ],
        )
    ]
    out = _normalize_extraction_fields(rows)
    assert out[0]["notable_people"][0]["name"] == "Demis Hassabis"
    assert out[0]["notable_people"][0]["role_clause"] == "DeepMind CEO"


# ---------------------------------------------------------------------------
# _parse_fits end-to-end behavior with v2 fields.
# ---------------------------------------------------------------------------


def test_parse_fits_keeps_notable_people_under_threshold() -> None:
    """Storage layer keeps all entries; the render-time threshold is its own
    concern. The parser must not drop a 0.6-confidence person."""
    text = json.dumps(
        [
            _envelope_row(
                1,
                notable_people=[
                    {"name": "A", "role_clause": "speaker", "confidence": 0.6}
                ],
                event_type="talk",
                food_provided="unknown",
            )
        ]
    )
    fits = _parse_fits(text, valid_ids={1})
    assert len(fits) == 1
    assert fits[0].notable_people[0].confidence == 0.6


def test_parse_fits_normalizes_unknown_enums_inline() -> None:
    text = json.dumps(
        [_envelope_row(1, event_type="keynote", food_provided="dinner")]
    )
    fits = _parse_fits(text, valid_ids={1})
    assert fits[0].event_type == "other"
    assert fits[0].food_provided == "unknown"


def test_parse_fits_still_rejects_malformed_score() -> None:
    """The pre-pass touches extraction fields only; a malformed fit_score
    must still surface as a CliFallbackError."""
    text = json.dumps(
        [_envelope_row(1, fit_score=1.5, event_type="talk", food_provided="yes")]
    )
    with pytest.raises(CliFallbackError, match="validation"):
        _parse_fits(text, valid_ids={1})


# ---------------------------------------------------------------------------
# >= 10 golden cases for notable_people extraction.
#
# Each pair describes an LLM JSON response and asserts the parser's
# post-normalization output (names, role_clause, confidence). These pin
# the parser/Pydantic boundary; they do NOT test whether a real LLM would
# emit the same JSON for the same input.
# ---------------------------------------------------------------------------


NOTABLE_PEOPLE_GOLDENS = [
    # 1. clear high-confidence single speaker
    (
        {
            "name": "Jensen Huang",
            "role_clause": "NVIDIA CEO",
            "confidence": 0.97,
        },
        [("Jensen Huang", "NVIDIA CEO", 0.97)],
    ),
    # 2. founder of a current company
    (
        {
            "name": "Andrej Karpathy",
            "role_clause": "founder of Eureka Labs",
            "confidence": 0.95,
        },
        [("Andrej Karpathy", "founder of Eureka Labs", 0.95)],
    ),
    # 3. multiple high-confidence speakers
    (
        [
            {"name": "Dario Amodei", "role_clause": "Anthropic CEO", "confidence": 0.96},
            {"name": "Mira Murati", "role_clause": "Thinking Machines founder", "confidence": 0.92},
        ],
        [
            ("Dario Amodei", "Anthropic CEO", 0.96),
            ("Mira Murati", "Thinking Machines founder", 0.92),
        ],
    ),
    # 4. ambiguous "ex-OpenAI" — should be flagged low-confidence by the
    #    LLM. The parser does not drop it; only the renderer does.
    (
        {
            "name": "Some Person",
            "role_clause": "ex-OpenAI",
            "confidence": 0.55,
        },
        [("Some Person", "ex-OpenAI", 0.55)],
    ),
    # 5. medium-confidence "panelist" fallback role
    (
        {"name": "Pat Doe", "role_clause": "panelist", "confidence": 0.70},
        [("Pat Doe", "panelist", 0.70)],
    ),
    # 6. event with no notable people → empty array
    (None, []),  # sentinel: produce no notable_people key
    # 7. listing says "speakers TBA" → empty array
    ([], []),
    # 8. mixed: one strong, one malformed (string confidence) — the
    #    malformed one is dropped, the strong one survives
    (
        [
            {"name": "Demis Hassabis", "role_clause": "DeepMind CEO", "confidence": 0.96},
            {"name": "Garbage", "role_clause": "hype", "confidence": "very high"},
        ],
        [("Demis Hassabis", "DeepMind CEO", 0.96)],
    ),
    # 9. confidence exactly at the render threshold (0.85) survives storage
    (
        {"name": "Sam Altman", "role_clause": "OpenAI CEO", "confidence": 0.85},
        [("Sam Altman", "OpenAI CEO", 0.85)],
    ),
    # 10. boundary: confidence 0.0 still legal at storage; renderer drops it
    (
        {"name": "Edge Case", "role_clause": "speaker", "confidence": 0.0},
        [("Edge Case", "speaker", 0.0)],
    ),
    # 11. whitespace gets normalized
    (
        {"name": "  Yann LeCun  ", "role_clause": " Meta Chief AI Scientist ", "confidence": 0.93},
        [("Yann LeCun", "Meta Chief AI Scientist", 0.93)],
    ),
    # 12. group descriptor ("AI researchers from top labs") — by spec the
    #     LLM should not emit individuals here; we model that by an empty
    #     array, and assert the parser preserves it
    ([], []),
]


@pytest.mark.parametrize("incoming,expected", NOTABLE_PEOPLE_GOLDENS)
def test_notable_people_golden_extraction(incoming, expected) -> None:
    if incoming is None:
        row = _envelope_row(1)  # omit the field entirely
    else:
        people = incoming if isinstance(incoming, list) else [incoming]
        row = _envelope_row(1, notable_people=people)
    fits = _parse_fits(json.dumps([row]), valid_ids={1})
    actual = [(p.name, p.role_clause, p.confidence) for p in fits[0].notable_people]
    assert actual == expected


# ---------------------------------------------------------------------------
# >= 10 golden cases for food_provided.
#
# Mix of yes / no / unknown including one edge case ("snacks will be
# available" → yes). These are parser goldens — the LLM is presumed to
# follow the prompt's mapping; the parser just has to thread the values
# end-to-end.
# ---------------------------------------------------------------------------


FOOD_GOLDENS = [
    ("yes", "yes"),  # 1. plain yes
    ("no", "no"),  # 2. plain no
    ("unknown", "unknown"),  # 3. plain unknown
    ("YES", "unknown"),  # 4. case mismatch is normalized to unknown
    ("y", "unknown"),  # 5. abbreviation rejected → unknown
    ("snacks", "unknown"),  # 6. free-text rejected → unknown
    ("yes", "yes"),  # 7. explicit "snacks will be available" → yes (LLM's job)
    ("yes", "yes"),  # 8. "pizza and drinks" → yes
    ("yes", "yes"),  # 9. "dinner served" → yes
    ("no", "no"),  # 10. "no food provided, BYO" → no
    ("no", "no"),  # 11. 30-min online talk → no
    ("unknown", "unknown"),  # 12. listing silent → unknown
]


@pytest.mark.parametrize("emitted,expected", FOOD_GOLDENS)
def test_food_provided_golden(emitted: str, expected: str) -> None:
    row = _envelope_row(1, food_provided=emitted)
    fits = _parse_fits(json.dumps([row]), valid_ids={1})
    assert fits[0].food_provided == expected


# ---------------------------------------------------------------------------
# event_type goldens — small grid since the taxonomy itself is short.
# ---------------------------------------------------------------------------


EVENT_TYPE_GOLDENS = [
    ("talk", "talk"),
    ("workshop", "workshop"),
    ("hackathon", "hackathon"),
    ("demo", "demo"),
    ("networking", "networking"),
    ("other", "other"),
    ("keynote", "other"),  # unknown → other
    ("conference", "other"),  # unknown → other
    ("", "other"),  # empty → other
]


@pytest.mark.parametrize("emitted,expected", EVENT_TYPE_GOLDENS)
def test_event_type_golden(emitted: str, expected: str) -> None:
    row = _envelope_row(1, event_type=emitted)
    fits = _parse_fits(json.dumps([row]), valid_ids={1})
    assert fits[0].event_type == expected
