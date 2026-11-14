"""The sweeper of ``infra_atomic_store``: delete the rows the ``database``
atomic_store driver already treats as absent.

The driver filters expiry on every statement against the database's clock, so a
row left behind is never visible; this only reclaims space. The relay's
housekeeping (``sandloom.core.tasks.relay``) runs it on every database it
drains, next to the purge of the outbox and of the dedupe logs: the table is
part of the core chain, so every hosting database has it and only the one the
driver is configured on holds rows.

A row is purged once it has been expired for ``grace_seconds``: the sweeper
compares with this process's clock, the driver with the database's, and the
grace absorbs their skew, so a row the driver still reads as live is never
deleted. The expiry is checked again on the outer ``DELETE`` (the victim
subquery reads its snapshot), so a key a concurrent write is re-creating is not
deleted once that write commits.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import Table, and_, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError

from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.sql_alchemy.models.atomic_store import InfraAtomicStore

logger = logging.getLogger(__name__)

#: A row is purged this long after it expired (clock skew between this process
#: and the database).
DEFAULT_GRACE_SECONDS = 60.0


async def purge_expired_atomic_entries(
    uow_factory: Any,
    *,
    limit: int,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
) -> int:
    """``DELETE`` up to ``limit`` rows expired for over ``grace_seconds``."""
    if limit <= 0:
        return 0
    table = cast("Table", InfraAtomicStore.__table__)
    cutoff = datetime.now(UTC) - timedelta(seconds=grace_seconds)
    due = and_(table.c.expires_at.is_not(None), table.c.expires_at < cutoff)
    victims = select(table.c.key).where(due).limit(limit).scalar_subquery()
    statement = delete(table).where(and_(table.c.key.in_(victims), due))
    try:
        unit: UnitOfWorkContract = uow_factory()
        async with unit as uow:
            session: Any = uow.session
            result = cast("CursorResult[Any]", await session.execute(statement))
            return int(getattr(result, "rowcount", 0) or 0)
    except SQLAlchemyError as exc:
        # A database whose core chain predates the table (or that cannot answer
        # now): nothing to reclaim, and the relay's other housekeeping goes on.
        logger.debug("atomic store purge skipped: %s", type(exc).__name__)
        return 0


__all__ = ["DEFAULT_GRACE_SECONDS", "purge_expired_atomic_entries"]
