"""SQLAlchemy implementations of the OutboxStore / AuditStore contracts.

This is the ONLY place that knows the relational outbox/audit ORM models on the
write path. The public ``publish_event`` / ``record_audit`` adapters in
``sandloom.core.di`` go through the abstract :class:`OutboxStore` / :class:`AuditStore`
contracts and never import these models — that is what makes the persistence
layer backend-agnostic (a document/graph backend would ship its own store).

Both stores are stateless, so a single shared instance is reused per process.
"""

from __future__ import annotations

import uuid
import weakref
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, bindparam, delete, func, or_, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

import sandloom.core.sql_alchemy.uow as _uow
from sandloom.core.contracts.uow import UnitOfWorkContract
from sandloom.core.infra.contracts import (
    OutboxHandle,
)
from sandloom.core.observability.tracing import get_current_trace_id
from sandloom.core.sql_alchemy import effect_rows
from sandloom.core.sql_alchemy.driver_compat import session_connections_ok
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage, _utc_naive_now
from sandloom.core.sql_alchemy.outbox_store.audit import SQLAlchemyAuditStore
from sandloom.core.sql_alchemy.outbox_store.relay import _RelayMixin
from sandloom.core.sql_alchemy.outbox_store.statements import (
    _any_id,
    _id_in,
    _is_postgres,
    _naive_utc,
    _system_uow,
)
from sandloom.core.sql_alchemy.tenant_rls import non_tenant_statements

#: The PostgreSQL mark-delivered UPDATE, prebuilt (ids and timestamp bound
#: per call): it runs after EVERY committed publish, and building an
#: ORM-enabled UPDATE each time cost more than the statement's own round trip.
_mark_delivered_pg: Any = None


def _mark_delivered_statement() -> Any:
    global _mark_delivered_pg
    stmt = _mark_delivered_pg
    if stmt is None:
        table: Any = OutboxMessage.__table__
        stmt = _mark_delivered_pg = (
            update(table)
            .where(
                _any_id(table, ()),
                # Never resurrect a dead-lettered row (see mark_delivered).
                table.c.dead_at.is_(None),
                table.c.delivered_at.is_(None),
                # No WAL flush wait for THIS transaction (``SET LOCAL
                # synchronous_commit = off``, evaluated with the matched
                # rows): a delivered mark lost to a crash only leaves the row
                # to the relay — a redelivery the at-least-once contract
                # already allows (stable envelope ids) — while every request
                # COMMIT stays fully synchronous. Saves one fsync per
                # dispatch round and returns its pooled connection sooner.
                text("set_config('synchronous_commit', 'off', true) IS NOT NULL"),
            )
            .values(delivered_at=bindparam("delivered_at"), last_error=None)
        )
    return stmt


async def _autocommit_connection(session: Any) -> None:
    """Procure ``session``'s connection in AUTOCOMMIT when it owns it.

    Then one statement is one round trip: no BEGIN / COMMIT. Only for a
    session bound to an ENGINE that has not procured a connection yet (a fresh
    UoW), and only while the driver-internals probe confirms SQLAlchemy's
    ``SessionTransaction._connections``. A session joined to an outer
    connection / transaction (test harnesses, ``bind_test_uow``) or one
    already holding a connection keeps its transaction: its statement must
    stay inside it.
    """

    if not session_connections_ok():
        # SQLAlchemy's private ``_connections`` changed (probe: one warning +
        # metric): stay transactional — BEGIN / UPDATE / COMMIT, same result.
        return
    sync = getattr(session, "sync_session", session)
    try:
        bind = sync.get_bind()
    except Exception:  # noqa: BLE001 - unknown bind: keep the transaction
        return
    tx = sync.get_transaction()
    if not isinstance(bind, Engine) or tx is None or tx.nested:
        return
    if bind in tx._connections:
        return
    await session.connection(execution_options={"isolation_level": "AUTOCOMMIT"})


#: Per UoW factory: an ``isolation_level="AUTOCOMMIT"`` view of the engine its
#: sessions are bound to (``_NO_VIEW`` when the factory does not qualify).
#: Built ONCE per factory — ``execution_options()`` creates an option engine,
#: and building one per mark cost more than it saved. The view shares the
#: parent engine's pool, dialect and event listeners (the ``tenant_rls``
#: hooks included), so a dispose / post-fork pool reset of the parent applies
#: to it too. Weak keys: a dropped factory (a test's) drops its view.
_AUTOCOMMIT_VIEWS: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
_NO_VIEW = object()


