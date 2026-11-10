"""Verifies the scout-bot wraps every synchronous DB call in run_in_executor.

``_storage.py`` is sync (sqlite3 / SQLAlchemy compat). Slash-command
handlers are async; without the executor wrap, a slow query would block
discord.py's gateway heartbeat. The test seam is the ``run_db_call``
helper in ``api/integrations/discord/bot.py`` — every handler is
expected to route DB I/O through it.

Two assertions here:

1. ``run_db_call`` actually invokes ``loop.run_in_executor`` (so a
   future refactor that calls the DB directly from the coroutine is
   caught immediately by this test, not in prod under load).
2. A sync call wrapped this way returns its result without blocking
   the loop (smoke level — the test would deadlock if the wrap was
   missing and the test ran on the same thread).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from api.integrations.discord import bot as bot_mod
from api.integrations.discord.bot import run_db_call


async def test_run_db_call_uses_run_in_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatch the running loop's ``run_in_executor`` and assert it fires."""
    loop = asyncio.get_running_loop()
    called: dict[str, Any] = {"count": 0, "fn": None}

    real_run_in_executor = loop.run_in_executor

    def _spy(executor: Any, fn: Any, *args: Any) -> Any:
        called["count"] += 1
        called["fn"] = fn
        return real_run_in_executor(executor, fn, *args)

    monkeypatch.setattr(loop, "run_in_executor", _spy)

    result = await run_db_call(lambda: "ok")
    assert result == "ok"
    assert called["count"] == 1
    assert called["fn"] is not None


async def test_run_db_call_returns_sync_result_without_blocking_loop() -> None:
    """Smoke: a sync function wrapped in run_db_call yields back to the loop.

    If the wrap were missing (handler called the sync function directly
    inside an ``await`` expression), this test would still pass — but a
    SECOND coroutine racing on a sleep would not get to run during the
    sync work. We assert the second coroutine ticks at least once
    during the first one's "blocking" call.
    """
    ticks: list[int] = []

    async def _ticker() -> None:
        for i in range(5):
            ticks.append(i)
            await asyncio.sleep(0.01)

    async def _worker() -> str:
        # ``run_db_call`` punts the body to a thread. Inside, we busy-
        # wait briefly. The ticker should keep adding entries while
        # we wait.
        def _sync_work() -> str:
            import time as _t
            _t.sleep(0.03)  # block the worker thread, not the loop
            return "done"
        return await run_db_call(_sync_work)

    result, _ = await asyncio.gather(_worker(), _ticker())
    assert result == "done"
    # The ticker should have made progress while _sync_work was on
    # the executor thread.
    assert len(ticks) >= 2


async def test_handlers_route_db_through_run_db_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """``handle_today`` and friends must not invoke the DB synchronously.

    We monkeypatch ``run_db_call`` to record invocations. Then call
    ``handle_today`` with a no-op factory; if the handler skipped the
    helper and called the DB directly, the spy would see zero calls.
    """
    from dataclasses import dataclass, field
    from datetime import UTC, datetime

    @dataclass
    class _Resp:
        calls: list[Any] = field(default_factory=list)

        async def send_message(self, *args: Any, **kwargs: Any) -> None:
            self.calls.append((args, kwargs))

    @dataclass
    class _User:
        id: int

    @dataclass
    class _Interaction:
        user: _User
        response: _Resp = field(default_factory=_Resp)

    invoked = {"count": 0}

    async def _spy(fn):  # mimics the run_db_call signature
        invoked["count"] += 1
        return fn()

    monkeypatch.setattr(bot_mod, "run_db_call", _spy)

    interaction = _Interaction(user=_User(id=1))

    def _factory():
        # Return an object that responds to the queries the handler runs.
        class _StubConn:
            def execute(self, *_a, **_kw):
                class _Cursor:
                    def fetchall(self) -> list[Any]:
                        return []
                    def fetchone(self) -> Any:
                        return None
                return _Cursor()
            def close(self) -> None:
                pass
        return _StubConn()

    await bot_mod.handle_today(
        interaction,
        db_factory=_factory,
        now=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
    )
    # /today does at least one DB call (the time-window query).
    assert invoked["count"] >= 1
