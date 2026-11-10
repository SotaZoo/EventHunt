import json

import pytest

from api.integrations.scrapers._util import get_attendance_mode
from api.integrations.scrapers.luma import _jsonld_to_raw


def test_captured_luma_source_retains_attendance(fixtures_dir):
    fixture = json.loads((fixtures_dir / "luma_attendance.json").read_text())
    event = _jsonld_to_raw(fixture["source_url"], fixture["event"])
    assert event.attendance_mode == "in-person"
    assert event.venue == "Fordham University School of Law"


@pytest.mark.parametrize(("data", "expected"), [
    ({}, None),
    ({"location": {"name": "Online"}}, None),
    ({"event_type": "workshop"}, None),
    ({"location": "Online"}, None),
    ({"eventAttendanceMode": "https://other.test/OnlineEventAttendanceMode"}, None),
    ({"location": {"@type": "VirtualLocation"}}, "online"),
    ({"location": {"@type": "Place"}}, "in-person"),
    ({"location": [{"@type": "Place"}, {"@type": "VirtualLocation"}]}, "mixed"),
])
def test_only_explicit_attendance_evidence_is_normalized(data, expected):
    assert get_attendance_mode(data) == expected
