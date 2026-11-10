"""Drain durable invitation mail inside the hosted application, not the operator CLI."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from starlette.concurrency import run_in_threadpool

from api.services.invitations import InvitationService

LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 5.0


@asynccontextmanager
async def invitation_delivery(
    workflow: InvitationService, *, enabled: bool, poll_seconds: float = POLL_SECONDS,
) -> AsyncIterator[None]:
    """Resume pending mail and finish in-flight receipts before normal shutdown."""
    if not enabled:
        yield
        return
    stop = asyncio.Event()

    async def drain() -> None:
        while not stop.is_set():
            try:
                pending = await run_in_threadpool(workflow.repository.pending_mail)
                for mail_id in pending:
                    if stop.is_set():
                        break
                    await run_in_threadpool(workflow.deliver, mail_id)
            except Exception as exc:
                # Database/HTTP exceptions may embed private parameters. Emit
                # the failure class only; durable state remains the recovery source.
                LOGGER.error("Invitation delivery pass failed (%s)", type(exc).__name__)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)

    task = asyncio.create_task(drain(), name="invitation-delivery")
    try:
        yield
    finally:
        stop.set()
        await task
