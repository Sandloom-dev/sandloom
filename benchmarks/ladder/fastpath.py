"""Read fast-path PROTOTYPES mounted on the real Sandloom benchmark app.

NOT framework code: three alternative implementations of the S3 read, added
to the very same Sandloom application (same middleware stack, same lifespan) so
their full-HTTP cost is directly comparable with ``GET /s3/{id}``:

``/ro/{id}``        the S3 handler verbatim with ``db: ReadOnlyDbSession`` (the
                    declared read-only policy; DB_READ_ONLY_SESSIONS=true).
``/fast/uow/{id}``  F1 — keeps Sandloom's request UoW (``DbSession``: BEGIN/COMMIT,
                    tenant scope, post-commit hooks) but selects COLUMNS (no ORM
                    instance / identity map) and returns ``orjson`` bytes in a
                    plain ``Response`` (no pydantic model, no ``response_model``
                    re-validation, no ``jsonable_encoder``).
``/fast/core/{id}`` F2 — no Session and no transaction: SQLAlchemy Core on an
                    ``AUTOCOMMIT`` connection of Sandloom's own default engine, one
                    prebuilt statement, Row -> ``orjson``. One round trip.
``/fast/core2/{id}`` F2b — F2 on a DEDICATED engine created AUTOCOMMIT (F2
                    switches the isolation level on every checkout/checkin).
``/fast/pg/{id}``   F3 — ceiling: the raw asyncpg connection underneath Sandloom's
                    pool (``driver_connection``) -> ``fetchrow`` -> ``orjson``.

Tenant note: F2/F3 bypass the ORM ``do_orm_execute`` tenant guard; a framework
version must add ``tenant_filter(Model)`` (Core) / an explicit ``tenant_id``
predicate itself and refuse TenantScoped tables without one.
"""

from __future__ import annotations

from typing import Any

import orjson
from fastapi import HTTPException, Response
from sqlalchemy import bindparam, select

from sandloom.core.di import DbSession, ReadOnlyDbSession
from benchsuite.apps.sandloom_app import app
from benchsuite.bench import services
from benchsuite.bench.api import ItemOut
from benchsuite.bench.models import BenchItem

_T = BenchItem.__table__
_STMT = select(_T.c.id, _T.c.name, _T.c.value).where(_T.c.id == bindparam("id"))
_SQL = "SELECT id, name, value FROM bench_items WHERE id = $1"
_ENGINE: dict[str, Any] = {}


def _engine() -> Any:
    eng = _ENGINE.get("ac")
    if eng is None:
        from sandloom.core.runtime import get_active_core

        base = get_active_core().database("default").engine
        eng = _ENGINE["ac"] = base.execution_options(isolation_level="AUTOCOMMIT")
    return eng


def _json(row: Any) -> Response:
    if row is None:
        raise HTTPException(404, detail="not found")
    return Response(
        orjson.dumps({"id": row[0], "name": row[1], "value": row[2]}),
        media_type="application/json",
    )


@app.get("/ro/{item_id}", response_model=ItemOut)
async def s3_read_only(item_id: int, db: ReadOnlyDbSession) -> ItemOut:
    """The bench S3 handler VERBATIM, only the session DECLARED read-only."""
    item = await services.get_item(db, item_id)
    if item is None:
        raise HTTPException(404, detail="not found")
    return ItemOut(id=item.id, name=item.name, value=item.value)


@app.get("/fast/uow/{item_id}")
async def fast_uow(item_id: int, db: DbSession) -> Response:
    row = (await db.execute(_STMT, {"id": item_id})).first()
    return _json(row)


@app.get("/fast/core/{item_id}")
async def fast_core(item_id: int) -> Response:
    async with _engine().connect() as conn:
        row = (await conn.execute(_STMT, {"id": item_id})).first()
    return _json(row)


def _dedicated_engine() -> Any:
    """A separate AUTOCOMMIT engine (same URL / pool shape as Sandloom's): no
    per-checkout isolation-level switch, unlike ``execution_options``."""
    eng = _ENGINE.get("ded")
    if eng is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        from sandloom.core.drivers.linger_pool import LingeringQueuePool
        from sandloom.core.runtime import get_active_core
        from sandloom.core.config.settings import settings

        # Same pool shape AND class as Sandloom's request pool (LingeringQueuePool:
        # a stock QueuePool churns overflow connections at conc > pool_size).
        url = get_active_core().database("default").engine.url
        eng = _ENGINE["ded"] = create_async_engine(
            url,
            pool_pre_ping=False,
            pool_use_lifo=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            poolclass=LingeringQueuePool,
            overflow_linger_seconds=settings.db_pool_overflow_linger_seconds,
            isolation_level="AUTOCOMMIT",
        )
    return eng


@app.get("/fast/core2/{item_id}")
async def fast_core2(item_id: int) -> Response:
    async with _dedicated_engine().connect() as conn:
        row = (await conn.execute(_STMT, {"id": item_id})).first()
    return _json(row)


@app.get("/fast/pg/{item_id}")
async def fast_pg(item_id: int) -> Response:
    async with _dedicated_engine().connect() as conn:
        raw = await conn.get_raw_connection()
        row = await raw.driver_connection.fetchrow(_SQL, item_id)
    return _json(row)


async def dispose() -> None:
    """Close the prototype's dedicated engine (the ladder calls it on teardown)."""
    eng = _ENGINE.pop("ded", None)
    if eng is not None:
        await eng.dispose()


__all__ = ["app", "dispose"]
