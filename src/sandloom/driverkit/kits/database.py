"""``DatabaseProviderConformance``: what a ``database`` driver must do (spec 11).

A driver's tests subclass the kit and override ``make_provider`` (an unopened
provider of a database the test owns: an empty one, or a scratch one the tests
may create tables in). The kit speaks to the driver through its relational
door: a unit's ``session`` takes ``sqlalchemy.text`` statements, as the
``sqlalchemy`` driver's does. A driver on another engine overrides
:meth:`DatabaseProviderConformance.execute` (and :meth:`begin_savepoint` when it
declares ``savepoints``). Every test uses table names of its own and drops them.

The features the kit exercises: ``transactions`` and ``savepoints`` (the unit's
call shapes), and the pairing of a store with its feature (``transactional_outbox``
with :meth:`~DatabaseProvider.outbox`, ``audit`` with ``audit``,
``command_idempotency`` with ``command_log``, ``http_idempotency`` with
``request_log``). The stores' own behaviour needs the core migration chain and
is the kernel's tests', not the driver's.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, func, insert, select, text
from sqlalchemy.schema import CreateTable, DropTable

from sandloom.core.infra.ports.database import (
    DATABASE_CAPABILITY,
    DatabaseProvider,
)
from sandloom.driverkit.kits._harness import Call, ProviderConformance, require

#: The feature that makes each store method answer a store, not ``None``.
_STORE_FEATURES: dict[str, str] = {
    "outbox": "transactional_outbox",
    "audit": "audit",
    "command_log": "command_idempotency",
    "request_log": "http_idempotency",
}


def _table() -> Table:
    """A table of its own: one ``id`` column, a name no other test shares."""
    return Table(f"kit_db_{uuid.uuid4().hex[:12]}", MetaData(), Column("id", Integer))


@pytest.mark.asyncio
class DatabaseProviderConformance(ProviderConformance[DatabaseProvider]):
    """What a ``database`` driver must do, after the harness's checks."""

    contract = DatabaseProvider
    protocol = None
    spec = DATABASE_CAPABILITY

    # ------------------------------------------------------- the driver seam
    async def execute(self, session: Any, statement: Any) -> list[tuple[Any, ...]]:
        """Run ``statement`` (a SQLAlchemy construct, or SQL text) in the unit's
        ``session``; the rows it answers (none for one that returns no row)."""
        result = await session.execute(
            text(statement) if isinstance(statement, str) else statement
        )
        return [tuple(row) for row in result.fetchall()] if result.returns_rows else []

    def begin_savepoint(self, session: Any) -> AbstractAsyncContextManager[Any]:
        """A nested transaction of ``session`` (a SAVEPOINT) as an async context
        manager that releases it on a normal exit and rolls back to it on an
        exception."""
        return session.begin_nested()  # type: ignore[no-any-return]

    # ----------------------------------------------------------- the helpers
    @asynccontextmanager
    async def _unit(self, provider: DatabaseProvider) -> AsyncIterator[Any]:
        """A unit of the database: it commits on a normal exit, rolls back on an
        exception."""
        async with provider.uow_factory()() as unit:
            yield unit

    @asynccontextmanager
    async def _scratch(self, provider: DatabaseProvider) -> AsyncIterator[Table]:
        """A table of this test's own, dropped afterwards."""
        table = _table()
        async with self._unit(provider) as unit:
            await self.execute(unit.session, CreateTable(table))
        try:
            yield table
        finally:
            with suppress(Exception):
                async with self._unit(provider) as unit:
                    await self.execute(unit.session, DropTable(table))

    async def _count(self, provider: DatabaseProvider, table: Table) -> int:
        async with self._unit(provider) as unit:
            rows = await self.execute(
                unit.session, select(func.count()).select_from(table)
            )
        return int(rows[0][0])

    async def _select_one(self, provider: DatabaseProvider) -> None:
        async with self._unit(provider) as unit:
            rows = await self.execute(unit.session, "SELECT 1")
        require(rows == [(1,)], f"SELECT 1 answered {rows!r}")

    def feature_calls(self, provider: DatabaseProvider) -> Mapping[str, Call]:
        async def transactions() -> None:
            await self._select_one(provider)

        async def savepoints() -> None:
            async with (
                self._unit(provider) as unit,
                self.begin_savepoint(unit.session),
            ):
                await self.execute(unit.session, "SELECT 1")

        return {"transactions": transactions, "savepoints": savepoints}

    def operation_calls(self, provider: DatabaseProvider) -> Mapping[str, Call]:
        return {"uow": lambda: self._select_one(provider)}

    # --------------------------------------------------------- uow_factory
    async def test_uow_factory_is_callable_and_makes_a_unit_per_call(
        self, provider: DatabaseProvider
    ) -> None:
        factory = provider.uow_factory()
        require(callable(factory), "uow_factory() must return a callable")
        first, second = factory(), factory()
        require(first is not second, "the factory must make a new unit per call")

    async def test_a_unit_runs_a_statement_in_its_session(
        self, provider: DatabaseProvider
    ) -> None:
        await self._select_one(provider)

    # ------------------------------------------------------- transactions
    async def test_a_committed_write_is_seen_by_the_next_unit(
        self, provider: DatabaseProvider
    ) -> None:
        self.requires_feature(provider, "transactions")
        async with self._scratch(provider) as table:
            async with self._unit(provider) as unit:
                await self.execute(unit.session, insert(table).values(id=1))
            require(
                await self._count(provider, table) == 1,
                "a unit that exited normally must commit its write",
            )

    async def test_a_unit_that_raises_rolls_its_write_back(
        self, provider: DatabaseProvider
    ) -> None:
        self.requires_feature(provider, "transactions")
        async with self._scratch(provider) as table:
            with pytest.raises(RuntimeError, match="kit-abort"):
                async with self._unit(provider) as unit:
                    await self.execute(unit.session, insert(table).values(id=1))
                    raise RuntimeError("kit-abort")
            require(
                await self._count(provider, table) == 0,
                "a unit that raised must roll its write back",
            )

    async def test_a_unit_does_not_see_the_uncommitted_write_of_another(
        self, provider: DatabaseProvider
    ) -> None:
        self.requires_feature(provider, "transactions")
        self.requires_feature(provider, "concurrent_writers")
        async with (
            self._scratch(provider) as table,
            self._unit(provider) as writer,
        ):
            await self.execute(writer.session, insert(table).values(id=1))
            async with self._unit(provider) as reader:
                rows = await self.execute(
                    reader.session, select(func.count()).select_from(table)
                )
            require(
                rows[0][0] == 0,
                "a unit must not see the write another unit has not committed",
            )

    # ---------------------------------------------------------- savepoints
    async def test_a_rolled_back_savepoint_undoes_only_its_writes(
        self, provider: DatabaseProvider
    ) -> None:
        self.requires_feature(provider, "savepoints")
        async with self._scratch(provider) as table:
            async with self._unit(provider) as unit:
                await self.execute(unit.session, insert(table).values(id=1))
                with pytest.raises(RuntimeError, match="kit-undo"):
                    async with self.begin_savepoint(unit.session):
                        await self.execute(unit.session, insert(table).values(id=2))
                        raise RuntimeError("kit-undo")
            async with self._unit(provider) as unit:
                rows = await self.execute(unit.session, select(table.c.id))
            require(
                rows == [(1,)],
                f"the unit's own write must survive a rolled-back savepoint, and "
                f"the savepoint's must not: the table holds {rows!r}",
            )

    async def test_a_released_savepoint_keeps_its_writes(
        self, provider: DatabaseProvider
    ) -> None:
        self.requires_feature(provider, "savepoints")
        async with self._scratch(provider) as table:
            async with (
                self._unit(provider) as unit,
                self.begin_savepoint(unit.session),
            ):
                await self.execute(unit.session, insert(table).values(id=1))
            require(
                await self._count(provider, table) == 1,
                "a savepoint that exited normally must keep its write",
            )

    # -------------------------------------------------------------- stores
    async def test_a_store_is_answered_exactly_when_its_feature_is_declared(
        self, provider: DatabaseProvider
    ) -> None:
        declared = provider.features()
        for method, feature in _STORE_FEATURES.items():
            store = getattr(provider, method)()
            if feature in declared:
                require(
                    store is not None,
                    f"{feature!r} is declared, but {method}() answers None",
                )
            else:
                require(
                    store is None,
                    f"{feature!r} is not declared, but {method}() answers a store: "
                    "a store method answers None exactly when its feature is "
                    "not declared",
                )

    # ------------------------------------------------- has_pending_changes
    async def test_a_fresh_session_has_no_pending_changes(
        self, provider: DatabaseProvider
    ) -> None:
        async with self._unit(provider) as unit:
            answer = provider.has_pending_changes(unit.session)
        require(
            answer is False,
            f"has_pending_changes of a session nothing wrote to must be False, "
            f"not {answer!r}",
        )

    # --------------------------------------------------- ensure_model_schema
    async def test_ensure_model_schema_answers_what_it_did(
        self, make_provider: Callable[[], DatabaseProvider]
    ) -> None:
        """``True`` builds the schema of the metadata passed (and is repeatable);
        ``False`` (the migration chains own the schema) builds nothing."""
        table = _table()
        metadata = table.metadata
        built = make_provider()
        await built.aopen()
        try:
            answer = await built.ensure_model_schema(metadata, recreate=False)
            require(
                isinstance(answer, bool),
                f"ensure_model_schema must answer a bool, not {answer!r}",
            )
            if answer:
                require(
                    await self._count(built, table) == 0,
                    "ensure_model_schema answered True: the table of the "
                    "metadata must exist",
                )
                again = await built.ensure_model_schema(metadata, recreate=False)
                require(
                    again is True,
                    "ensure_model_schema must be repeatable on a database it "
                    f"already built (answered {again!r})",
                )
            else:
                with pytest.raises(Exception):  # noqa: B017 - the driver's own error type
                    await self._count(built, table)
        finally:
            with suppress(Exception):
                async with self._unit(built) as unit:
                    await self.execute(unit.session, DropTable(table, if_exists=True))
            await built.aclose()
