"""Eval metrics for the M0 hard gate.

Three numbers the eval reports per prompt version:

- ``precision_at_5`` — of the top-5 highest-fit_score events, how many were
  rated +1? **Hard gate: ≥ 0.70.**
- ``recall_at_threshold`` — of all rated-positive events, how many did the
  scorer give ``fit_score ≥ threshold`` (default 0.70)? Higher is better.
- ``urgency_precision`` — of events flagged ``urgency == high``, how many were
  rated +1? Avoids burning Discord alerts on weak fits.

A ranked list of `(SeedRecord, EventFit)` pairs feeds all three metrics so the
eval CLI can print a single explanation table alongside the numbers.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from api.integrations.llm import EventFit
from api.tasks.eval_load_seed import SeedRecord


@dataclass(frozen=True)
class ScoredRecord:
    seed: SeedRecord
    fit: EventFit

    @property
    def label(self) -> int:
        return self.seed.rating

    @property
    def score(self) -> float:
        return self.fit.fit_score


@dataclass(frozen=True)
class EvalReport:
    precision_at_5: float
    recall_at_threshold: float
    urgency_precision: float | None  # None if no urgency=high in batch
    threshold: float
    n_scored: int
    n_positive: int
    n_negative: int
    top_5: list[ScoredRecord]
    passed_gate: bool  # precision@5 ≥ 0.70

    def to_dict(self) -> dict:
        return {
            "precision_at_5": self.precision_at_5,
            "recall_at_threshold": self.recall_at_threshold,
            "urgency_precision": self.urgency_precision,
            "threshold": self.threshold,
            "n_scored": self.n_scored,
            "n_positive": self.n_positive,
            "n_negative": self.n_negative,
            "top_5": [
                {
                    "url": r.seed.url,
                    "title": r.seed.title,
                    "rating": r.seed.rating,
                    "fit_score": r.fit.fit_score,
                    "urgency": r.fit.urgency,
                    "reasoning": r.fit.reasoning,
                }
                for r in self.top_5
            ],
            "passed_gate": self.passed_gate,
        }


def precision_at_k(scored: list[ScoredRecord], k: int = 5) -> float:
    """Fraction of the top-``k`` highest-scored events that have rating == +1.

    Empty input → 0.0. ``k`` larger than ``len(scored)`` is clamped, but the
    denominator stays ``min(k, len(scored))`` so a 3-event eval reports
    precision@3 disguised as precision@k. The eval gate calls k=5 only when
    there are enough scored records to make it meaningful.
    """
    if not scored:
        return 0.0
    top = sorted(scored, key=lambda r: r.score, reverse=True)[:k]
    hits = sum(1 for r in top if r.label == 1)
    return hits / len(top)


def recall_at_threshold(scored: list[ScoredRecord], threshold: float = 0.70) -> float:
    """Of rating-+1 events, fraction whose fit_score ≥ ``threshold``.

    Returns 0.0 if there are no positives in the batch — defensive default,
    callers should warn separately when ``n_positive == 0``.
    """
    positives = [r for r in scored if r.label == 1]
    if not positives:
        return 0.0
    matched = sum(1 for r in positives if r.score >= threshold)
    return matched / len(positives)


def urgency_precision(scored: list[ScoredRecord]) -> float | None:
    """Of events flagged ``urgency == high``, fraction rated +1.

    Neutrals (rating == 0) are excluded from the denominator — they're
    "no signal," not "false positive," and counting them would penalize a
    well-calibrated scorer for events the user simply hasn't rated.

    Returns ``None`` if no rated high-urgency flags exist (the metric
    isn't meaningful and a 0.0 would falsely look like a regression).
    """
    high_rated = [r for r in scored if r.fit.urgency == "high" and r.label != 0]
    if not high_rated:
        return None
    hits = sum(1 for r in high_rated if r.label == 1)
    return hits / len(high_rated)


def evaluate(
    scored: Iterable[ScoredRecord],
    *,
    k: int = 5,
    threshold: float = 0.70,
    gate: float = 0.70,
) -> EvalReport:
    """Compute all three metrics and the M0 pass/fail gate.

    Gate is forced to ``False`` when fewer than ``k`` records were scored —
    "precision@5" computed over 3 records is precision@3 in disguise, and
    silently passing it would hide scoring failures (e.g. all ids
    hallucinated by the LLM).
    """
    items = list(scored)
    p_at_k = precision_at_k(items, k=k)
    rec = recall_at_threshold(items, threshold=threshold)
    urg = urgency_precision(items)
    top = sorted(items, key=lambda r: r.score, reverse=True)[:k]
    n_pos = sum(1 for r in items if r.label == 1)
    n_neg = sum(1 for r in items if r.label == -1)
    enough_to_gate = len(items) >= k
    return EvalReport(
        precision_at_5=p_at_k,
        recall_at_threshold=rec,
        urgency_precision=urg,
        threshold=threshold,
        n_scored=len(items),
        n_positive=n_pos,
        n_negative=n_neg,
        top_5=top,
        passed_gate=enough_to_gate and p_at_k >= gate,
    )


__all__ = [
    "EvalReport",
    "ScoredRecord",
    "evaluate",
    "precision_at_k",
    "recall_at_threshold",
    "urgency_precision",
]
