"""One round trip for a UoW's outbox + audit rows (``sql_alchemy.effect_rows``).

The plain rows ``publish_event`` / ``record_audit`` register are written by ONE
prebuilt statement at flush time on PostgreSQL. Pinned here: the statement
count, the stored values, read-your-writes inside the transaction (autoflush,
also when the rows are the only thing pending), and the savepoint rules — a
row registered or written inside a savepoint that rolls back (or whose
ancestor rolls back) is never dispatched and never committed.
"""

from __future__ import annotations

import gc
import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sandloom.core import di
from sandloom.core import unit_of_work as uow_mod
from sandloom.core.sql_alchemy.models.audit import AuditLogEntry
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.outbox_store import (
    sql_alchemy_audit_store,
    sql_alchemy_outbox_store,
)
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.testing import reload_test_env
from sandloom.core.unit_of_work import dispatch as uow_mod_dispatch
from sandloom.core.unit_of_work import scope as uow_mod_scope

CHANNEL = "core.test_event.v1"


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    backend_dir = Path(__file__).resolve().parents[4]
    env_test = backend_dir / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not configured for tests")
    eng = create_async_engine(database_url, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(OutboxMessage.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def preflight(self, channel: str, payload: dict[str, Any]) -> None:
        pass

    async def publish_event(
        self,
        channel: str,
        payload: dict[str, Any],
        schema_version: str = "v1",
        event_id: str | None = None,
    ) -> None:
        self.published.append(payload)


def _statements(engine: AsyncEngine) -> list[str]:
    seen: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_a: Any) -> None:
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    return seen


async def _run_uow(
    session_factory: async_sessionmaker[AsyncSession],
    bus: _RecordingBus,
    body: Any,
) -> None:
    uow = SQLAlchemyUnitOfWork(session_factory=session_factory)
    async with uow:
        session = uow.session
        assert session is not None
        session.info[uow_mod_scope._EVENT_BUS_KEY] = bus
        session.info[uow_mod_scope._OUTBOX_STORE_KEY] = sql_alchemy_outbox_store()
        session.info[uow_mod_scope._AUDIT_STORE_KEY] = sql_alchemy_audit_store()
        session.info[uow_mod_scope._UOW_FACTORY_KEY] = lambda: SQLAlchemyUnitOfWork(
            session_factory=session_factory
        )
        token = uow_mod_scope._current_uow_session.set(session)
        try:
            await body(session)
        finally:
            uow_mod_scope._current_uow_session.reset(token)
    await uow_mod._dispatch_outbox_after_commit(session)
    await uow_mod_dispatch.drain_post_commit()


async def _rows(
    session_factory: async_sessionmaker[AsyncSession], tag: str
) -> tuple[list[OutboxMessage], list[AuditLogEntry]]:
    async with session_factory() as s:
        outbox = list(
            (
                await s.execute(
                    select(OutboxMessage).where(
                        OutboxMessage.payload["tag"].as_string() == tag
                    )
                )
            ).scalars()
        )
        audit = list(
            (
                await s.execute(
                    select(AuditLogEntry).where(AuditLogEntry.resource_id == tag)
                )
            ).scalars()
        )
        return outbox, audit


@pytest.mark.asyncio
async def test_outbox_and_audit_rows_are_one_statement(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tag = uuid.uuid4().hex
    actor = uuid.uuid4()
    bus = _RecordingBus()
    seen = _statements(engine)

    async def body(_session: AsyncSession) -> None:
        await di.publish_event(CHANNEL, {"tag": tag, "n": 1})
        await di.publish_event(CHANNEL, {"tag": tag, "n": 2})
        di.record_audit(
            "core.test.fused",
            actor_id=actor,
            resource_type="test",
            resource_id=tag,
            data={"password": "s3cret", "k": [1, 2]},
        )

    await _run_uow(session_factory, bus, body)
    inserts = [s for s in seen if "INSERT INTO" in s]
    assert len(inserts) == 1, inserts
    assert inserts[0].startswith("WITH audit AS (INSERT INTO audit_log")

    outbox, audit = await _rows(session_factory, tag)
    assert sorted(int(r.payload["n"]) for r in outbox) == [1, 2]
    for row in outbox:
        assert row.channel == CHANNEL
        assert row.schema_version == "v1"
        assert row.delivery_attempts == 0
        assert row.emitted_at is not None and row.emitted_at.tzinfo is None
        assert row.delivered_at is not None  # dispatched + marked after commit
    assert len(audit) == 1
    entry = audit[0]
    assert entry.action == "core.test.fused"
    assert entry.actor_id == actor
    assert entry.actor_type == "user"
    assert entry.resource_type == "test"
    assert entry.data == {"password": "***", "k": [1, 2]}
    assert entry.occurred_at is not None
    assert sorted(p["n"] for p in bus.published) == [1, 2]


@pytest.mark.asyncio
async def test_fused_rows_keep_the_outbox_trace_id(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fused INSERT writes the outbox provenance column (core ``0008``)."""
    from sandloom.core.sql_alchemy import outbox_store

    trace = "ab" * 16
    monkeypatch.setattr(outbox_store, "get_current_trace_id", lambda: trace)
    tag = uuid.uuid4().hex

    async def body(_session: AsyncSession) -> None:
        await di.publish_event(CHANNEL, {"tag": tag, "n": 1})
        di.record_audit(
            "core.test.fused",
            actor_id=uuid.uuid4(),
            resource_type="test",
            resource_id=tag,
        )

    await _run_uow(session_factory, _RecordingBus(), body)
    outbox, audit = await _rows(session_factory, tag)
    assert [row.trace_id for row in outbox] == [trace]
    assert len(audit) == 1


@pytest.mark.asyncio
async def test_rows_of_one_table_are_one_single_table_insert(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Outbox-only rows: one prebuilt INSERT for any number of rows."""
    tag = uuid.uuid4().hex
    bus = _RecordingBus()
    seen = _statements(engine)

    async def body(_session: AsyncSession) -> None:
        await di.publish_event(CHANNEL, {"tag": tag, "n": 1})
        await di.publish_event(CHANNEL, {"tag": tag, "n": 2})

    await _run_uow(session_factory, bus, body)
    inserts = [s for s in seen if "INSERT INTO" in s]
    assert len(inserts) == 1 and inserts[0].startswith("INSERT INTO outbox_messages")
    outbox, _audit = await _rows(session_factory, tag)
    assert len(outbox) == 2 and len(bus.published) == 2


@pytest.mark.asyncio
async def test_rows_are_visible_inside_the_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tag = uuid.uuid4().hex
    bus = _RecordingBus()

    async def body(session: AsyncSession) -> None:
        await di.publish_event(CHANNEL, {"tag": tag})
        di.record_audit("core.test.fused", actor_id=None, resource_id=tag)
        # autoflush writes them before this query, as with the ORM INSERTs
        found = await session.execute(
            select(OutboxMessage.id).where(
                OutboxMessage.payload["tag"].as_string() == tag
            )
        )
        assert len(found.all()) == 1
        count = await session.execute(
            text("SELECT count(*) FROM audit_log WHERE resource_id = :t"), {"t": tag}
        )
        assert count.scalar_one() == 1
        # a second batch in the same UoW is written by the commit flush
        await di.publish_event(CHANNEL, {"tag": tag, "second": True})

    await _run_uow(session_factory, bus, body)
    outbox, audit = await _rows(session_factory, tag)
    assert len(outbox) == 2 and len(audit) == 1
    assert len(bus.published) == 2


@pytest.mark.asyncio
async def test_written_rows_of_a_rolled_back_savepoint_are_not_dispatched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tag = uuid.uuid4().hex
    bus = _RecordingBus()

    async def body(session: AsyncSession) -> None:
        await di.publish_event(CHANNEL, {"tag": tag, "keep": 1})
        try:
            async with session.begin_nested():
                await di.publish_event(CHANNEL, {"tag": tag, "drop": 1})
                di.record_audit("core.test.fused", actor_id=None, resource_id=tag)
                await session.flush()  # the fused INSERT runs in the savepoint
                raise RuntimeError("roll back this savepoint")
        except RuntimeError:
            pass
        async with session.begin_nested():
            await di.publish_event(CHANNEL, {"tag": tag, "released": 1})
            await session.flush()

    await _run_uow(session_factory, bus, body)
    outbox, audit = await _rows(session_factory, tag)
    assert sorted(sorted(r.payload) for r in outbox) == [
        ["keep", "tag"],
        ["released", "tag"],
    ]
    assert audit == []
    published = [p for p in bus.published if p.get("tag") == tag]
    assert {"tag": tag, "drop": 1} not in published
    assert {"tag": tag, "keep": 1} in published
    assert {"tag": tag, "released": 1} in published


@pytest.mark.asyncio
async def test_rows_of_a_released_savepoint_follow_its_parent_rollback(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tag = uuid.uuid4().hex
    bus = _RecordingBus()

    async def body(session: AsyncSession) -> None:
        await di.publish_event(CHANNEL, {"tag": tag, "keep": 1})
        try:
            async with session.begin_nested():
                async with session.begin_nested():
                    await di.publish_event(CHANNEL, {"tag": tag, "inner": 1})
                    di.record_audit("core.test.fused", actor_id=None, resource_id=tag)
                    await session.flush()  # fused, inside the inner savepoint
                # the inner savepoint was RELEASED; its parent now rolls back
                raise RuntimeError("roll back the outer savepoint")
        except RuntimeError:
            pass

    await _run_uow(session_factory, bus, body)
    outbox, audit = await _rows(session_factory, tag)
    assert [r.payload for r in outbox] == [{"tag": tag, "keep": 1}]
    assert audit == []
    assert [p for p in bus.published if p.get("tag") == tag] == [
        {"tag": tag, "keep": 1}
    ]


@pytest.mark.asyncio
async def test_rolled_back_uow_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tag = uuid.uuid4().hex
    bus = _RecordingBus()

    async def body(session: AsyncSession) -> None:
        await di.publish_event(CHANNEL, {"tag": tag})
        di.record_audit("core.test.fused", actor_id=None, resource_id=tag)
        await session.flush()
        raise RuntimeError("fail the UoW")

    with pytest.raises(RuntimeError):
        await _run_uow(session_factory, bus, body)
    outbox, audit = await _rows(session_factory, tag)
    assert outbox == [] and audit == []
    assert bus.published == []


@pytest.mark.asyncio
async def test_a_uow_leaves_no_cyclic_garbage(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tag = uuid.uuid4().hex
    bus = _RecordingBus()

    async def body(session: AsyncSession) -> None:
        await di.publish_event(CHANNEL, {"tag": tag})
        di.record_audit("core.test.fused", actor_id=None, resource_id=tag)

    await _run_uow(session_factory, bus, body)  # warm caches
    gc.collect()
    # Count only cycles that involve objects these UoWs created: background
    # work left by earlier tests in the same xdist worker (pool disposal,
    # post-commit dispatch tasks) may produce unrelated cyclic garbage in
    # this window, so a bare `gc.collect() == 0` is order-dependent.
    gc.set_debug(gc.DEBUG_SAVEALL)
    gc.disable()
    try:
        for _ in range(5):
            await _run_uow(session_factory, bus, body)
        gc.collect()
        ours = [obj for obj in gc.garbage if _uow_owned(obj)]
    finally:
        gc.set_debug(0)
        gc.garbage.clear()
        gc.enable()
    assert ours == [], sorted({type(obj).__qualname__ for obj in ours})


_SESSION_TYPES = {"Session", "AsyncSession", "SessionTransaction", "InstanceState"}


def _uow_owned(obj: object) -> bool:
    """A UoW / session / effect-row object (not unrelated process garbage)."""
    cls = type(obj)
    module = getattr(cls, "__module__", "") or ""
    if module.startswith("sandloom."):
        return True
    return module.startswith("sqlalchemy.") and cls.__name__ in _SESSION_TYPES


@pytest.mark.asyncio
async def test_fused_writes_keep_the_planner_optimized_executor(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The listeners must not read as an application ORM policy: the
    execution planner refuses its optimized executor when ANY class-level
    session transaction listener exists."""
    from sandloom.core.sql_alchemy.planner_safety import policy_reason

    tag = uuid.uuid4().hex

    async def body(_session: AsyncSession) -> None:
        await di.publish_event(CHANNEL, {"tag": tag})
        di.record_audit("core.test.fused", actor_id=None, resource_id=tag)

    await _run_uow(session_factory, _RecordingBus(), body)
    assert policy_reason(engine, []) is None


@pytest.mark.asyncio
async def test_payload_mutated_after_publish_dispatches_what_was_committed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The dispatched event and the outbox row are the payload AT publish time.

    A caller reusing / mutating its dict after ``publish_event`` (top level or
    nested) must not make the bus deliver something else than the committed
    row under the same event id (the relay would redeliver the row's version).
    """
    tag = uuid.uuid4().hex
    bus = _RecordingBus()

    async def body(_session: AsyncSession) -> None:
        payload: dict[str, Any] = {"tag": tag, "n": 1, "items": [{"k": 1}]}
        await di.publish_event(CHANNEL, payload)
        payload["n"] = 2
        payload["extra"] = True
        payload["items"][0]["k"] = 99
        payload["items"].append({"k": 3})

    await _run_uow(session_factory, bus, body)
    expected = {"tag": tag, "n": 1, "items": [{"k": 1}]}
    outbox, _audit = await _rows(session_factory, tag)
    assert [row.payload for row in outbox] == [expected]
    assert bus.published == [expected]


@pytest.mark.asyncio
async def test_plain_rows_alone_dirty_the_session_for_autoflush(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Rows are plain objects, not ORM instances: the flush trigger alone
    makes a query autoflush them, an explicit flush writes them for raw SQL
    on the same connection, and the trigger itself is never written nor left
    pending."""
    from sandloom.core.orm import Base

    tag = uuid.uuid4().hex
    bus = _RecordingBus()
    seen = _statements(engine)

    async def body(session: AsyncSession) -> None:
        await di.publish_event(CHANNEL, {"tag": tag, "n": 1})
        assert len(session.new) == 1  # the trigger, not an OutboxMessage
        found = await session.execute(
            select(OutboxMessage.id).where(
                OutboxMessage.payload["tag"].as_string() == tag
            )
        )
        assert len(found.all()) == 1
        assert not session.new
        di.record_audit("core.test.fused", actor_id=None, resource_id=tag)
        await session.flush()
        conn = await session.connection()
        count = await conn.execute(
            text("SELECT count(*) FROM audit_log WHERE resource_id = :t"), {"t": tag}
        )
        assert count.scalar_one() == 1
        assert not session.new

    await _run_uow(session_factory, bus, body)
    inserts = [s for s in seen if "INSERT INTO" in s]
    assert [s.split(" (")[0] for s in inserts] == [
        "INSERT INTO outbox_messages",
        "INSERT INTO audit_log",
    ]
    assert not any("sandloom_effect_rows_flush_trigger" in s for s in seen)
    assert (
        "sandloom_effect_rows_flush_trigger_never_written" not in Base.metadata.tables
    )
    outbox, audit = await _rows(session_factory, tag)
    assert len(outbox) == 1 and len(audit) == 1
    assert bus.published == [{"tag": tag, "n": 1}]


@pytest.mark.asyncio
async def test_rows_of_a_savepoint_rolled_back_before_any_flush_are_dropped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Never written (no flush ran inside the savepoint): not committed and
    not dispatched — while rows registered after the rollback still are."""
    tag = uuid.uuid4().hex
    bus = _RecordingBus()

    async def body(session: AsyncSession) -> None:
        await di.publish_event(CHANNEL, {"tag": tag, "keep": 1})
        try:
            async with session.begin_nested():
                await di.publish_event(CHANNEL, {"tag": tag, "drop": 1})
                di.record_audit("core.test.fused", actor_id=None, resource_id=tag)
                raise RuntimeError("roll back before any flush")
        except RuntimeError:
            pass
        await di.publish_event(CHANNEL, {"tag": tag, "after": 1})

    await _run_uow(session_factory, bus, body)
    outbox, audit = await _rows(session_factory, tag)
    assert sorted(sorted(r.payload) for r in outbox) == [
        ["after", "tag"],
        ["keep", "tag"],
    ]
    assert audit == []
    assert sorted(sorted(p) for p in bus.published) == [
        ["after", "tag"],
        ["keep", "tag"],
    ]
