"""Units, ``db()`` ownership and write admission (spec §4.3, §4.4) on real
PostgreSQL, through the kernel's own engines and session class.

Independent oracles (spec §10.1): every assertion is about what reaches the
database — the probe table's rows after the request — never about a derived
artifact. Covered:

* ``db()``: outside a unit (SANDLOOM-R002); a child task (SANDLOOM-R013) before and
  after the parent created the lazy request unit (nothing is created by the
  child); ``db_session()`` in a child works and owns its unit; an exited unit
  refuses any later use through a captured reference (no auto-begin);
* best-effort detection of a session object passed to another task:
  ``add``, ``delete``, ``merge``, ``sync_session.expunge``, a statement and
  ``exec_driver_sql`` on a captured connection; the owner's flush contains
  none of it; the known undetectable form (in-place JSON mutation) is pinned;
* write admission: the first writing unit wins; a second unit's write is
  refused BEFORE it reaches the cursor — ORM, Core, ``text()``,
  ``exec_driver_sql()`` through the session's connection under ``unscoped()``,
  a nested unit on the same database, an unflushed change, a child task (both
  orders); a read-only parent plus a child's sole write is allowed; a
  contract unit has its own admission scope; a seeded random property over
  unit / write / flush / child sequences: no second writer's statement ever
  executes.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import Integer, String, insert, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import sandloom.core.sql_alchemy.write_admission as _wa
from sandloom.core import runtime
from sandloom.core.tenant import unscoped
from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.core.testing.infra import override_instance
from sandloom.core.unit_of_work import LazyUnit, db_session
from sandloom.core.unit_of_work.effects import UnitOfWorkRequiredError
from sandloom.core.unit_of_work.request import (
    ConcurrentSessionUseError,
    MultiStoreWriteError,
    RequestScope,
    WriteAdmission,
    bind_request_scope,
    current_request_scope,
    db,
)

pytestmark = pytest.mark.asyncio

TABLE = "wsa_admission_probe"


class _Base(DeclarativeBase):
    pass


class Probe(_Base):
    __tablename__ = TABLE

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    v: Mapped[str] = mapped_column(String(50))
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


@pytest.fixture
async def kcore(
    sandloom_core: Any, db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    """The platform core on the test database, active for ``db_session()``."""
    async with db_engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
        await conn.execute(text(f"DELETE FROM {TABLE}"))
    async with override_instance(
        sandloom_core, "database", driver="sqlalchemy", url=os.environ["DATABASE_URL"]
    ):
        monkeypatch.setattr(runtime, "_process_core", sandloom_core)
        yield sandloom_core


async def _rows(db_engine: Any) -> list[str]:
    async with db_engine.connect() as conn:
        return [
            r[0] for r in await conn.execute(text(f"SELECT v FROM {TABLE} ORDER BY id"))
        ]


def _scope(core: Any) -> RequestScope:
    scope = RequestScope(core=core, policy=None)
    scope.lazy_factory = lambda: LazyUnit(core, "default")
    return scope


async def _in_request(core: Any, body: Callable[[], Awaitable[Any]]) -> Any:
    """Run ``body`` like a request handler: its scope, its lazy unit."""
    scope = _scope(core)
    with bind_request_scope(scope):
        try:
            result = await body()
        except BaseException as exc:
            if scope.lazy is not None:
                await scope.lazy.finish(exc)
            raise
        if scope.lazy is not None:
            await scope.lazy.finish(None)
    return result


# --------------------------------------------------------------------------- #
# db() and ownership
# --------------------------------------------------------------------------- #
async def test_db_outside_any_unit_is_refused(kcore: Any) -> None:
    with pytest.raises(UnitOfWorkRequiredError, match="SANDLOOM-R002"):
        db()


async def test_db_in_a_block_is_that_units_session(kcore: Any, db_engine: Any) -> None:
    async with db_session() as session:
        assert db() is session
        await db().execute(insert(Probe).values(v="block"))
    assert await _rows(db_engine) == ["block"]


async def test_lazy_request_unit_commits_on_success_and_rolls_back_on_error(
    kcore: Any, db_engine: Any
) -> None:
    async def ok() -> None:
        db().add(Probe(v="lazy-ok"))
        await db().flush()

    async def boom() -> None:
        db().add(Probe(v="lazy-rolled-back"))
        await db().flush()
        raise RuntimeError("handler failed")

    await _in_request(kcore, ok)
    with pytest.raises(RuntimeError):
        await _in_request(kcore, boom)
    assert await _rows(db_engine) == ["lazy-ok"]


async def test_child_db_before_the_parent_creates_nothing_then_parent_owns(
    kcore: Any, db_engine: Any
) -> None:
    async def body() -> None:
        async def child() -> None:
            db()

        with pytest.raises(ConcurrentSessionUseError, match="SANDLOOM-R013"):
            await asyncio.create_task(child())
        scope = current_request_scope()
        assert scope is not None and scope.lazy is None  # the child created nothing
        session = db()  # the parent creates and owns it
        session.add(Probe(v="parent"))
        await session.flush()
        with pytest.raises(ConcurrentSessionUseError):
            await asyncio.create_task(child())  # after: refused too

        async def child_own_unit() -> None:
            async with db_session() as own:
                assert db() is own  # the child owns its own unit

        await asyncio.create_task(child_own_unit())

    await _in_request(kcore, body)
    assert await _rows(db_engine) == ["parent"]


async def test_child_open_uow_before_the_parent_works(kcore: Any) -> None:
    async def body() -> None:
        async def child() -> int:
            async with db_session() as own:
                return int((await own.execute(text("SELECT 1"))).scalar_one())

        assert await asyncio.create_task(child()) == 1
        assert db() is not None

    await _in_request(kcore, body)


async def test_an_exited_unit_refuses_reuse_and_never_autobegins(kcore: Any) -> None:
    async with db_session() as session:
        await session.execute(text("SELECT 1"))
    with pytest.raises(UnitOfWorkRequiredError, match="SANDLOOM-R002"):
        await session.execute(text("SELECT 1"))
    with pytest.raises(UnitOfWorkRequiredError):
        session.add(Probe(v="late"))


@pytest.fixture
def owner_checks(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Turn the best-effort owner detection on for one test, whatever the
    ambient ``DB_SESSION_OWNER_CHECKS`` (it is off the hot path, spec §10.4)."""
    added = [
        n
        for n in (*_wa._GUARDED_METHODS, "_connection_for_bind")
        if n not in _wa.KernelSession.__dict__
    ]
    # The setting too: install_write_admission re-reads it whenever an engine
    # is installed, so an engine first built inside the test (e.g. when no
    # earlier test built it) would turn the module flag back off.
    monkeypatch.setattr(_wa.settings, "db_session_owner_checks", True)
    monkeypatch.setattr(_wa, "_owner_checks", True)
    _wa._install_method_guards()
    yield
    for name in added:
        delattr(_wa.KernelSession, name)


