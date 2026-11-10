from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.sql import text

from api.configs.settings import DatabaseSettings, database_settings


@dataclass(frozen=True)
class Database:
    settings: DatabaseSettings
    engine: Engine

    @property
    def dialect(self) -> str:
        return self.settings.dialect

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with self.engine.connect() as conn:
            yield conn

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    @contextmanager
    def compat_connect(self) -> Iterator[CompatConnection]:
        with self.engine.begin() as conn:
            yield CompatConnection(conn, dialect=self.dialect)

    def open_compat(self) -> CompatConnection:
        transaction = self.engine.begin()
        conn = transaction.__enter__()

        def _close() -> None:
            transaction.__exit__(None, None, None)

        return CompatConnection(conn, dialect=self.dialect, close_callback=_close)


class CompatRow:
    def __init__(self, keys: Sequence[str], values: Sequence[object]) -> None:
        self._keys = list(keys)
        self._values = list(values)
        self._mapping = dict(zip(self._keys, self._values, strict=False))

    def __getitem__(self, key: str | int) -> object:
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> list[str]:
        return list(self._keys)

    def get(self, key: str, default: object = None) -> object:
        return self._mapping.get(key, default)

    def as_dict(self) -> dict[str, object]:
        return dict(self._mapping)


class CompatResult:
    def __init__(self, result, *, dialect: str) -> None:  # type: ignore[no-untyped-def]
        self.rowcount = result.rowcount
        self.lastrowid = result.lastrowid if dialect == "sqlite" else None
        self._rows: list[CompatRow] = []
        if result.returns_rows:
            keys = list(result.keys())
            self._rows = [CompatRow(keys, tuple(row)) for row in result.fetchall()]

    def fetchone(self) -> CompatRow | None:
        if not self._rows:
            return None
        return self._rows.pop(0)

    def fetchall(self) -> list[CompatRow]:
        rows = self._rows
        self._rows = []
        return rows


class CompatConnection:
    def __init__(
        self,
        conn: Connection,
        *,
        dialect: str,
        close_callback=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self._conn = conn
        self.dialect = dialect
        self._close_callback = close_callback

    def execute(
        self,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
    ) -> CompatResult:
        statement, bound = _to_sqlalchemy_text(sql, params)
        return CompatResult(
            self._conn.execute(text(statement), bound),
            dialect=self.dialect,
        )

    def close(self) -> None:
        if self._close_callback is not None:
            self._close_callback()
            self._close_callback = None
            return
        self._conn.close()


def _to_sqlalchemy_text(
    sql: str,
    params: Sequence[object] | Mapping[str, object] | None,
) -> tuple[str, Mapping[str, object]]:
    if params is None:
        return sql, {}
    if isinstance(params, Mapping):
        return sql, params
    values = list(params)
    out: list[str] = []
    bound: dict[str, object] = {}
    index = 0
    for char in sql:
        if char == "?":
            name = f"p{index}"
            out.append(f":{name}")
            bound[name] = values[index]
            index += 1
        else:
            out.append(char)
    if index != len(values):
        raise ValueError("SQL parameter count does not match placeholders")
    return "".join(out), bound


def _ensure_sqlite_parent(settings: DatabaseSettings) -> None:
    if settings.dialect != "sqlite":
        return
    database = make_url(settings.url).database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)


def _install_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_database(settings: DatabaseSettings | None = None) -> Database:
    settings = settings or database_settings()
    _ensure_sqlite_parent(settings)
    if settings.dialect == "sqlite":
        engine = create_engine(
            settings.url,
            connect_args={"timeout": 10.0},
            future=True,
        )
        _install_sqlite_pragmas(engine)
    else:
        engine = create_engine(
            settings.url,
            future=True,
            pool_size=int(os.environ.get("DATABASE_POOL_SIZE", "2")),
            max_overflow=int(os.environ.get("DATABASE_MAX_OVERFLOW", "0")),
            pool_pre_ping=True,
        )
    return Database(settings=settings, engine=engine)


def open_database() -> Database:
    return create_database()
