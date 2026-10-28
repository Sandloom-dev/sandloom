"""Read units and leases: the transactions the planner executors run in.

:class:`_ReadUnit` is the shared read-only transaction of a request read scope
(general executor) and :class:`_Lease` one optimized-executor connection
lease; plus the raw-driver connection helpers both use.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    inspect,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from sandloom.core.exceptions import DatabaseUnavailable
from sandloom.core.planner import (
    ExecutionContext,
    Plan,
)
from sandloom.core.sql_alchemy.dead_connection import (
    count_recovery,
    is_dead_connection_error,
    never_sent,
    recovery_enabled,
)
from sandloom.core.sql_alchemy.planner_adapter.forms import (
    _TENANT_PARAM,
    LeaseError,
    _Compiled,
    _CompiledList,
    _ListForm,
    begin_read_only_sql,
)
from sandloom.core.sql_alchemy.rls import TENANT_SETTING
from sandloom.core.tenant_context import _current_tenant

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.core.sql_alchemy.planner_adapter import SQLAlchemyExecutionAdapter


def _in_transaction(raw: Any) -> bool:
    probe = getattr(raw, "is_in_transaction", None)
    return bool(probe()) if callable(probe) else False


def _live_raw(conn: AsyncConnection | None, fallback: Any) -> Any:
    """The driver connection currently under ``conn`` (no reconnect attempt).

    A statement of the general executor replaces a pooled connection found
    dead at the transaction's first message in place
    (:mod:`sandloom.core.sql_alchemy.dead_connection`): the driver connection read
    at lease time is then stale.
    """
    sync = getattr(conn, "sync_connection", None)
    fairy = getattr(sync, "_dbapi_connection", None)
    dbapi = getattr(fairy, "dbapi_connection", None)
    if dbapi is None:
        return fallback
    return getattr(dbapi, "driver_connection", fallback)


def _terminate(raw: Any) -> None:
    """Kill the server session NOW (sync): its transaction and GUC die with it."""
    if raw is not None:
        with suppress(Exception):
            raw.terminate()


async def _general_page(
    session: AsyncSession,
    c: _CompiledList,
    form: _CompiledList | _ListForm,
    names: Sequence[str],
    key: Sequence[Any],
    ctx: ExecutionContext,
    limit: int,
    after: Sequence[Any] | None,
) -> list[Sequence[Any]]:
    """The GENERAL executor's page on ``session`` (ORM, tenant guard on);
    ``form`` / ``names`` / ``key``: :meth:`_CompiledList.bind`."""
    params: dict[str, Any] = dict(zip(names, key, strict=True))
    for i, value in enumerate(c.where_consts):
        params[f"_w{i}"] = value
    for i, value in enumerate(c.join_consts):
        params[f"_c{i}"] = value
    if c.tenant_param:
        params[_TENANT_PARAM] = ctx.tenant
    params["_limit"] = limit
    stmt = form.stmt
    if after is not None:
        stmt = form.stmt_after
        for i, value in enumerate(after):
            params[f"_k{i}"] = value
    rows = (await session.execute(stmt, params)).all()
    if c.columns:
        return [tuple(r) for r in rows]
    n = c.n_entities
    return [
        tuple([getattr(r[side], attr) for side, attr in c.getters]) + tuple(r[n:])
        for r in rows
    ]


async def _general_read(
    session: AsyncSession,
    plan: Plan[Any],
    c: _Compiled,
    key: Sequence[Any],
    ctx: ExecutionContext,
) -> tuple[Any, ...] | None:
    """The GENERAL executor's statement on ``session`` (ORM, tenant guard on)."""
    entity: Any = c.entity
    obj: Any
    if c.join:
        params: dict[str, Any] = dict(zip(plan.operation.keys, key, strict=True))
        for i, value in enumerate(c.consts):
            params[f"_c{i}"] = value
        if c.tenant_param:
            params[_TENANT_PARAM] = ctx.tenant
        row = (await session.execute(c.stmt, params)).one_or_none()
        if row is None:
            return None
        if c.columns:
            return tuple(row)
        pair = (row[0], row[1])
        return tuple([getattr(pair[side], attr) for side, attr in c.getters])
    if c.pk_get:
        obj = await session.get(entity, key[0] if len(key) == 1 else tuple(key))
        if obj is not None:
            # An identity-map hit inside a unit can carry EXPIRED attributes
            # (a flushed server-side ``onupdate`` column, a committed unit
            # with expire_on_commit): ``get`` does not reload them and a
            # plain attribute access would lazy-load outside the greenlet.
            # Reload exactly those DTO columns, as a SELECT would. Only
            # EXPIRED ones: a column never loaded by policy (deferred,
            # raiseload) keeps raising on access, as on the ORM path.
            expired = inspect(obj).expired_attributes
            missing = [f for f in plan.fields if f in expired]
            if missing:
                await session.refresh(obj, attribute_names=missing)
    else:
        by_params = dict(zip(plan.operation.by, key, strict=True))
        obj = (await session.execute(c.stmt, by_params)).scalar_one_or_none()
    if obj is None:
        return None
    return tuple([getattr(obj, f) for f in plan.fields])


