"""Observable model-output contract for source-grounded card annotation models."""
from datetime import timedelta

import pytest
from pydantic import ValidationError

from api.integrations.llm import EventFit


def test_scored_event_keeps_optional_highlight_and_deadline_evidence():
    fit = EventFit.model_validate({
        "event_id": 1, "fit_score": 0.91, "reasoning": "Shared fit explanation",
        "urgency": "low", "urgency_reason": None,
        "highlight": {
            "text": "Panel with Dick Hardt, OAuth 2.0 co-author",
            "evidence": "Dick Hardt, co-author of OAuth 2.0",
        },
        "urgency_note": {
            "kind": "deadline", "text": "RSVP by Friday at 6 PM",
            "evidence": "RSVP closes October 9, 2026 at 6 PM EDT",
            "deadline_at": "2026-10-09T18:00:00-04:00",
            "deadline_evidence": "October 9, 2026 at 6 PM EDT",
        },
    })
    assert fit.highlight.text == "Panel with Dick Hardt, OAuth 2.0 co-author"
    assert fit.highlight.evidence == "Dick Hardt, co-author of OAuth 2.0"
    assert fit.urgency_note.kind == "deadline"
    assert fit.urgency_note.deadline_at.utcoffset() == timedelta(hours=-4)
    legacy = EventFit(event_id=2, fit_score=.8, reasoning="Honest explanation", urgency="low")
    assert legacy.highlight is None
    assert legacy.urgency_note is None


@pytest.mark.parametrize("deadline", [None, "2026-10-09T18:00:00"])
def test_deadline_annotation_requires_an_unambiguous_timezone_aware_deadline(deadline):
    with pytest.raises(ValidationError):
        EventFit.model_validate({
            "event_id": 1, "fit_score": .91, "reasoning": "Shared fit", "urgency": "low",
            "urgency_note": {"kind": "deadline", "text": "RSVP by Friday",
                             "evidence": "RSVP closes Friday", "deadline_at": deadline},
        })


@pytest.mark.parametrize("expression", [
    "tomorrow at 6 PM EDT", "Friday at 6 PM EDT", "October 9 at 6 PM EDT",
    "October 9, 2026", "October 9, 2026 at 6 PM", "10/09/2026 at 6 PM EDT",
    "October 9, 26 at 6 PM EDT", "October 9, 2026 at 6 PM XYZ",
    "Friday, October 10, 2026 at 6 PM EDT", "October 9, 2026 at 6 PM EDT -0500",
    "October 9, 2026 at 6 PM UTC+03:00",
    "2026-10-09T18:00:00",
])
def test_source_time_must_supply_all_unambiguous_calendar_and_clock_context(expression):
    with pytest.raises(ValidationError):
        EventFit.model_validate({
            "event_id": 1, "fit_score": .91, "reasoning": "Shared fit", "urgency": "low",
            "urgency_note": {"kind": "deadline", "text": "Register by October 9",
                             "evidence": expression, "deadline_evidence": expression,
                             "deadline_at": "2026-10-09T18:00:00-04:00"},
        })


@pytest.mark.parametrize("expression,expected_instant,zone", [
    ("October 9, 2026 at 6 PM EDT", "2026-10-09T18:00:00-04:00", "EDT"),
    ("2026-10-09T18:00:00-04:00", "2026-10-09T18:00:00-04:00", "UTC-04:00"),
    ("October 9, 2026 at 6 PM UTC", "2026-10-09T18:00:00+00:00", "UTC"),
])
def test_application_normalizes_source_time_instead_of_trusting_model_instant(expression, expected_instant, zone):
    fit = EventFit.model_validate({
        "event_id": 1, "fit_score": .91, "reasoning": "Shared fit", "urgency": "low",
        "urgency_note": {"kind": "deadline", "text": "Register by an invented date",
                         "evidence": expression, "deadline_evidence": expression,
                         "deadline_at": "2030-01-01T12:00:00Z"},
    })
    assert fit.urgency_note.deadline_at.isoformat() == expected_instant
    assert fit.urgency_note.text.startswith("Register by Oct 9, 2026 at 6 PM ")
    assert fit.urgency_note.text.endswith(zone)
