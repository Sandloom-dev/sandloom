"""Bare Litestar + RAW asyncpg (no SQLAlchemy) — a clearly-labelled ceiling.

Same HTTP layer and S1/S2 as ``litestar_app``; S3/S4 skip the ORM and talk to
asyncpg directly (one pool sized like the others: pool_size + max_overflow).
Reported as ``litestar-asyncpg`` — NOT an apples-to-apples DB comparison with
the SQLAlchemy-based implementations; it shows what the ORM layer costs.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import asyncpg
import msgspec
from litestar import Litestar, Request, get, post
from litestar.exceptions import NotAuthorizedException, NotFoundException

from benchsuite import common
from benchsuite.bare.auth import AuthError, authenticate, make_redis

_redis = make_redis()
_pool: asyncpg.Pool | None = None


class ItemOut(msgspec.Struct):
    id: int
    name: str
    value: int


class WriteIn(msgspec.Struct):
    name: str
    value: int


class WriteOut(msgspec.Struct):
    id: int


def pool() -> asyncpg.Pool:
    assert _pool is not None, "pool not initialised (startup did not run)"
    return _pool


_SELECT = "SELECT id, name, value FROM bench_items WHERE id = $1"
_INSERT_WRITE = "INSERT INTO bench_writes (name, value) VALUES ($1, $2) RETURNING id"
_INSERT_OUTBOX = (
    "INSERT INTO bare_outbox_messages "
    "(id, channel, schema_version, payload, emitted_at, delivery_attempts) "
    "VALUES ($1, $2, 'v1', $3, $4, 0)"
)


@get("/s1")
async def s1() -> dict[str, bool]:
    return {"ok": True}


@get("/s2")
async def s2(request: Request) -> dict[str, object]:
    try:
        user_id = await authenticate(request.headers.get("authorization"), _redis)
    except AuthError as exc:
        raise NotAuthorizedException(detail=str(exc)) from exc
    return {"ok": True, "user_id": user_id}


@get("/s3/{item_id:int}")
async def s3(item_id: int) -> ItemOut:
    row = await pool().fetchrow(_SELECT, item_id)
    if row is None:
        raise NotFoundException(detail="not found")
    return ItemOut(id=row["id"], name=row["name"], value=row["value"])


@post("/s4", status_code=201)
async def s4(data: WriteIn) -> WriteOut:
    async with pool().acquire() as conn, conn.transaction():
        new_id = await conn.fetchval(_INSERT_WRITE, data.name, data.value)
        await conn.execute(
            _INSERT_OUTBOX,
            uuid.uuid4(),
            common.EVENT_CHANNEL,
            json.dumps({"id": new_id, "name": data.name}),
            datetime.now(UTC).replace(tzinfo=None),
        )
    return WriteOut(id=new_id)


async def _startup() -> None:
    global _pool
    _pool = await asyncpg.create_pool(
        common.asyncpg_dsn(),
        min_size=1,
        max_size=common.pool_size() + common.max_overflow(),
    )


async def _shutdown() -> None:
    await _redis.aclose()
    if _pool is not None:
        await _pool.close()


app = Litestar(
    route_handlers=[s1, s2, s3, s4], on_startup=[_startup], on_shutdown=[_shutdown]
)