class _ReadUnit:
    """A lazy read-only lease shared by a scope's sequential reads.

    Optimized READ COMMITTED reads share a raw transaction. If a general read
    follows, end and release that lease before opening the ORM transaction;
    subsequent reads use the ORM session. READ COMMITTED takes a fresh snapshot
    per statement either way. Stronger isolation always uses one ORM unit.
    A failure aborts the unit permanently; cancellation never pools an open
    transaction. Each read rebinds the transaction-local tenant if needed.
    """

    __slots__ = (
        "_adapter",
        "_lease",
        "_session",
        "_raw",
        "_broken",
        "_isolation",
        "_raw_active",
        "_raw_tenant",
    )

    def __init__(
        self, adapter: SQLAlchemyExecutionAdapter, isolation: str = "READ COMMITTED"
    ) -> None:
        self._adapter = adapter
        self._isolation = isolation
        self._lease: _Lease | None = None
        self._session: AsyncSession | None = None
        self._raw: Any = None
        self._broken = False
        self._raw_active = False
        self._raw_tenant: uuid.UUID | None = None

    async def fetch(
        self, plan: Plan[Any], ctx: ExecutionContext, key: Sequence[Any]
    ) -> Sequence[Any] | None:
        if self._broken:
            raise LeaseError(
                f"{plan.operation.name}: the scope's read unit was aborted by an "
                "earlier error"
            )
        token = None
        if ctx.tenant is not None and _current_tenant.get() != ctx.tenant:
            token = _current_tenant.set(ctx.tenant)
        try:
            raw = await self._optimized(plan, ctx)
            if raw is not None:
                c: _Compiled = plan.compiled
                assert c.sql is not None  # noqa: S101 - type narrowing of an invariant
                args = (
                    (*key, *c.consts, ctx.tenant)
                    if plan.tenant_scoped
                    else (*key, *c.consts)
                )
                row = await raw.fetchrow(c.sql, *args)
                values = None if row is None else tuple(row)
            else:
                session = self._session or await self._open()
                values = await _general_read(session, plan, plan.compiled, key, ctx)
            cp = self._adapter.checkpoint
            if cp is not None:
                await cp("fetched")
        except BaseException as exc:
            await self._abort()
            if (
                isinstance(exc, Exception)
                and not isinstance(exc, DatabaseUnavailable)
                and is_dead_connection_error(exc)
            ):
                raise DatabaseUnavailable(exc) from exc
            raise
        finally:
            if token is not None:
                _current_tenant.reset(token)
        return values

    async def _optimized(self, plan: Plan[Any], ctx: ExecutionContext) -> Any:
        if (
            plan.executor != "optimized"
            or self._session is not None
            or self._isolation != "READ COMMITTED"
        ):
            return None
        if not self._raw_active:
            lease = self._lease = _Lease(self._adapter)
            await lease.__aenter__()
            self._raw = await lease.first("execute", begin_read_only_sql(ctx.tenant))
            self._raw_active, self._raw_tenant = True, ctx.tenant
            cp = self._adapter.checkpoint
            if cp is not None:
                await cp("begun")
        elif self._raw_tenant != ctx.tenant:
            await self._raw.execute(
                f"SELECT set_config('{TENANT_SETTING}', $1, true)",
                "" if ctx.tenant is None else str(ctx.tenant),
            )
            self._raw_tenant = ctx.tenant
        return self._raw

    async def _open(self) -> AsyncSession:
        if self._raw_active:
            # No second checkout while holding the first: pools of one work.
            await self.close()
        lease = _Lease(self._adapter)
        conn, raw = await lease.__aenter__()  # aborts itself on failure
        self._lease, self._raw = lease, raw
        try:
            self._adapter._read_only(conn, self._isolation)
            session = self._session = AsyncSession(
                bind=conn, expire_on_commit=False, autoflush=False
            )
            return session
        except BaseException:
            # The lease already exists even if session/isolation setup fails.
            await self._abort()
            raise

    async def fetch_page(
        self,
        plan: Plan[Any],
        ctx: ExecutionContext,
        key: Sequence[Any],
        limit: int,
        after: Sequence[Any] | None,
    ) -> list[Sequence[Any]]:
        if self._broken:
            raise LeaseError(
                f"{plan.operation.name}: the scope's read unit was aborted by an "
                "earlier error"
            )
        token = None
        if ctx.tenant is not None and _current_tenant.get() != ctx.tenant:
            token = _current_tenant.set(ctx.tenant)
        try:
            c: _CompiledList = plan.compiled
            form, key, names = c.bind(key)
            raw = await self._optimized(plan, ctx)
            if raw is not None:
                sql = form.sql if after is None else form.sql_after
                assert sql is not None  # noqa: S101 - type narrowing of an invariant
                rows: list[Sequence[Any]] = [
                    tuple(r)
                    for r in await raw.fetch(
                        sql, *c.sql_args(key, ctx.tenant, after, limit)
                    )
                ]
            else:
                session = self._session or await self._open()
                rows = await _general_page(
                    session, c, form, names, key, ctx, limit, after
                )
            cp = self._adapter.checkpoint
            if cp is not None:
                await cp("fetched")
        except BaseException as exc:
            await self._abort()
            if (
                isinstance(exc, Exception)
                and not isinstance(exc, DatabaseUnavailable)
                and is_dead_connection_error(exc)
            ):
                raise DatabaseUnavailable(exc) from exc
            raise
        finally:
            if token is not None:
                _current_tenant.reset(token)
        return rows

    async def close(self) -> None:
        lease, session = self._lease, self._session
        if lease is None:
            return
        try:
            if self._raw_active:
                await self._raw.execute("COMMIT")
            elif session is not None:
                await session.close()  # ROLLBACK (read-only: nothing to keep)
            cp = self._adapter.checkpoint
            if cp is not None:
                await cp("ended")
        except BaseException as exc:
            await self._abort()
            await lease.__aexit__(type(exc), exc, None)
            raise
        try:
            await lease.__aexit__(None, None, None)
        except BaseException:
            self._broken = True
            raise
        finally:
            self._lease = self._session = None
            self._raw = None
            self._raw_active, self._raw_tenant = False, None

    async def _abort(self) -> None:
        self._broken = True
        lease, session = self._lease, self._session
        # synchronous: the server aborts the transaction
        _terminate(lease.raw if lease is not None else self._raw)
        self._lease = self._session = None
        self._raw = None
        self._raw_active, self._raw_tenant = False, None
        try:
            if session is not None:
                with suppress(Exception):
                    await session.close()
        finally:
            if lease is not None:
                await lease._abort()