@pytest.mark.parametrize(
    "misuse",
    ["add", "delete", "merge", "expunge", "execute", "exec_driver_sql"],
)
async def test_a_session_passed_to_another_task_is_detected(
    kcore: Any, db_engine: Any, misuse: str, owner_checks: Any
) -> None:
    async with db_session() as session:
        row = Probe(v="seed")
        session.add(row)
        await session.flush()
        conn = await session.connection()

        async def child() -> None:
            if misuse == "add":
                session.add(Probe(v="smuggled"))
            elif misuse == "delete":
                await session.delete(row)
            elif misuse == "merge":
                await session.merge(Probe(v="merged"))
            elif misuse == "expunge":
                session.sync_session.expunge(row)
            elif misuse == "execute":
                await session.execute(insert(Probe).values(v="smuggled"))
            else:
                await conn.exec_driver_sql(
                    f"INSERT INTO {TABLE} (v) VALUES ('smuggled')"
                )

        with pytest.raises(ConcurrentSessionUseError, match="SANDLOOM-R013"):
            await asyncio.create_task(child())
    assert await _rows(db_engine) == ["seed"]


async def test_in_place_json_mutation_is_a_known_undetectable_form(
    kcore: Any, db_engine: Any
) -> None:
    """Stated gap (spec §4.3 item 4): SQLAlchemy emits no event for an
    in-place mutation of a plain JSON value, so no hook can tell which task
    made it. Pinned: the child's mutation is NOT detected (and, since no
    attribute event fired, not flushed either)."""
    async with db_session() as session:
        row = Probe(v="json", data={"k": 1})
        session.add(row)
        await session.flush()

        async def child() -> None:
            assert row.data is not None
            row.data["k"] = 2  # no event, no detection

        await asyncio.create_task(child())  # no error raised


