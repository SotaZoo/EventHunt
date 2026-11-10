"""Tests for ``api.tasks.eval_load_seed``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.tasks.eval_load_seed import (
    SeedParseError,
    parse_seed,
    write_jsonl,
)


def test_parses_minimal_record() -> None:
    text = """\
url: https://lu.ma/abc
title: Test Event
date: 2026-04-25
rating: +1
source: calendar+luma
---
"""
    records = parse_seed(text)
    assert len(records) == 1
    rec = records[0]
    assert rec.url == "https://lu.ma/abc"
    assert rec.title == "Test Event"
    assert rec.date == "2026-04-25"
    assert rec.rating == 1
    assert rec.source == "calendar+luma"
    assert rec.host is None
    assert rec.notes is None


def test_negative_and_neutral_ratings() -> None:
    text = """\
url: https://lu.ma/a
title: A
date: 2026-01-01
rating: -1
source: calendar-followed(foo)
---
url: https://lu.ma/b
title: B
date: 2026-01-02
rating: 0
source: calendar-only
---
"""
    records = parse_seed(text)
    ratings = [r.rating for r in records]
    assert ratings == [-1, 0]


def test_question_mark_rating_skipped() -> None:
    text = """\
url: https://lu.ma/a
title: A
date: 2026-01-01
rating: +1
source: calendar+luma
---
url: https://lu.ma/b
title: B
date: 2026-01-02
rating: ?
source: luma-only
---
url: https://lu.ma/c
title: C
date: 2026-01-03
rating: -1
source: calendar-followed(foo)
---
"""
    records = parse_seed(text)
    assert [r.url for r in records] == ["https://lu.ma/a", "https://lu.ma/c"]


def test_optional_fields_parsed() -> None:
    text = """\
url: https://lu.ma/x
title: X
date: 2026-01-01
rating: +1
source: calendar+luma
host: Some Host
notes: a free-text note
---
"""
    records = parse_seed(text)
    assert records[0].host == "Some Host"
    assert records[0].notes == "a free-text note"


def test_comments_and_blank_lines_ignored() -> None:
    text = """\
# this is a comment header
# more comments

url: https://lu.ma/a
title: A
date: 2026-01-01
rating: +1
source: calendar+luma
---
"""
    records = parse_seed(text)
    assert len(records) == 1


def test_missing_required_field_raises() -> None:
    text = """\
url: https://lu.ma/a
title: A
date: 2026-01-01
rating: +1
---
"""
    with pytest.raises(SeedParseError, match="missing required fields"):
        parse_seed(text)


def test_unknown_rating_raises() -> None:
    text = """\
url: https://lu.ma/a
title: A
date: 2026-01-01
rating: 99
source: calendar+luma
---
"""
    with pytest.raises(SeedParseError, match="unknown rating"):
        parse_seed(text)


def test_empty_input() -> None:
    assert parse_seed("") == []
    assert parse_seed("\n---\n---\n") == []


def test_write_jsonl_roundtrip(tmp_path: Path) -> None:
    text = """\
url: https://lu.ma/a
title: A
date: 2026-01-01
rating: +1
source: calendar+luma
---
url: https://lu.ma/b
title: B
date: 2026-01-02
rating: -1
source: calendar-followed(foo)
host: NYC Founders Club
---
"""
    records = parse_seed(text)
    out = tmp_path / "seed.jsonl"
    n = write_jsonl(records, out)
    assert n == 2

    lines = out.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first == {
        "url": "https://lu.ma/a",
        "title": "A",
        "date": "2026-01-01",
        "rating": 1,
        "source": "calendar+luma",
        "host": None,
        "notes": None,
    }
    second = json.loads(lines[1])
    assert second["rating"] == -1
    assert second["host"] == "NYC Founders Club"


def test_real_seed_file_loads() -> None:
    """Smoke test against the actual data/taste-seed.txt the project ships with.

    Confirms the parser stays compatible with whatever the seed-builder emits,
    and that the file has enough labels for the M0 precision@5 gate.
    """
    repo = Path(__file__).resolve().parents[1]
    seed = repo / "data" / "taste-seed.txt"
    if not seed.exists():
        pytest.skip("data/taste-seed.txt not generated yet")
    records = parse_seed(seed.read_text())
    pos = sum(1 for r in records if r.rating == 1)
    neg = sum(1 for r in records if r.rating == -1)
    assert pos >= 5, f"need ≥5 positives for precision@5; got {pos}"
    assert neg >= 5, f"need ≥5 negatives for a meaningful gate; got {neg}"
