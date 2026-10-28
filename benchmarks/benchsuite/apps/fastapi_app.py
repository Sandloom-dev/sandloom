"""Bare FastAPI (same FastAPI/Starlette/pydantic versions as Sandloom's lock).

Idiomatic FastAPI defaults: pydantic response models, a session dependency,
``HTTPBearer`` for the token. No middleware.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from benchsuite.bare.auth import AuthError, authenticate, make_redis
from benchsuite.bare.db import Item, SessionLocal, Write, engine, outbox_row

_redis = make_redis()
_bearer = HTTPBearer(auto_error=False)


class ItemOut(BaseModel):
    id: int
    name: str
    value: int


class WriteIn(BaseModel):
    name: str
    value: int


class WriteOut(BaseModel):
    id: int


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await _redis.aclose()
    await engine.dispose()


app = FastAPI(lifespan=lifespan)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    header = f"Bearer {credentials.credentials}" if credentials else None
    try:
        return await authenticate(header, _redis)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@app.get("/s1")
async def s1() -> dict[str, bool]:
    return {"ok": True}


@app.get("/s2")
async def s2(user_id: str = Depends(current_user)) -> dict[str, object]:
    return {"ok": True, "user_id": user_id}


@app.get("/s3/{item_id}", response_model=ItemOut)
async def s3(item_id: int, session: AsyncSession = Depends(get_session)) -> ItemOut:
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not found")
    return ItemOut(id=item.id, name=item.name, value=item.value)


@app.post("/s4", status_code=status.HTTP_201_CREATED, response_model=WriteOut)
async def s4(body: WriteIn, session: AsyncSession = Depends(get_session)) -> WriteOut:
    async with session.begin():
        row = Write(name=body.name, value=body.value)
        session.add(row)
        await session.flush()
        new_id = row.id
        session.add(outbox_row({"id": new_id, "name": body.name}))
    return WriteOut(id=new_id)
