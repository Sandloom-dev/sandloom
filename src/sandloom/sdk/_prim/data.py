"""New L2 data primitives of ``sandloom.sdk.data`` (spec §2.3, §4.3).

``db()`` is the session of the running unit, ``open_uow()`` opens a unit
(today ``db_session``), ``MoneyLedger`` names a money ledger's unique
operation-key column (ADR-0009). All three are the kernel's (H1, WS-A).
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.di import db as _kernel_db
from sandloom.core.di import db_session
from sandloom.core.orm import MoneyLedger, Page, TenantScoped


def db() -> AsyncSession:
    """The session of the innermost open unit of the current task (§4.3)::

        item = Item(title=body.title)
        db().add(item)
        await db().flush()   # never commit(): the unit owns the transaction

    Outside any unit it raises ``UnitOfWorkRequiredError`` (SANDLOOM-R002); in a
    request the first call opens the lazy request unit, committed when the
    handler succeeds and rolled back on an exception. A child task opens its
    own unit with :func:`open_uow` (SANDLOOM-R013 otherwise).
    """
    return cast(AsyncSession, _kernel_db())


def open_uow(database: str | None = None) -> AbstractAsyncContextManager[AsyncSession]:
    """Open a unit of work on ``database`` (today ``db_session``, §4.3)::

        async with open_uow() as session:
            session.add(row)          # committed when the block exits

    One transaction, committed on success and rolled back on an exception,
    with the transactional outbox for the events it publishes.
    """
    return db_session(database or "default")


__all__ = ["MoneyLedger", "Page", "TenantScoped", "db", "open_uow"]
