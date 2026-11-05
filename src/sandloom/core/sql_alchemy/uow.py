"""SQLAlchemy implementation of UnitOfWorkContract.

Note: This module is intentionally placed under core/sql_alchemy as a
transition step. Eventually, all SQL-specific pieces should be moved into a
plugin implementing the same abstract contracts.

A unit opened inside another unit of the same task gets its transaction from
its engine's nesting policy (:mod:`sandloom.core.sql_alchemy.nesting`, chosen
once per engine): its own, or a SAVEPOINT of its caller's.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.sql_alchemy.nesting import (
    FACTORY_POLICY_ATTR,
    NO_FACTORY_KW,
    factory_policy,
)
from sandloom.core.sql_alchemy.write_admission import mark_unit_closed
from sandloom.core.unit_of_work.hooks import _run_before_commit_callbacks
from sandloom.core.unit_of_work.request import release_unit


class SQLAlchemyUnitOfWork(UnitOfWorkContract):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session_factory = session_factory
        self.session: AsyncSession | None = None
        #: the nesting policy's handle of a unit opened inside its caller's
        #: transaction (``close(session)`` hands the caller's back), else None
        self._nested: Any = None

    async def __aenter__(self) -> SQLAlchemyUnitOfWork:
        factory = self._session_factory
        # The engine's nesting policy, cached on the factory with its bind:
        # one attribute read per unit (PostgreSQL: never nests).
        cached = getattr(factory, FACTORY_POLICY_ATTR, None)
        if cached is not None and cached[0] is getattr(
            factory, "kw", NO_FACTORY_KW
        ).get("bind"):
            policy = cached[1]
        else:
            policy = factory_policy(factory)
        if policy.nests and await policy.enter(self):
            return self
        session = factory()
        try:
            await session.begin()
        except BaseException:
            # begin() can fail (pool exhausted, DB unreachable, pre-ping). If it
            # does, __aexit__ never runs, so close the session here to avoid
            # leaking it (and any checked-out connection) back into the pool.
            await session.close()
            raise
        self.session = session
        return self

    def open_lazily(self) -> AsyncSession:
        """Construct the session WITHOUT beginning (no I/O): it autobegins at
        its first statement. The caller (the lazy request unit) commits or
        rolls back and then closes it (:meth:`close`)."""
        session = self._session_factory()
        self.session = session
        return session

    async def close(self) -> None:
        """Close the session and mark the unit exited (lazy request unit)."""
        session, self.session = self.session, None
        if session is not None:
            nested, self._nested = self._nested, None
            if nested is None:
                await session.close()
            else:
                await nested.close(session)
            release_unit(session)
            mark_unit_closed(session)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.session:
            try:
                if exc:
                    await self.rollback()
                else:
                    await self._commit_after_checks()
            finally:
                # Always release the connection, even if commit/rollback raised.
                session, self.session = self.session, None
                # (no helper frame: closed once per unit)
                nested, self._nested = self._nested, None
                if nested is None:
                    await session.close()
                else:
                    await nested.close(session)
                release_unit(session)
                mark_unit_closed(session)

    async def _commit_after_checks(self) -> None:
        """Run the session's ``before_commit`` checks, then commit.

        A failing check rolls the transaction back and propagates (the
        ``on_commit`` queue then never runs: nothing committed).
        """
        assert self.session is not None  # noqa: S101 - type narrowing of an invariant
        try:
            await _run_before_commit_callbacks(self.session)
        except BaseException:
            await self.rollback()
            raise
        await self.commit()

    async def commit(self) -> None:
        if self.session:
            await self.session.commit()

    async def rollback(self) -> None:
        if self.session:
            await self.session.rollback()
