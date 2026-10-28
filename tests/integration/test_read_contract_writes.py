"""A READ contract that writes fails loudly (real PostgreSQL).

A read contract's Unit of Work is always rolled back. Before, only a
published event was refused: an ORM ``add`` / change / ``delete``, a Core DML
statement, a ``record_audit(required=True)`` row or a queued ``on_commit``
was silently discarded while the call returned success (GLM review P1).
Every such write now raises :class:`ReadContractWriteError` — on the per-call
UoW and on the request read scope's shared transaction alike — and nothing
is persisted.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import String, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core.di import on_commit, record_audit
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import (
    ReadContractWriteError,
    contract_method,
    contract_read_scope,
    contract_session,
)
from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.core.testing.infra import override_instance

pytestmark = pytest.mark.asyncio

SERVICE = "demo.read_guard.v1"


class _Base(DeclarativeBase):
    pass


class Note(_Base):
    __tablename__ = "read_guard_demo_notes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    body: Mapped[str] = mapped_column(String(50))


class Notes:
    @classmethod
    @contract_method
    async def count(cls) -> int:
        db = contract_session()
        return len((await db.execute(select(Note.id))).all())

    @classmethod
    @contract_method
    async def add_unflushed(cls) -> int:
        contract_session().add(Note(body="lost"))
        return 1

    @classmethod
    @contract_method
    async def add_flushed(cls) -> int:
        db = contract_session()
        db.add(Note(body="lost"))
        await db.flush()
        return 1

    @classmethod
    @contract_method
    async def add_swallowed(cls) -> int:
        db = contract_session()
        db.add(Note(body="lost"))
        try:
            await db.flush()
        except ReadContractWriteError:
            pass
        return 1

    @classmethod
    @contract_method
    async def modify(cls) -> int:
        db = contract_session()
        note = (await db.execute(select(Note))).scalars().first()
        assert note is not None
        note.body = "changed"
        return 1

    @classmethod
    @contract_method
    async def core_insert(cls) -> int:
        await contract_session().execute(insert(Note).values(body="lost"))
        return 1

    @classmethod
    @contract_method
    async def audit(cls) -> int:
        record_audit(
            "demo.read.audited",
            actor_id=uuid.uuid4(),
            resource_type="note",
            resource_id="1",
        )
        return 1

    @classmethod
    @contract_method
    async def queue_effect(cls) -> int:
        await on_commit(lambda: None)
        return 1


@pytest.fixture
async def notes_table(db_engine: Any) -> AsyncIterator[None]:
    async with db_engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
        await conn.execute(text("DELETE FROM read_guard_demo_notes"))
        await conn.execute(
            text("INSERT INTO read_guard_demo_notes (body) VALUES ('seed')")
        )
    yield
    async with db_engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)


@pytest.fixture
async def provider(
    sandloom_core: Any, db_engine: Any, notes_table: None
) -> AsyncIterator[Any]:
    async with override_instance(
        sandloom_core, "database", driver="sqlalchemy", url=os.environ["DATABASE_URL"]
    ):
        db = sandloom_core.database("default")
        core = CoreFacade(app=FastAPI())
        core.database = lambda name="default": db  # type: ignore[method-assign]
        core.register_service(SERVICE, Notes)
        yield core.get_service(SERVICE)


async def _rows(db_engine: Any) -> list[str]:
    sessions = async_sessionmaker(db_engine, class_=AsyncSession)
    async with sessions() as db:
        return [
            str(row)
            for row in (
                await db.execute(text("SELECT body FROM read_guard_demo_notes"))
            ).scalars()
        ]


def _scope(shared: bool) -> AbstractAsyncContextManager[Any]:
    return contract_read_scope() if shared else nullcontext()


@pytest.mark.parametrize("shared", [False, True], ids=["own_uow", "read_scope"])
@pytest.mark.parametrize(
    ("method", "what"),
    [
        ("add_unflushed", "added ORM objects"),
        ("add_flushed", "added ORM objects"),
        ("add_swallowed", "added ORM objects"),
        ("modify", "modified ORM objects"),
        ("core_insert", "INSERT / UPDATE / DELETE"),
        ("audit", "audit / outbox rows"),
        ("queue_effect", "on_commit"),
    ],
)
async def test_read_contract_write_fails_loudly(
    provider: Any, db_engine: Any, method: str, what: str, shared: bool
) -> None:
    call: Callable[[], Awaitable[int]] = getattr(provider, method)
    async with _scope(shared):
        with pytest.raises(ReadContractWriteError, match=what):
            await call()
        # the shared transaction was dropped: a following read is clean
        assert await provider.count() == 1
    assert await _rows(db_engine) == ["seed"]


async def test_pure_read_still_succeeds(provider: Any) -> None:
    assert await provider.count() == 1
    async with contract_read_scope():
        assert await provider.count() == 1
        assert await provider.count() == 1