# --------------------------------------------------------------------------- #
# write admission
# --------------------------------------------------------------------------- #
WRITES: dict[str, Callable[[Any], Awaitable[Any]]] = {
    "orm": lambda s: _orm_write(s),
    "core": lambda s: s.execute(insert(Probe).values(v="second")),
    "text": lambda s: s.execute(text(f"INSERT INTO {TABLE} (v) VALUES ('second')")),
    "exec_driver_sql_unscoped": lambda s: _driver_write(s),
    "dml_cte": lambda s: s.execute(
        text(
            f"WITH ins AS (INSERT INTO {TABLE} (v) VALUES ('second') RETURNING id) "
            "SELECT id FROM ins"
        )
    ),
}


async def _orm_write(session: Any) -> None:
    session.add(Probe(v="second"))
    await session.flush()


async def _driver_write(session: Any) -> None:
    with unscoped():
        conn = await session.connection()
        await conn.exec_driver_sql(f"INSERT INTO {TABLE} (v) VALUES ('second')")


@pytest.mark.parametrize("path", sorted(WRITES))
async def test_a_second_writing_unit_is_refused_before_the_cursor(
    kcore: Any, db_engine: Any, path: str
) -> None:
    async def body() -> None:
        db().add(Probe(v="first"))
        await db().flush()  # the request unit is the writer
        async with db_session() as second:
            await second.execute(text("SELECT 1"))  # reads are fine
            with pytest.raises(MultiStoreWriteError, match="SANDLOOM-R007"):
                await WRITES[path](second)
            raise _Abort

    with pytest.raises(_Abort):
        await _in_request(kcore, body)
    assert await _rows(db_engine) == []  # nothing of either unit committed


class _Abort(Exception):
    pass


async def test_an_unflushed_change_in_a_second_unit_is_refused_at_commit(
    kcore: Any, db_engine: Any
) -> None:
    async def body() -> None:
        db().add(Probe(v="first"))
        await db().flush()
        with pytest.raises(MultiStoreWriteError):
            async with db_session() as second:
                second.add(Probe(v="unflushed"))  # flushed by commit()

    await _in_request(kcore, body)
    assert await _rows(db_engine) == ["first"]


async def test_the_first_writer_wins_even_if_it_opened_later(
    kcore: Any, db_engine: Any
) -> None:
    async def body() -> None:
        await db().execute(text("SELECT 1"))  # the request unit only reads
        async with db_session() as block:
            await block.execute(insert(Probe).values(v="block-writer"))
        with pytest.raises(MultiStoreWriteError):
            await db().execute(insert(Probe).values(v="late"))

    with pytest.raises(MultiStoreWriteError):
        # the late write was refused; the handler's failure rolls the lazy unit
        async def wrapped() -> None:
            await body()
            raise MultiStoreWriteError("propagate")

        await _in_request(kcore, wrapped)
    assert await _rows(db_engine) == ["block-writer"]


async def test_read_only_parent_and_a_childs_sole_write_is_allowed(
    kcore: Any, db_engine: Any
) -> None:
    async def body() -> None:
        await db().execute(text("SELECT 1"))

        async def child() -> None:
            async with db_session() as own:
                await own.execute(insert(Probe).values(v="child-only-writer"))

        await asyncio.create_task(child())

    await _in_request(kcore, body)
    assert await _rows(db_engine) == ["child-only-writer"]


