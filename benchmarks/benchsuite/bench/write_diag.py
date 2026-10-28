"""Write-path stand — services and the W6 command contract (DIAGNOSTIC ONLY).

Each W-rung adds ONE write-side mechanism to the previous one, all on the
secure S5 stack (JWT + ``TenantScope()`` + ``require(Subscribed(of=Account.user()))`` + a
``TenantScoped`` table under FORCEd RLS, NOBYPASSRLS app role) and with NO
password hashing anywhere (the write-heavy profile's argon2 login is exactly
what this stand removes):

=====  =====================================================================
rung   adds
=====  =====================================================================
W1     one ORM INSERT (``db.add`` + ``flush``) in the request UoW
W2     W1 + ``publish_event`` (an ``outbox_messages`` row, same transaction;
       post-commit dispatch to the bus)
W3     W2 + ``record_audit`` (an ``audit_log`` row, same transaction)
W4o    UPDATE by PK, ORM load-modify-flush (``db.get`` + ``value += 1``)
W4c    UPDATE by PK, one ORM-enabled Core ``UPDATE … RETURNING`` (no load)
W5     a two-table business transaction: order + line INSERTs, outbox,
       audit and an ``on_commit`` callback
W6     W5 through a COMMAND contract (``operation_key``): framework dedupe in
       the provider's durable command log, inside its own UoW (contract pool)
W7     W3 as an ADR-0003 class-F route: ``strict_revocation()`` and a
       durable ``idempotency_guard`` (advisory try-lock + claim in the
       request UoW, response stored before COMMIT; until 2026-10-24 the
       fail-closed cache guard); not in ``writes.RUNGS`` (needs a fresh
       ``Idempotency-Key`` per request — ``ladder/hot_paths.py``)
=====  =====================================================================

The ``fail`` switch of every service raises AFTER all of its writes (inside
the transaction): the atomicity tests prove nothing of it survives.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.di import on_commit, publish_event, record_audit
from sandloom.core.module_api import contract_method, contract_session
from benchsuite.common import EVENT_CHANNEL

from .write_models import (
    BenchTenantCounter,
    BenchTenantOrder,
    BenchTenantOrderLine,
    BenchTenantWrite,
)

SERVICE = "bench.writediag.v1"

#: ``on_commit`` callbacks that ran (W5/W6) — read by the atomicity tests.
ON_COMMIT_RUNS: dict[str, int] = {"count": 0}


def _committed() -> None:
    ON_COMMIT_RUNS["count"] += 1


def _maybe_fail(fail: bool) -> None:
    if fail:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="diag: forced failure")


async def w_insert(
    db: AsyncSession,
    name: str,
    value: int,
    *,
    event: bool = False,
    audit_actor: UUID | None = None,
    fail: bool = False,
) -> int:
    """W1 (insert), W2 (+ event), W3 (+ event + audit)."""
    row = BenchTenantWrite(name=name, value=value)
    db.add(row)
    await db.flush()
    new_id = row.id
    if event:
        await publish_event(EVENT_CHANNEL, {"id": new_id, "name": name})
    if audit_actor is not None:
        record_audit(
            "bench.write.created",
            actor_id=audit_actor,
            resource_type="bench_tenant_write",
            resource_id=str(new_id),
            data={"value": value},
        )
    _maybe_fail(fail)
    return new_id


async def w4_orm(db: AsyncSession, counter_id: int, *, fail: bool = False) -> int:
    """W4o: load the row, modify it, flush (SELECT + UPDATE)."""
    counter = await db.get(BenchTenantCounter, counter_id)
    if counter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
    counter.value += 1
    await db.flush()
    value = counter.value
    _maybe_fail(fail)
    return value


#: WM: rows loaded and attribute assignments per row (200 assignments).
WM_ROWS = 20
WM_ASSIGNMENTS = 10


async def wm_orm(db: AsyncSession, counter_id: int, *, fail: bool = False) -> int:
    """WM: load ``WM_ROWS`` mapped rows (one SELECT), assign ``value``
    ``WM_ASSIGNMENTS`` times on each, flush (the UPDATEs)."""
    from benchsuite.setup_db import WRITE_COUNTERS_PER_TENANT

    start = min(counter_id, WRITE_COUNTERS_PER_TENANT - WM_ROWS + 1)
    rows = (
        await db.scalars(
            select(BenchTenantCounter)
            .where(BenchTenantCounter.id >= start)
            .where(BenchTenantCounter.id < start + WM_ROWS)
            .order_by(BenchTenantCounter.id)
        )
    ).all()
    if len(rows) != WM_ROWS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
    total = 0
    for row in rows:
        for _ in range(WM_ASSIGNMENTS):
            row.value = row.value + 1
        total += row.value
    await db.flush()
    _maybe_fail(fail)
    return total


async def w4_core(db: AsyncSession, counter_id: int, *, fail: bool = False) -> int:
    """W4c: one ``UPDATE … SET value = value + 1 … RETURNING value``."""
    stmt = (
        update(BenchTenantCounter)
        .where(BenchTenantCounter.id == counter_id)
        .values(value=BenchTenantCounter.value + 1)
        .returning(BenchTenantCounter.value)
        .execution_options(synchronize_session=False)
    )
    value = (await db.execute(stmt)).scalar_one_or_none()
    if value is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
    _maybe_fail(fail)
    return int(value)


async def place_order(
    db: AsyncSession, name: str, value: int, actor: Any, *, fail: bool = False
) -> int:
    """W5: order + line + outbox + audit + on_commit, one transaction."""
    order = BenchTenantOrder(name=name, total=value)
    db.add(order)
    await db.flush()  # the line needs the order id
    order_id = order.id
    db.add(BenchTenantOrderLine(order_id=order_id, sku=name, qty=value))
    await db.flush()
    await publish_event(EVENT_CHANNEL, {"id": order_id, "name": name})
    record_audit(
        "bench.order.placed",
        actor_id=actor,
        resource_type="bench_tenant_order",
        resource_id=str(order_id),
        data={"total": value},
    )
    await on_commit(_committed)
    _maybe_fail(fail)
    return order_id


class WriteDiagContract:
    """``bench.writediag.v1`` — W6: W5 as a provider-owned COMMAND."""

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def place_order(
        cls, name: str, value: int, actor_id: UUID | None, operation_key: str
    ) -> int:
        return await place_order(contract_session(), name, value, actor_id)

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def place_order_failing(
        cls, name: str, value: int, actor_id: UUID | None, operation_key: str
    ) -> int:
        """W6 atomicity probe: fails after every write of ``place_order``."""
        return await place_order(contract_session(), name, value, actor_id, fail=True)


__all__ = [
    "ON_COMMIT_RUNS",
    "SERVICE",
    "WriteDiagContract",
    "place_order",
    "w4_core",
    "w4_orm",
    "w_insert",
]
