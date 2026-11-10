import pytest
from pydantic import ValidationError

from api.integrations.llm import EventFit, NotablePerson
from api.models import Event


def test_event_model_is_importable_from_api_models():
    assert Event.__name__ == "Event"


# ---------------------------------------------------------------------------
# EventFit — v2 extraction fields are additive and default to no-op values.
# Pinning this in a Pydantic-only test catches regressions in defaults
# (e.g. a default_factory removal) without dragging in the LLM dispatcher.
# ---------------------------------------------------------------------------


def test_eventfit_v2_fields_have_safe_defaults():
    fit = EventFit(
        event_id=1,
        fit_score=0.8,
        reasoning="ok",
        urgency="low",
        urgency_reason=None,
    )
    assert fit.notable_people == []
    assert fit.event_type is None
    assert fit.food_provided is None


def test_eventfit_accepts_v2_payload():
    fit = EventFit.model_validate(
        {
            "event_id": 1,
            "fit_score": 0.9,
            "reasoning": "great",
            "urgency": "high",
            "urgency_reason": "2 days out",
            "notable_people": [
                {"name": "Yann LeCun", "role_clause": "Meta AI Chief", "confidence": 0.95}
            ],
            "event_type": "talk",
            "food_provided": "yes",
        }
    )
    assert fit.notable_people[0] == NotablePerson(
        name="Yann LeCun", role_clause="Meta AI Chief", confidence=0.95
    )
    assert fit.event_type == "talk"
    assert fit.food_provided == "yes"


def test_eventfit_rejects_unknown_event_type():
    """Pydantic stays strict; the resilient coercion is handled in the
    cli_fallback normalizer BEFORE validation. A bare model_validate
    should still reject unknown enums."""
    with pytest.raises(ValidationError):
        EventFit.model_validate(
            {
                "event_id": 1,
                "fit_score": 0.8,
                "reasoning": "x",
                "urgency": "low",
                "urgency_reason": None,
                "event_type": "keynote",
            }
        )


def test_eventfit_rejects_unknown_food_provided():
    with pytest.raises(ValidationError):
        EventFit.model_validate(
            {
                "event_id": 1,
                "fit_score": 0.8,
                "reasoning": "x",
                "urgency": "low",
                "urgency_reason": None,
                "food_provided": "maybe",
            }
        )


def test_notable_person_requires_non_blank_name():
    with pytest.raises(ValidationError):
        NotablePerson(name="", role_clause="x", confidence=0.9)


def test_notable_person_clamps_confidence():
    with pytest.raises(ValidationError):
        NotablePerson(name="X", role_clause="Y", confidence=1.5)
    with pytest.raises(ValidationError):
        NotablePerson(name="X", role_clause="Y", confidence=-0.1)