def _autocommit_view(uow_factory: Callable[[], UnitOfWorkContract]) -> Any:
    """The cached AUTOCOMMIT engine view for ``uow_factory``, or ``None``.

    Only for a factory producing :class:`SQLAlchemyUnitOfWork` sessions bound
    to a PostgreSQL ``AsyncEngine``. A factory whose sessions are bound to a
    CONNECTION (a test harness joined to an outer transaction — its mark must
    stay inside that transaction), to several binds, or to another dialect
    keeps the session path.
    """
    try:
        view = _AUTOCOMMIT_VIEWS.get(uow_factory)
    except TypeError:  # not weak-referenceable: no cache, no view
        return None
    if view is None:
        view = _build_autocommit_view(uow_factory)
        _AUTOCOMMIT_VIEWS[uow_factory] = view
    return None if view is _NO_VIEW else view


def _build_autocommit_view(uow_factory: Callable[[], UnitOfWorkContract]) -> Any:
    try:
        uow = uow_factory()  # constructing it opens nothing
    except Exception:  # noqa: BLE001 - keep the session path
        return _NO_VIEW
    if not isinstance(uow, _uow.SQLAlchemyUnitOfWork):
        return _NO_VIEW
    kw = getattr(uow._session_factory, "kw", None) or {}
    bind = kw.get("bind")
    if kw.get("binds") or not isinstance(bind, AsyncEngine):
        return _NO_VIEW
    if bind.dialect.name != "postgresql":
        return _NO_VIEW
    return bind.execution_options(isolation_level="AUTOCOMMIT")


