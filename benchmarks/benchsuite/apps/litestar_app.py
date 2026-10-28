"""Bare Litestar (msgspec) with the SAME SQLAlchemy access as bare FastAPI.

Keeping the DB layer identical makes Litestar-vs-FastAPI a comparison of the
HTTP/serialization layer only. See ``litestar_asyncpg_app`` for a raw-asyncpg
variant.
"""

from __future__ import annotations

import msgspec
from litestar import Litestar, Request, get, post
from litestar.exceptions import NotAuthorizedException, NotFoundException

from benchsuite.bare.auth import AuthError, authenticate, make_redis
from benchsuite.bare.db import Item, SessionLocal, Write, engine, outbox_row

_redis = make_redis()


class ItemOut(msgspec.Struct):
    id: int
    name: str
    value: int


class WriteIn(msgspec.Struct):
    name: str
    value: int


class WriteOut(msgspec.Struct):
    id: int


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
    async with SessionLocal() as session:
        item = await session.get(Item, item_id)
        if item is None:
            raise NotFoundException(detail="not found")
        return ItemOut(id=item.id, name=item.name, value=item.value)


@post("/s4", status_code=201)
async def s4(data: WriteIn) -> WriteOut:
    async with SessionLocal() as session, session.begin():
        row = Write(name=data.name, value=data.value)
        session.add(row)
        await session.flush()
        new_id = row.id
        session.add(outbox_row({"id": new_id, "name": data.name}))
    return WriteOut(id=new_id)


async def _shutdown() -> None:
    await _redis.aclose()
    await engine.dispose()


app = Litestar(route_handlers=[s1, s2, s3, s4], on_shutdown=[_shutdown])
