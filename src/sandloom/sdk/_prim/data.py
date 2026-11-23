"""New L2 data primitives of ``sandloom.sdk.data`` (spec §2.3, §4.3).

``db()`` is the session of the running unit, ``open_uow()`` opens a unit
(today ``db_session``), ``MoneyLedger`` names a money ledger's unique
operation-key column (ADR-0009). All three are the kernel's (H1, WS-A).
``conflict_insert``, ``tenant_upsert`` and ``transaction_lock`` run on ``db()``
unless given a session (the portable SQL forms, SANDLOOM-C028; ``tenant_upsert``
is the upsert of a ``TenantScoped`` model, ADR-0018).
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.di import db as _kernel_db
from sandloom.core.di import db_session
from sandloom.core.orm import ConflictInsert, MoneyLedger, Page, TenantScoped
from sandloom.core.orm import conflict_insert as _conflict_insert
from sandloom.core.orm import tenant_upsert as _tenant_upsert
from sandloom.core.orm import transaction_lock as _transaction_lock
from sandloom.sdk._unit import open_uow_for_module


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

    Without ``database`` the unit opens on the primary database of the module
    that calls ``open_uow`` (the one its migration chain lives on, as its
    contracts' units do). Code that belongs to no loaded module, such as a
    script, gets ``"default"``; so does a module whose ``define_module(name=)``
    differs from its package: pass the database name there.

    A module that requires several databases, ``default`` not among them, and
    declares no ``migrations_database`` has no primary one: ``open_uow()``
    then raises ``MigrationTargetError`` on entering the block; pass
    ``database``. A module that requires ``default`` and another database gets
    ``default``.

    The database of a call without ``database`` is resolved when the block is
    entered, against the core active then (as ``db_session`` does), not when
    ``open_uow()`` is called.
    """
    if database is None:
        return open_uow_for_module(sys._getframe(1).f_globals.get("__name__"))
    return db_session(database)


__all__ = ["MoneyLedger", "Page", "TenantScoped", "db", "open_uow"]


def conflict_insert(
    table: Any, *, session: AsyncSession | None = None
) -> ConflictInsert:
    """``INSERT ... ON CONFLICT`` for ``table`` (a model class or a table)::

        stmt = conflict_insert(Counter).values(key=k, n=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Counter.key], set_={"n": Counter.n + 1}
        )
        await db().execute(stmt)

    The database of ``session`` (default: :func:`db`) writes it; one without
    ``ON CONFLICT`` refuses (SANDLOOM-R015).
    """
    return _conflict_insert(table, session=session if session is not None else db())


async def tenant_upsert(
    model: type[TenantScoped],
    rows: Sequence[Mapping[str, Any]],
    *,
    update: Sequence[str],
    conflict_on: Sequence[str] | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Insert ``rows`` of a ``TenantScoped`` model for the bound tenant and, where a
    row of that tenant already exists, refresh the ``update`` columns from the
    proposed row::

        # Plan has UniqueConstraint("tenant_id", "code")
        await tenant_upsert(
            Plan,
            [{"id": new_id(), "code": "pro", "price": 20}],
            update=["price"],
        )

    ``rows`` are mappings of attribute name to a plain Python value. A SQL
    expression is refused, and so is the tenant column: a row always takes the
    bound tenant (none bound: ``MissingTenantError``). Every row names the same
    columns, the conflict key and the ``update`` columns among them. ``update``
    lists attributes of the model other than the tenant column and the primary
    key.

    The row conflicts on a unique key that includes the tenant column: the
    table's only one, else the one ``conflict_on=[...]`` names (the tenant column
    may be left out). A row of another tenant is never the conflicting row, so it
    is never updated or chosen by the tenant-inclusive key. Every other key acts
    as on a plain insert: a primary key or another unique key that a row of
    another tenant holds is the database's integrity error, which shows that the
    key exists (PostgreSQL runs that check outside row-level security, so a
    policy does not hide it), so do not reuse a key across tenants. A foreign key
    from a tenant table to another tenant table must be composite,
    ``(tenant_id, parent_id)``: a model that does not pair the tenant on both
    sides is refused when it is defined (SANDLOOM-R020), as are a ``REPLACE``
    conflict clause (SANDLOOM-R019) and a tenant column that is not one plain
    mapped ``Uuid`` column (SANDLOOM-R021). What the helper refuses, with
    ``ValueError`` (SANDLOOM-R018) before anything is sent: a model without such
    a unique key, a model in an
    inheritance hierarchy, one whose tenant column is not named ``tenant_id``, one
    with a column that is not exactly ``sqlalchemy.Column``, one that declares a
    ``<dialect>_on_conflict*`` option of any value on a column, constraint or
    index (the declared options are read: a database schema built another way is
    not), one whose statement would read a tenant table (a column default or type
    ``bind_expression`` that selects from one), and a row or an ``update`` /
    ``conflict_on`` column outside the rules above. A database without ``ON
    CONFLICT``, or whose statement the tenant guard cannot read, refuses
    (SANDLOOM-R015).

    Pending changes of ``session`` (default: :func:`db`) are flushed first, and
    the call returns nothing. Instances of the upserted rows that the session
    already holds keep their old values until refreshed. The rows go to the
    driver as the parameter sets of one ``executemany``. A driver that runs the
    command once per set (asyncpg, aiosqlite) updates the row an earlier row of
    the batch inserted when a later row repeats its key; a driver that batches
    the sets into one multi-row statement (psycopg2) hands PostgreSQL the same
    key twice in one ``ON CONFLICT DO UPDATE``, which it refuses (cardinality
    violation: the command cannot affect a row a second time). A batch that
    names one key twice therefore ends differently per driver: the caller's to
    avoid.
    """
    await _tenant_upsert(
        model,
        rows,
        update=update,
        conflict_on=conflict_on,
        session=session if session is not None else db(),
    )


async def transaction_lock(key: int, *, session: AsyncSession | None = None) -> None:
    """Hold the lock ``key`` until the transaction of ``session`` ends::

        await transaction_lock(ADMIN_LOCK_KEY)   # the section runs one at a time

    ``key`` is a signed 64-bit integer the module chooses and keeps stable; the
    commit or the rollback releases it. ``session`` defaults to :func:`db`. A
    database without such a lock refuses (SANDLOOM-R015). It needs the database
    feature ``transaction_locks``: declare it in ``define_module`` with
    ``CapabilityRequirement("database", features=frozenset({"transaction_locks"}))``
    (SANDLOOM-C030).
    """
    await _transaction_lock(key, session=session if session is not None else db())
