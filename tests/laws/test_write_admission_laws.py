"""Write admission (spec §8.2; §4.4 write-admission guarantee).

Property: for any sequence of unit opens, writes, flushes and child tasks in
one request, no second writer's statement ever executes. The oracle is what
reaches PostgreSQL: the probe rows committed after the request always come
from ONE unit, and at most one unit ever had a write statement return.

Steps (Hypothesis draws 1..8 of them):

* ``lazy`` — a Core INSERT through the request's lazy unit (``db()``); a
  refusal fails the request (its lazy unit rolls back);
* ``flush`` — an ORM add + flush through ``db()``, refused the same way;
* ``block`` — a nested ``db_session()`` unit writes; a refusal is caught;
* ``child`` — a child task opens its own ``db_session()`` and writes; a
  refusal is caught.

The seeded version is
``tests/integration/test_units_and_admission.py::
test_random_unit_sequences_never_execute_a_second_writer``; this file has its
own probe table (``laws_admission_probe``) and declarative base.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Integer, String, insert, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core import runtime
from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.core.testing.infra import override_instance
from sandloom.core.unit_of_work import LazyUnit, db_session
from sandloom.core.unit_of_work.request import (
    MultiStoreWriteError,
    RequestScope,
    bind_request_scope,
)
from sandloom.core.unit_of_work.request import db as request_db

pytestmark = pytest.mark.asyncio

TABLE = "laws_admission_probe"


class _LawsBase(DeclarativeBase):
    pass


class Probe(_LawsBase):
    __tablename__ = TABLE

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    v: Mapped[str] = mapped_column(String(50))


class _Abort(Exception):
    """The request failed because its lazy unit's write was refused."""


@pytest.fixture
async def kcore(
    sandloom_core: Any, db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    """The platform core on the test database, active for ``db_session()``."""
    async with db_engine.begin() as conn:
        await conn.run_sync(_LawsBase.metadata.create_all)
    async with override_instance(
        sandloom_core, "database", driver="sqlalchemy", url=os.environ["DATABASE_URL"]
    ):
        monkeypatch.setattr(runtime, "_process_core", sandloom_core)
        yield sandloom_core


async def _rows(db_engine: Any) -> list[str]:
    async with db_engine.connect() as conn:
        result = await conn.execute(text(f"SELECT v FROM {TABLE} ORDER BY id"))
        return [r[0] for r in result]


async def _in_request(core: Any, body: Callable[[], Awaitable[None]]) -> None:
    """Run ``body`` like a request handler: its scope, its lazy unit."""
    scope = RequestScope(core=core, policy=None)
    scope.lazy_factory = lambda: LazyUnit(core, "default")
    with bind_request_scope(scope):
        try:
            await body()
        except BaseException as exc:
            if scope.lazy is not None:
                await scope.lazy.finish(exc)
            raise
        if scope.lazy is not None:
            await scope.lazy.finish(None)


STEPS = st.lists(
    st.sampled_from(["lazy", "block", "child", "flush"]), min_size=1, max_size=8
)


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(steps=STEPS)
async def test_no_second_writer_statement_ever_executes(
    kcore: Any, db_engine: Any, steps: list[str]
) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text(f"DELETE FROM {TABLE}"))
    #: units whose write statement RETURNED (i.e. executed at the database)
    writers_seen: set[str] = set()

    async def write(session: Any, who: str) -> None:
        await session.execute(insert(Probe).values(v=who))
        writers_seen.add(who.split(":")[0])

    async def body() -> None:
        for i, step in enumerate(steps):
            if step in ("lazy", "flush"):
                try:
                    if step == "lazy":
                        await write(request_db(), f"lazy:{i}")
                    else:
                        request_db().add(Probe(v=f"lazy:{i}"))
                        await request_db().flush()
                        writers_seen.add("lazy")
                except MultiStoreWriteError as exc:
                    raise _Abort from exc  # the request fails: rolled back
            elif step == "block":
                try:
                    async with db_session() as block:
                        await write(block, f"block{i}:{i}")
                except MultiStoreWriteError:
                    pass
            else:

                async def child(i: int = i) -> None:
                    try:
                        async with db_session() as own:
                            await write(own, f"child{i}:{i}")
                    except MultiStoreWriteError:
                        pass

                await asyncio.create_task(child())

    try:
        await _in_request(kcore, body)
    except _Abort:
        # the lazy unit was refused, so it never executed a write
        assert "lazy" not in writers_seen, (steps, writers_seen)
    rows = await _rows(db_engine)
    units = {r.split(":")[0] for r in rows}
    assert len(units) <= 1, (steps, rows)
    assert len(writers_seen) <= 1, (steps, writers_seen)
    # every unit whose statement executed and was not rolled back is the one
    # whose rows are committed
    assert units <= writers_seen, (steps, rows, writers_seen)
