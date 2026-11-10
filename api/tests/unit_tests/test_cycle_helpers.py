from datetime import UTC, datetime

from api.tasks.cycle import _scoring_date


def test_scoring_date_normalizes_postgres_datetime() -> None:
    value = datetime(2026, 9, 28, 18, 0, tzinfo=UTC)

    assert _scoring_date(value) == "2026-09-28T18:00:00+00:00"
    assert _scoring_date("2026-09-28T18:00:00+00:00") == "2026-09-28T18:00:00+00:00"