@pytest.mark.parametrize("parent_first", [True, False])
async def test_a_child_and_its_parent_never_both_write(
    kcore: Any, db_engine: Any, parent_first: bool
) -> None:
    async def child() -> None:
        async with db_session() as own:
            await own.execute(insert(Probe).values(v="child"))

    async def body() -> None:
        if parent_first:
            await db().execute(insert(Probe).values(v="parent"))
            with pytest.raises(MultiStoreWriteError):
                await asyncio.create_task(child())
        else:
            await asyncio.create_task(child())
            with pytest.raises(MultiStoreWriteError):
                await db().execute(insert(Probe).values(v="parent"))
            raise _Abort

    if parent_first:
        await _in_request(kcore, body)
        assert await _rows(db_engine) == ["parent"]
    else:
        with pytest.raises(_Abort):
            await _in_request(kcore, body)
        assert await _rows(db_engine) == ["child"]


async def test_outside_a_request_units_are_unconstrained(
    kcore: Any, db_engine: Any
) -> None:
    async with db_session() as a:
        await a.execute(insert(Probe).values(v="a"))
    async with db_session() as b:
        await b.execute(insert(Probe).values(v="b"))
    assert await _rows(db_engine) == ["a", "b"]


async def test_declared_secondaries_are_admitted() -> None:
    adm = WriteAdmission(write_store="default", secondaries=("analytics",))
    first, second, third = object(), object(), object()
    adm.admit(first, "default", "w")
    adm.admit(second, "analytics", "w")  # the opted-out secondary
    with pytest.raises(MultiStoreWriteError):
        adm.admit(third, "analytics", "w")  # one unit per secondary
    other = WriteAdmission(write_store="default")
    with pytest.raises(MultiStoreWriteError, match="write store"):
        other.admit(first, "analytics", "w")


async def test_a_secondary_written_before_the_write_store_is_admitted() -> None:
    """The documented pattern (secondaries first, the write store last): a
    secondary's write never makes it the request's writer, so the write
    store's unit is still admitted after it; one unit per secondary and one
    writer on the write store still hold."""
    adm = WriteAdmission(write_store="default", secondaries=("analytics",))
    secondary, primary, again, extra = object(), object(), object(), object()
    adm.admit(secondary, "analytics", "w")
    assert adm.writer is None
    adm.admit(primary, "default", "w")
    assert adm.writer_database == "default"
    with pytest.raises(MultiStoreWriteError, match="opted-out secondary"):
        adm.admit(again, "analytics", "w")
    with pytest.raises(MultiStoreWriteError, match="second unit"):
        adm.admit(extra, "default", "w")


# --------------------------------------------------------------------------- #
# property: no second writer's statement ever executes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", range(12))
async def test_random_unit_sequences_never_execute_a_second_writer(
    kcore: Any, db_engine: Any, seed: int
) -> None:
    """Seeded random sequences of unit opens, writes, flushes and child tasks
    (Hypothesis joins the dev tools with WS-B; the property is the same): the
    rows committed always come from ONE unit."""
    rng = random.Random(seed)
    steps = [rng.choice(["lazy", "block", "child", "flush"]) for _ in range(6)]
    writers_seen: set[str] = set()

    async def write(session: Any, who: str) -> None:
        await session.execute(insert(Probe).values(v=who))
        writers_seen.add(who.split(":")[0])

    async def body() -> None:
        for i, step in enumerate(steps):
            if step in ("lazy", "flush"):
                try:
                    if step == "lazy":
                        await write(db(), f"lazy:{i}")
                    else:
                        db().add(Probe(v=f"lazy:{i}"))
                        await db().flush()
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
        writers_seen.discard("lazy")
    rows = await _rows(db_engine)
    units = {r.split(":")[0] for r in rows}
    assert len(units) <= 1, (steps, rows)
    assert len(writers_seen) <= 1, (steps, writers_seen)


async def test_classification_of_core_constructs(kcore: Any) -> None:
    """The conservative classifier on SQLAlchemy-built statements: a SELECT
    is read, a DML CTE built with Core is a write."""
    from sqlalchemy import Table

    from sandloom.core.sql_alchemy.write_admission import classify_sql

    table: Table = Probe.__table__  # type: ignore[assignment]
    cte = insert(table).values(v="x").returning(table.c.id).cte("ins")
    stmt = select(cte.c.id)
    dialect = kcore.database("default").engine.dialect
    assert classify_sql(str(stmt.compile(dialect=dialect))) is True
    assert classify_sql(str(select(table.c.v).compile(dialect=dialect))) is False
