"""Outbox repository for SQLAlchemy sessions (temporary implementation).

Provides a thin data access layer around the OutboxMessage model.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.sql_alchemy.models.outbox import OutboxMessage


class SQLAlchemyOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        channel: str,
        payload: dict[str, object],
        *,
        schema_version: str = "v1",
    ) -> None:
        msg = OutboxMessage(
            channel=channel, schema_version=schema_version, payload=payload
        )
        self._session.add(msg)
        # Do not commit here; UoW governs transaction boundaries
