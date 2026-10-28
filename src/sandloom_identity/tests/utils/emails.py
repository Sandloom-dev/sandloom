"""Deliver identity's after-commit emails in tests.

Identity queues every email with ``on_commit`` and sends it in the background
(``auth_service._email_after_commit``). A test that asserts on a send treats
the current Unit of Work as committed and waits for the deliveries.
"""

from __future__ import annotations

from typing import Any

from sandloom.sdk.testing import run_on_commit
from sandloom_identity.services.auth_service import drain_pending_emails


async def deliver_emails(*sessions: Any) -> None:
    """Run the on-commit queue of ``sessions`` (default: the bound UoW), then
    wait for the background deliveries it spawned."""
    if not sessions:
        await run_on_commit()
    for session in sessions:
        await run_on_commit(session)
    await drain_pending_emails()