class _Lease:
    """The connection lease (class-based: no generator frame per request).

    Refuses a pooled connection already inside a transaction; on ANY error or
    cancellation TERMINATES the driver connection synchronously (the server
    aborts the transaction, the tenant GUC dies with it) and invalidates it; a
    lease that ends with a transaction still open is invalidated too.
    """

    __slots__ = ("_adapter", "_conn", "_raw")

    def __init__(self, adapter: SQLAlchemyExecutionAdapter) -> None:
        self._adapter = adapter
        self._conn: AsyncConnection | None = None
        self._raw: Any = None

    @property
    def raw(self) -> Any:
        """The driver connection under the lease NOW (a statement of the
        general executor may have replaced a connection found dead)."""
        return _live_raw(self._conn, self._raw)

    async def __aenter__(self) -> tuple[AsyncConnection, Any]:
        await self._acquire()
        assert self._conn is not None  # noqa: S101 - type narrowing of an invariant
        return self._conn, self._raw

    async def _acquire(self) -> None:
        conn = self._conn = await self._adapter.engine.connect()
        try:
            # The checked-out pool connection is already established: reading
            # its driver connection needs no greenlet hop (get_raw_connection).
            raw = self._raw = conn.sync_connection.connection.driver_connection  # type: ignore[union-attr]
            if _in_transaction(raw):
                raise LeaseError("pooled connection handed out inside a transaction")
            cp = self._adapter.checkpoint
            if cp is not None:
                await cp("leased")
        except BaseException:
            await self._abort()
            raise

    async def first(self, method: str, *args: Any) -> Any:
        """The OPTIMIZED executor's FIRST message on this lease.

        It is ``BEGIN READ ONLY; set_config``. When asyncpg refuses it BEFORE
        writing a byte because
        the pooled connection died while idle, the lease is aborted, a fresh
        connection leased and the message sent on it (nothing reached the
        server). Anything else propagates. Returns the driver connection after
        a ``BEGIN`` (``execute``), else the message's result.
        """
        try:
            result = await getattr(self._raw, method)(*args)
        except Exception as error:
            if not (never_sent(error) and recovery_enabled()):
                raise
            await self._abort()
            await self._acquire()
            count_recovery("planner")
            result = await getattr(self._raw, method)(*args)
        return self._raw if method == "execute" else result

    async def __aexit__(self, et: Any, exc: Any, tb: Any) -> bool:
        if et is not None:
            await self._abort()
            if (
                isinstance(exc, Exception)
                and not isinstance(exc, DatabaseUnavailable)
                and is_dead_connection_error(exc)
            ):
                # the optimized executor's raw driver errors: a 503, as on
                # the SQLAlchemy path (sandloom.core.sql_alchemy.dead_connection)
                raise DatabaseUnavailable(exc) from exc
            return False
        if _in_transaction(self.raw):
            await self._abort()
            raise LeaseError("executor left a transaction open")
        conn = self._conn
        assert conn is not None  # noqa: S101 - type narrowing of an invariant
        try:
            await conn.close()
        except BaseException:
            await self._abort()
            raise
        return False

    async def _abort(self) -> None:
        _terminate(self.raw)
        conn = self._conn
        if conn is not None:
            with suppress(Exception):
                await conn.invalidate()
            with suppress(Exception):
                await conn.close()
