"""Relational execution adapter of the execution planner.

Implements :class:`sandloom.core.planner.ExecutionAdapter` for one SQLAlchemy
database instance. Two executors, ONE connection lifecycle:

``lease()``
    the only way either executor gets a connection. It refuses a pooled
    connection that is already inside a transaction, and on ANY error or
    cancellation TERMINATES the driver connection (synchronously — the server
    aborts its transaction and the tenant GUC dies with it, even if the task is
    cancelled again) and invalidates it, so a connection whose transaction state
    is unknown is never pooled. A lease that ends with a transaction still open
    is invalidated too.

``session(ctx)`` — GENERAL executor (any declared operation)
    an ORM ``AsyncSession`` bound to the leased connection with
    ``postgresql_readonly`` (``BEGIN READ ONLY``), the context's tenant bound
    (ORM tenant guard + the engine's ``tenant_rls`` hook set the GUC), always
    rolled back.

``raw_transaction(ctx)`` — OPTIMIZED executor (declared simple reads only)
    the asyncpg connection under the lease; on a ``tenant_rls`` engine
    ``BEGIN READ ONLY; SELECT set_config(tenant)`` in ONE simple-protocol
    message, then the generated statement, then ``COMMIT``. The read-only
    transaction is required even without RLS: a SELECT from a view can invoke
    a volatile function that writes. The generated
    statement always carries the tenant predicate for a tenant-scoped entity.

The tenant value comes from the :class:`ExecutionContext` built for THIS call;
nothing per-execution is stored in a plan.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from sandloom.core.planner import (
    ExecutionContext,
    Plan,
    PlanningError,
    Reads,
)
from sandloom.core.sql_alchemy.planner_adapter.compiler import _PlannerCompiler
from sandloom.core.sql_alchemy.planner_adapter.forms import (
    _Compiled,
    _CompiledList,
    begin_read_only_sql,
)
from sandloom.core.sql_alchemy.planner_adapter.units import (
    _general_page,
    _general_read,
    _Lease,
    _live_raw,
    _ReadUnit,
    _terminate,
)
from sandloom.core.tenant_context import _current_tenant, bind_tenant
from sandloom.core.unit_of_work.scope import is_active_uow_session, uow_database_name


class SQLAlchemyExecutionAdapter(_PlannerCompiler):
    """Planner adapter for one SQLAlchemy database instance (see module doc)."""

    # ------------------------------------------------- the shared lifecycle
    async def _cp(self, phase: str) -> None:
        if self.checkpoint is not None:
            await self.checkpoint(phase)

    def lease(self) -> _Lease:
        """A pooled connection that is never returned in an unknown state.

        ``async with adapter.lease() as (conn, raw):`` — the ONLY way either
        executor gets a connection (see :class:`_Lease`).
        """
        return _Lease(self)

    @asynccontextmanager
    async def session(self, ctx: ExecutionContext) -> AsyncIterator[AsyncSession]:
        """GENERAL executor scope: read-only ORM session, tenant bound, rolled back.

        The hot path (:meth:`fetch_one`) inlines exactly these steps; this
        scope serves probes and tests that need a session under the lease.
        """
        async with self.lease() as (conn, raw):
            self._read_only(conn)
            session = AsyncSession(bind=conn, expire_on_commit=False, autoflush=False)
            try:
                if ctx.tenant is not None:
                    with bind_tenant(ctx.tenant):
                        yield session
                else:
                    yield session
            except BaseException:
                # before any await: never a graceful ROLLBACK
                _terminate(_live_raw(conn, raw))
                with suppress(Exception):
                    await session.close()
                raise
            await self._cp("fetched")
            await session.close()  # ROLLBACK (read-only: nothing to keep)
            await self._cp("ended")

    @asynccontextmanager
    async def raw_transaction(self, ctx: ExecutionContext) -> AsyncIterator[Any]:
        """OPTIMIZED executor scope: BEGIN READ ONLY + GUC | ... | COMMIT."""
        lease = self.lease()
        async with lease:
            raw = await lease.first("execute", begin_read_only_sql(ctx.tenant))
            await self._cp("begun")
            yield raw
            await self._cp("fetched")
            await raw.execute("COMMIT")
            await self._cp("ended")

    def _read_only(
        self, conn: AsyncConnection, isolation: str = "READ COMMITTED"
    ) -> None:
        """``BEGIN READ ONLY`` at ``isolation`` for this checkout (reset by the
        pool on return).

        The SYNC ``Connection.execution_options``: on asyncpg the readonly and
        isolation characteristics are attributes of the DBAPI adapter (no I/O,
        no open transaction yet), so the greenlet hop of the async wrapper is
        skipped. READ COMMITTED is the contract's default: when the server's
        default isolation (read once per adapter from the dialect, set at the
        first connect) IS READ COMMITTED, setting it again per checkout would
        only cost instructions (~0.1 M per request, cachegrind); otherwise it
        is pinned explicitly on every checkout.
        """
        if not self._postgresql:
            return
        sync = conn.sync_connection
        assert sync is not None  # noqa: S101 - type narrowing of an invariant
        if isolation == "READ COMMITTED" and self._server_read_committed(sync):
            sync.execution_options(postgresql_readonly=True)
        else:
            sync.execution_options(postgresql_readonly=True, isolation_level=isolation)

    def _server_read_committed(self, sync: Any) -> bool:
        verdict = self._rc_default
        if verdict is None:
            level = getattr(sync.dialect, "default_isolation_level", None)
            verdict = self._rc_default = str(level).upper() == "READ COMMITTED"
        return verdict

    # ------------------------------------------------------------ execution
    async def fetch_one(
        self, plan: Plan[Any], ctx: ExecutionContext, key: Sequence[Any]
    ) -> Sequence[Any] | None:
        """Run ``plan`` under ``ctx`` (one lease, phases inlined: no wrappers)."""
        c: _Compiled = plan.compiled
        cp = self.checkpoint
        if plan.executor == "optimized":
            assert c.sql is not None  # noqa: S101 - type narrowing of an invariant
            args = (
                (*key, *c.consts, ctx.tenant)
                if plan.tenant_scoped
                else (
                    *key,
                    *c.consts,
                )
            )
            lease = _Lease(self)
            async with lease:
                raw = await lease.first("execute", begin_read_only_sql(ctx.tenant))
                if cp is not None:
                    await cp("begun")
                row = await raw.fetchrow(c.sql, *args)
                if cp is not None:
                    await cp("fetched")
                await raw.execute("COMMIT")
                if cp is not None:
                    await cp("ended")
            return None if row is None else tuple(row)
        async with _Lease(self) as (conn, raw):
            self._read_only(conn)
            session = AsyncSession(bind=conn, expire_on_commit=False, autoflush=False)
            token = None
            if ctx.tenant is not None and _current_tenant.get() != ctx.tenant:
                token = _current_tenant.set(ctx.tenant)
            try:
                values = await _general_read(session, plan, c, key, ctx)
            except BaseException:
                # before any await: never a graceful ROLLBACK
                _terminate(_live_raw(conn, raw))
                with suppress(Exception):
                    await session.close()
                raise
            finally:
                if token is not None:
                    _current_tenant.reset(token)
            if cp is not None:
                await cp("fetched")
            await session.close()  # ROLLBACK (read-only: nothing to keep)
            if cp is not None:
                await cp("ended")
        return values

    def read_unit(self, isolation: str = "READ COMMITTED") -> _ReadUnit:
        """A scope-wide read-only unit for the GENERAL executor (lazy lease).

        ``isolation`` is the unit's transaction isolation (explicit, reset by
        the pool on return): READ COMMITTED by default, REPEATABLE READ for a
        one-snapshot scope.
        """
        return _ReadUnit(self, isolation)

    async def fetch_page(  # noqa: C901
        self,
        plan: Plan[Any],
        ctx: ExecutionContext,
        key: Sequence[Any],
        limit: int,
        after: Sequence[Any] | None,
        unit: AsyncSession | None = None,
    ) -> list[Sequence[Any]]:
        """One ListOperation page: on ``unit`` (general form, its transaction),
        else in its own read-only transaction on the plan's executor."""
        c: _CompiledList = plan.compiled
        form, key, names = c.bind(key)
        if unit is not None:
            _check_unit(plan, ctx, unit)
            token = None
            if ctx.tenant is not None and _current_tenant.get() != ctx.tenant:
                token = _current_tenant.set(ctx.tenant)
            try:
                return await _general_page(unit, c, form, names, key, ctx, limit, after)
            finally:
                if token is not None:
                    _current_tenant.reset(token)
        cp = self.checkpoint
        if plan.executor == "optimized":
            sql = form.sql if after is None else form.sql_after
            assert sql is not None  # noqa: S101 - type narrowing of an invariant
            args = c.sql_args(key, ctx.tenant, after, limit)
            lease = _Lease(self)
            async with lease:
                raw = await lease.first("execute", begin_read_only_sql(ctx.tenant))
                if cp is not None:
                    await cp("begun")
                rows = await raw.fetch(sql, *args)
                if cp is not None:
                    await cp("fetched")
                await raw.execute("COMMIT")
                if cp is not None:
                    await cp("ended")
            return [tuple(r) for r in rows]
        async with _Lease(self) as (conn, raw):
            self._read_only(conn)
            session = AsyncSession(bind=conn, expire_on_commit=False, autoflush=False)
            token = None
            if ctx.tenant is not None and _current_tenant.get() != ctx.tenant:
                token = _current_tenant.set(ctx.tenant)
            try:
                out = await _general_page(
                    session, c, form, names, key, ctx, limit, after
                )
            except BaseException:
                _terminate(_live_raw(conn, raw))
                with suppress(Exception):
                    await session.close()
                raise
            finally:
                if token is not None:
                    _current_tenant.reset(token)
            if cp is not None:
                await cp("fetched")
            await session.close()
            if cp is not None:
                await cp("ended")
        return out

    async def fetch_in_unit(
        self,
        plan: Plan[Any],
        ctx: ExecutionContext,
        key: Sequence[Any],
        unit: AsyncSession,
    ) -> Sequence[Any] | None:
        """The GENERAL form on the Unit of Work's own session (mixed routes).

        No lease: the UoW owns the connection, the transaction (commit /
        rollback, outbox, audit, on_commit) and its tenant GUC (the engine's
        ``tenant_rls`` hook syncs it to the bound tenant before the statement,
        as for any ORM statement of the UoW). ORM autoflush and the identity
        map give read-your-own-writes. An error propagates to the UoW, which
        rolls back.
        """
        _check_unit(plan, ctx, unit)
        token = None
        if ctx.tenant is not None and _current_tenant.get() != ctx.tenant:
            token = _current_tenant.set(ctx.tenant)
        try:
            return await _general_read(unit, plan, plan.compiled, key, ctx)
        finally:
            if token is not None:
                _current_tenant.reset(token)


