"""Parse ``data/taste-seed.txt`` → ``tests/eval/seed.jsonl``.

The seed file is a human-editable, record-oriented text format described in
``data/taste-seed.example.txt``. This loader normalizes it into a stable
JSONL fixture the eval harness can rely on across runs.

Records with rating ``?`` are excluded from the JSONL (they have no usable
ground-truth label). Ratings are stored as integers: -1, 0, +1.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEFAULT_SEED = REPO / "data" / "taste-seed.txt"
DEFAULT_OUT = REPO / "tests" / "eval" / "seed.jsonl"

REQUIRED_FIELDS = ("url", "title", "date", "rating", "source")
VALID_RATINGS = {"+1": 1, "0": 0, "-1": -1, "?": None}


@dataclass
class SeedRecord:
    url: str
    title: str
    date: str
    rating: int  # -1 | 0 | 1
    source: str
    host: str | None = None
    notes: str | None = None


class SeedParseError(ValueError):
    """Raised when a seed record is missing fields or has an unknown rating."""


def _split_records(text: str) -> Iterator[str]:
    """Yield non-empty record blocks separated by lines containing only '---'."""
    buf: list[str] = []
    for line in text.splitlines():
        if line.strip() == "---":
            block = "\n".join(buf).strip()
            if block:
                yield block
            buf = []
        else:
            buf.append(line)
    tail = "\n".join(buf).strip()
    if tail:
        yield tail


def _parse_block(block: str, *, lineno: int) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise SeedParseError(f"record near line {lineno}: no ':' in {line!r}")
        key, _, value = line.partition(":")
        fields[key.strip().lower()] = value.strip()
    return fields


def parse_seed(text: str) -> list[SeedRecord]:
    """Parse the full seed-file text into typed records.

    Skips ``rating: ?`` rows (they have no usable label). Raises
    ``SeedParseError`` on malformed records — silent skipping would let bad
    edits poison the eval set.
    """
    records: list[SeedRecord] = []
    lineno = 1
    for block in _split_records(text):
        fields = _parse_block(block, lineno=lineno)
        lineno += block.count("\n") + 2  # + the '---' separator

        if fields.get("rating") == "?":
            continue

        missing = [f for f in REQUIRED_FIELDS if f not in fields]
        if missing:
            raise SeedParseError(
                f"record missing required fields {missing} (url={fields.get('url')!r})"
            )

        rating_raw = fields["rating"]
        if rating_raw not in VALID_RATINGS:
            raise SeedParseError(
                f"unknown rating {rating_raw!r} for url={fields['url']!r}; "
                f"expected one of {sorted(VALID_RATINGS)}"
            )
        rating_int = VALID_RATINGS[rating_raw]
        if rating_int is None:
            continue

        records.append(
            SeedRecord(
                url=fields["url"],
                title=fields["title"],
                date=fields["date"],
                rating=rating_int,
                source=fields["source"],
                host=fields.get("host") or None,
                notes=fields.get("notes") or None,
            )
        )
    return records


def write_jsonl(records: Iterable[SeedRecord], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w") as fh:
        for rec in records:
            fh.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
            n += 1
    return n


def load(seed_path: Path = DEFAULT_SEED) -> list[SeedRecord]:
    return parse_seed(seed_path.read_text())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    records = load(args.seed)
    n = write_jsonl(records, args.out)

    pos = sum(1 for r in records if r.rating == 1)
    neg = sum(1 for r in records if r.rating == -1)
    neutral = sum(1 for r in records if r.rating == 0)
    print(f"  parsed {n} records from {args.seed.relative_to(REPO)}")
    print(f"  → {args.out.relative_to(REPO)}")
    print(f"  breakdown: +1={pos}  0={neutral}  -1={neg}")


if __name__ == "__main__":
    main()