class SQLAlchemyOutboxStore(_RelayMixin):
    """Transactional outbox over the ``outbox_messages`` table."""

    def enqueue(
        self,
        session: Any,
        channel: str,
        payload: Mapping[str, Any],
        schema_version: str,
    ) -> OutboxHandle:
        # A plain row, written by the UoW's next flush together with its
        # audit rows: ONE round trip on PostgreSQL (sandloom.core.sql_alchemy.
        # effect_rows). ``publish_event`` hands over its own payload
        # snapshot; a direct caller's mapping is copied (top level).
        row = effect_rows.OutboxRow(
            uuid.uuid4(),
            channel,
            schema_version,
            payload if type(payload) is dict else dict(payload),
            get_current_trace_id(),
            _utc_naive_now(),
        )
        effect_rows.register(session, row)
        return OutboxHandle(
            id=row.id, raw=row, trace_id=row.trace_id, emitted_at=row.emitted_at
        )

    def survived(self, handle: OutboxHandle) -> bool:
        """True if the outbox row was written and not undone by a SAVEPOINT.

        A row registered or written inside a ``begin_nested()`` that rolled
        back, or never written by a flush, is not dispatched (the relay is the
        durable backstop for everything that did commit).
        """
        return effect_rows.survived(handle.raw)

    async def mark_delivered(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        ids: Sequence[uuid.UUID],
    ) -> None:
        if not ids:
            return

        # Reopen on the SAME database the events were written to, so multi-DB
        # outboxes are marked delivered in their own database.
        #
        # ONE autocommit statement (no BEGIN / COMMIT; an allowlisted system
        # statement, so no RLS tenant setting either): the UPDATE is atomic on
        # its own, ``outbox_messages`` is not tenant-scoped, and the guards
        # below make it idempotent. A crash before it runs only leaves the rows
        # to the relay (at-least-once, stable event ids).
        view = _autocommit_view(uow_factory)
        if view is not None:
            # The same statement on a pooled Core connection of the cached
            # AUTOCOMMIT view: no AsyncSession / UoW wrapper per mark (the
            # whole saving — the round trips are identical).
            with non_tenant_statements():
                async with view.connect() as conn:
                    await conn.execute(
                        _mark_delivered_statement(),
                        {"outbox_ids": list(ids), "delivered_at": _utc_naive_now()},
                    )
            return
        async with _system_uow(uow_factory) as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            session = uow.session
            await _autocommit_connection(session)
            if _is_postgres(session):
                await session.execute(
                    _mark_delivered_statement(),
                    {"outbox_ids": list(ids), "delivered_at": _utc_naive_now()},
                )
                return
            await session.execute(
                update(OutboxMessage)
                .where(
                    _id_in(session, ids),
                    # Never resurrect a dead-lettered row: a slow post-commit
                    # dispatch must not clear a row the relay already gave up
                    # on (which would leave dead_at + delivered_at both set and
                    # contradict the dead-letter metric/alert).
                    OutboxMessage.dead_at.is_(None),
                    OutboxMessage.delivered_at.is_(None),
                )
                .values(delivered_at=_utc_naive_now(), last_error=None)
                .execution_options(synchronize_session=False)
            )

    # -- relay side ----------------------------------------------------------
    async def fetch_pending_ids(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        *,
        max_attempts: int,
        grace_seconds: float,
        limit: int,
    ) -> Sequence[uuid.UUID]:
        # Grace leaves the post-commit dispatcher time to deliver its own rows,
        # narrowing the duplicate-delivery window. ``next_attempt_at`` defers a
        # row that failed (backoff) or was parked during a broker outage.
        now = datetime.now(UTC).replace(tzinfo=None)
        grace_cutoff = now - timedelta(seconds=grace_seconds)
        async with _system_uow(uow_factory) as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            # A plain read — deliberately NO row lock. Locks taken here would be
            # released when this short transaction commits, i.e. before any
            # delivery happens, so they could not partition work between relay
            # instances anyway. Exclusivity comes from the per-row lease claim
            # in deliver_pending: a row whose lease is in the future is skipped
            # here, and a row claimed concurrently is a cheap "skipped" there.
            result = await uow.session.execute(
                select(OutboxMessage.id)
                .where(
                    OutboxMessage.delivered_at.is_(None),
                    OutboxMessage.dead_at.is_(None),
                    OutboxMessage.delivery_attempts < max_attempts,
                    OutboxMessage.emitted_at < grace_cutoff,
                    or_(
                        OutboxMessage.next_attempt_at.is_(None),
                        OutboxMessage.next_attempt_at <= now,
                    ),
                    or_(
                        OutboxMessage.locked_until.is_(None),
                        OutboxMessage.locked_until < now,
                    ),
                )
                .order_by(OutboxMessage.emitted_at)
                .limit(limit)
            )
            return [row[0] for row in result.all()]

    async def dead_letter_exhausted(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        *,
        max_attempts: int,
    ) -> int:
        """Dead-letter undelivered rows already at/over the attempt budget.

        Happens when OUTBOX_RELAY_MAX_ATTEMPTS is lowered below the attempts
        some rows already accumulated: they drop out of the pending query but
        were never marked dead — invisible to the dead-letter metric and to
        requeue. Leased (in-flight) rows are left to their owner's settle.
        """

        now = _utc_naive_now()
        async with _system_uow(uow_factory) as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            result = await uow.session.execute(
                update(OutboxMessage)
                .where(
                    OutboxMessage.delivered_at.is_(None),
                    OutboxMessage.dead_at.is_(None),
                    OutboxMessage.delivery_attempts >= max_attempts,
                    or_(
                        OutboxMessage.locked_until.is_(None),
                        OutboxMessage.locked_until < now,
                    ),
                )
                .values(dead_at=now, locked_until=None, locked_by=None)
                .execution_options(synchronize_session=False)
            )
            return int(getattr(result, "rowcount", 0) or 0)

    async def list_dead(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        *,
        channel: str | None = None,
        limit: int = 100,
        after: tuple[datetime, uuid.UUID] | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        async with uow_factory() as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            stmt = select(
                OutboxMessage.id,
                OutboxMessage.channel,
                OutboxMessage.delivery_attempts,
                OutboxMessage.last_error,
                OutboxMessage.dead_at,
                OutboxMessage.emitted_at,
            ).where(OutboxMessage.dead_at.is_not(None))
            if channel is not None:
                stmt = stmt.where(OutboxMessage.channel == channel)
            if after is not None:
                at, row_id = after
                at = _naive_utc(at)
                stmt = stmt.where(
                    or_(
                        OutboxMessage.dead_at > at,
                        and_(OutboxMessage.dead_at == at, OutboxMessage.id > row_id),
                    )
                )
            rows = (
                await uow.session.execute(
                    stmt.order_by(OutboxMessage.dead_at, OutboxMessage.id).limit(limit)
                )
            ).all()
            return [
                {
                    "id": row.id,
                    "channel": row.channel,
                    "attempts": row.delivery_attempts,
                    "last_error": row.last_error,
                    "dead_at": row.dead_at,
                    "emitted_at": row.emitted_at,
                }
                for row in rows
            ]

    async def list_channel_since(
        self,
        session: Any,
        *,
        channel: str,
        since: datetime,
        limit: int = 1000,
        after: tuple[datetime, uuid.UUID] | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        stmt = select(
            OutboxMessage.id, OutboxMessage.payload, OutboxMessage.emitted_at
        ).where(
            OutboxMessage.channel == channel,
            OutboxMessage.emitted_at >= _naive_utc(since),
        )
        if after is not None:
            at, row_id = _naive_utc(after[0]), after[1]
            stmt = stmt.where(
                or_(
                    OutboxMessage.emitted_at > at,
                    and_(OutboxMessage.emitted_at == at, OutboxMessage.id > row_id),
                )
            )
        rows = (
            await session.execute(
                stmt.order_by(OutboxMessage.emitted_at, OutboxMessage.id).limit(limit)
            )
        ).all()
        return [
            {"id": row.id, "payload": row.payload, "emitted_at": row.emitted_at}
            for row in rows
        ]

    async def requeue_dead(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        *,
        ids: Sequence[uuid.UUID] | None = None,
        channel: str | None = None,
        limit: int = 1000,
    ) -> int:
        """Move dead-lettered rows back to pending with a fresh attempt budget.

        The envelope id is unchanged, so consumers that already applied the
        event (e.g. a delivery that succeeded but whose settle was lost) still
        dedupe the redelivery. ``last_error`` is kept for forensics.
        """

        if ids is not None and not ids:
            return 0
        async with uow_factory() as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            target = select(OutboxMessage.id).where(
                OutboxMessage.dead_at.is_not(None),
                OutboxMessage.delivered_at.is_(None),
            )
            if ids is not None:
                target = target.where(OutboxMessage.id.in_(list(ids)))
            if channel is not None:
                target = target.where(OutboxMessage.channel == channel)
            target = target.order_by(OutboxMessage.dead_at).limit(limit)
            result = await uow.session.execute(
                update(OutboxMessage)
                .where(OutboxMessage.id.in_(target.scalar_subquery()))
                .values(
                    dead_at=None,
                    delivery_attempts=0,
                    next_attempt_at=None,
                    locked_until=None,
                    locked_by=None,
                )
                .execution_options(synchronize_session=False)
            )
            return int(getattr(result, "rowcount", 0) or 0)

    async def oldest_pending_age_seconds(
        self, uow_factory: Callable[[], UnitOfWorkContract]
    ) -> float | None:
        """``min(emitted_at)`` of the pending rows: one relay-index probe.

        The partial ``ix_outbox_messages_relay`` index (``delivered_at IS NULL
        AND dead_at IS NULL``,
        ordered by ``emitted_at``) answers it with its first entry — no scan,
        whatever the backlog. Rows in retry backoff count: they are undelivered.
        """

        table: Any = OutboxMessage.__table__
        statement = select(func.min(table.c.emitted_at)).where(
            table.c.delivered_at.is_(None), table.c.dead_at.is_(None)
        )
        async with uow_factory() as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            oldest = (await uow.session.execute(statement)).scalar_one_or_none()
        if oldest is None:
            return 0.0
        return max(0.0, (_utc_naive_now() - _naive_utc(oldest)).total_seconds())

    async def purge_delivered(
        self,
        uow_factory: Callable[[], UnitOfWorkContract],
        *,
        older_than_seconds: float,
        limit: int,
    ) -> int:
        """``DELETE`` up to ``limit`` rows delivered before ``now - older_than``.

        Batched (``id IN (SELECT … LIMIT n)``) so one call holds row locks only
        briefly; served by the partial ``ix_outbox_messages_delivered`` index.
        Dead-lettered and undelivered rows are never touched.
        """

        if limit <= 0:
            return 0
        table: Any = OutboxMessage.__table__
        cutoff = _utc_naive_now() - timedelta(seconds=older_than_seconds)
        victims = (
            select(table.c.id)
            .where(table.c.delivered_at.is_not(None), table.c.delivered_at < cutoff)
            .limit(limit)
            .scalar_subquery()
        )
        async with uow_factory() as uow:
            assert uow.session is not None  # noqa: S101 - type narrowing of an invariant
            result = await uow.session.execute(
                delete(table).where(table.c.id.in_(victims))
            )
            return int(getattr(result, "rowcount", 0) or 0)


_OUTBOX_STORE = SQLAlchemyOutboxStore()
_AUDIT_STORE = SQLAlchemyAuditStore()


def sql_alchemy_outbox_store() -> SQLAlchemyOutboxStore:
    """Return the shared (stateless) SQLAlchemy outbox store."""
    return _OUTBOX_STORE


def sql_alchemy_audit_store() -> SQLAlchemyAuditStore:
    """Return the shared (stateless) SQLAlchemy audit store."""
    return _AUDIT_STORE


__all__ = [
    "SQLAlchemyOutboxStore",
    "sql_alchemy_outbox_store",
    "sql_alchemy_audit_store",
]