def _check_unit(plan: Plan[Any], ctx: ExecutionContext, unit: AsyncSession) -> None:
    """A UoW-bound handle may only run inside ITS open unit, on its database."""
    database = uow_database_name(unit)  # None: caller-owned, not a UoW's
    if (
        database is not None
        and not is_active_uow_session(unit)
        and not unit.in_transaction()
    ):
        # A handle that outlived its unit (leaked into another request /
        # task, or used after the UoW closed): running it would autobegin
        # a NEW transaction outside any UoW. Refuse.
        raise PlanningError(
            f"{plan.operation.name}: UoW reads handle used outside its Unit "
            "of Work (the unit is closed or belongs to another scope)"
        )
    if database is not None and database != ctx.database:
        raise PlanningError(
            f"{plan.operation.name}: declared on database {ctx.database!r} but "
            f"the Unit of Work governs {database!r}; there is no cross-database "
            "transaction"
        )


def adapter_for_session(session: AsyncSession) -> SQLAlchemyExecutionAdapter:
    """The planner adapter of the engine ``session`` is bound to (cached).

    For code that holds a session but no core (a contract body on
    ``contract_session()``, a worker on ``db_session()``).
    """
    bind: Any = session.bind
    engine: Any = bind.engine if isinstance(bind, AsyncConnection) else bind
    if not isinstance(engine, AsyncEngine):
        raise PlanningError(f"session bound to {bind!r}: no async engine to plan on")
    sync = engine.sync_engine
    adapter: SQLAlchemyExecutionAdapter | None = sync.__dict__.get(_ADAPTER_ATTR)
    if adapter is None or adapter.engine.sync_engine is not sync:
        adapter = SQLAlchemyExecutionAdapter(engine)
        sync.__dict__[_ADAPTER_ATTR] = adapter
    return adapter


def session_reads(session: AsyncSession) -> Reads:
    """A :class:`~sandloom.core.planner.Reads` handle that runs ON ``session``.

    Use it inside a Unit of Work (request ``DbSession``, ``db_session()``,
    ``contract_session()``): declared operations then share the unit's
    transaction, connection and tenant context (general executor).
    """
    return Reads(None, session, adapter=adapter_for_session(session))


_ADAPTER_ATTR = "_sandloom_planner_session_adapter"


__all__ = [
    "SQLAlchemyExecutionAdapter",
    "adapter_for_session",
    "session_reads",
]
