"""Malformed historical Markdown must not stall all discovery queries."""
from time import process_time

from api.models.occurrences import occurrence_groups


def test_long_unmatched_brackets_do_not_cause_quadratic_registration_scan():
    rows = [
        {"source": "meetup", "url": "https://www.meetup.com/builders/events/12345/",
         "title": "Historical", "start_at": "2026-10-01T22:00:00Z", "description": "[" * 24000},
        {"source": "luma", "url": "https://luma.com/current",
         "title": "Current", "start_at": "2026-10-07T22:00:00Z"},
    ]
    started = process_time()
    assert len(occurrence_groups(rows)) == 2
    # CPU time avoids wall-clock flakes caused by other work on the shared Mac.
    # The reproduced hand-written scan exceeded this budget at this size.
    assert process_time() - started < 2
