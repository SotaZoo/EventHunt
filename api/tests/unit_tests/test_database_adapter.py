from __future__ import annotations

from api.repositories.database import CompatResult


class PostgresSelectResult:
    rowcount = 1
    returns_rows = True

    @property
    def lastrowid(self) -> object:
        raise AssertionError("Postgres SELECT results must not read lastrowid")

    def keys(self) -> list[str]:
        return ["value"]

    def fetchall(self) -> list[tuple[int]]:
        return [(1,)]


def test_postgres_result_does_not_read_lastrowid_before_fetching_rows() -> None:
    result = CompatResult(PostgresSelectResult(), dialect="postgres")

    assert result.lastrowid is None
    assert result.fetchone()["value"] == 1
