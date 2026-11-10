"""Tests for ``api.tasks.eval_precision`` — the M0 hard-gate metrics."""

from __future__ import annotations

from api.integrations.llm import EventFit
from api.tasks.eval_load_seed import SeedRecord
from api.tasks.eval_precision import (
    EvalReport,
    ScoredRecord,
    evaluate,
    precision_at_k,
    recall_at_threshold,
    urgency_precision,
)


def _seed(rating: int, *, url: str = "u", title: str = "t") -> SeedRecord:
    return SeedRecord(
        url=url,
        title=title,
        date="2026-04-25",
        rating=rating,
        source="test",
        host=None,
        notes=None,
    )


def _fit(score: float, *, urgency: str = "low", event_id: int = 1) -> EventFit:
    return EventFit(
        event_id=event_id,
        fit_score=score,
        reasoning="r",
        urgency=urgency,  # type: ignore[arg-type]
        urgency_reason=None,
    )


def _row(rating: int, score: float, *, urgency: str = "low") -> ScoredRecord:
    return ScoredRecord(seed=_seed(rating), fit=_fit(score, urgency=urgency))


def test_precision_at_5_perfect() -> None:
    items = [_row(1, 0.9), _row(1, 0.85), _row(1, 0.8), _row(1, 0.75), _row(1, 0.7),
             _row(-1, 0.1)]
    assert precision_at_k(items, k=5) == 1.0


def test_precision_at_5_mixed() -> None:
    # Top 5 by score: ratings = [+1, -1, +1, -1, +1] → 3/5 = 0.6
    items = [
        _row(1, 0.9),
        _row(-1, 0.85),
        _row(1, 0.8),
        _row(-1, 0.75),
        _row(1, 0.7),
        _row(-1, 0.1),
    ]
    assert precision_at_k(items, k=5) == 0.6


def test_precision_at_5_no_positives() -> None:
    items = [_row(-1, 0.9), _row(-1, 0.5), _row(-1, 0.1)]
    assert precision_at_k(items, k=5) == 0.0


def test_precision_at_5_fewer_than_k_records() -> None:
    items = [_row(1, 0.9), _row(-1, 0.5)]  # only 2 records
    # 1 hit / 2 examined → 0.5
    assert precision_at_k(items, k=5) == 0.5


def test_precision_at_5_empty() -> None:
    assert precision_at_k([], k=5) == 0.0


def test_recall_at_threshold() -> None:
    items = [
        _row(1, 0.9),  # match
        _row(1, 0.71),  # match
        _row(1, 0.6),  # miss (below threshold)
        _row(-1, 0.95),  # not a positive
    ]
    assert recall_at_threshold(items, threshold=0.7) == 2 / 3


def test_recall_no_positives() -> None:
    items = [_row(-1, 0.9), _row(-1, 0.1)]
    assert recall_at_threshold(items, threshold=0.7) == 0.0


def test_urgency_precision_all_hits() -> None:
    items = [_row(1, 0.9, urgency="high"), _row(1, 0.8, urgency="high")]
    assert urgency_precision(items) == 1.0


def test_urgency_precision_mixed() -> None:
    items = [
        _row(1, 0.9, urgency="high"),
        _row(-1, 0.85, urgency="high"),
        _row(1, 0.8, urgency="low"),  # not high → ignored
    ]
    assert urgency_precision(items) == 0.5


def test_urgency_precision_none_when_no_high() -> None:
    items = [_row(1, 0.9, urgency="low"), _row(1, 0.8, urgency="medium")]
    assert urgency_precision(items) is None


def test_evaluate_passes_gate() -> None:
    items = [_row(1, 0.9), _row(1, 0.85), _row(1, 0.8), _row(1, 0.75), _row(1, 0.7),
             _row(-1, 0.1)]
    rep = evaluate(items, k=5, threshold=0.7, gate=0.7)
    assert isinstance(rep, EvalReport)
    assert rep.precision_at_5 == 1.0
    assert rep.passed_gate is True
    assert rep.n_positive == 5
    assert rep.n_negative == 1


def test_evaluate_fails_gate() -> None:
    items = [
        _row(1, 0.9),
        _row(-1, 0.85),
        _row(-1, 0.8),
        _row(-1, 0.75),
        _row(-1, 0.7),
    ]
    rep = evaluate(items, k=5)
    assert rep.precision_at_5 == 0.2
    assert rep.passed_gate is False


def test_evaluate_to_dict_serializable() -> None:
    import json

    items = [_row(1, 0.9), _row(-1, 0.8)]
    rep = evaluate(items)
    payload = rep.to_dict()
    # round-trip — must be JSON-clean for results-{version}.json output.
    json.dumps(payload)
    assert payload["precision_at_5"] in (0.0, 0.5, 1.0)
    assert "top_5" in payload


def test_evaluate_below_k_fails_gate_even_at_perfect_precision() -> None:
    """precision@5 with only 3 items is precision@3 in disguise — passing the
    M0 gate on insufficient data would silently mask scoring failures."""
    items = [_row(1, 0.9), _row(1, 0.85), _row(1, 0.8)]
    rep = evaluate(items, k=5)
    assert rep.precision_at_5 == 1.0
    assert rep.passed_gate is False


def test_urgency_precision_excludes_neutrals_from_denominator() -> None:
    """A neutral (rating == 0) flagged urgency=high is 'no signal', not a
    miss — counting it would penalize a calibrated scorer for unrated events."""
    items = [
        _row(1, 0.9, urgency="high"),
        _row(0, 0.85, urgency="high"),  # excluded from both numerator and denom
        _row(-1, 0.7, urgency="high"),
    ]
    # 1 hit / 2 rated = 0.5; without the filter it would be 1/3 ≈ 0.33
    assert urgency_precision(items) == 0.5
