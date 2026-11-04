"""The sqlalchemy driver on SQLite (design/infra-agnostic/spec.md, slice 8a).

A real file database in ``tmp_path``, opened through the driver's own engine
path (``_build_engine``: the aiosqlite adapter's setup, the idle ping, write
admission). Units commit and roll back, every unit not declared read-only
begins ``BEGIN IMMEDIATE`` (reads before its first write included), a second
writer waits for the first and succeeds within ``busy_timeout``, a read-only
unit refuses writes and reads one snapshot, ``transaction_lock`` holds inside
a writable unit and refuses elsewhere, and the kernel's stores (request log,
command log and inbox claims, the outbox relay's compare-and-set claim and
AUTOCOMMIT mark, the saga sweep, projection watermarks) run on it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    event,
    exc,
    insert,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import sandloom.core.sql_alchemy.saga_store as _saga_store
from sandloom.core.config.settings import settings
from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.drivers.dbapi_aiosqlite import (
    NestedWriterRefused,
    SqliteUnavailable,
    in_memory,
    sqlite_url_problem,
)
from sandloom.core.drivers.sql_dialects import SQLITE
from sandloom.core.sql_alchemy.command_log import sql_alchemy_command_log_store
from sandloom.core.sql_alchemy.durable_inbox import durable_inbox
from sandloom.core.sql_alchemy.models.consumer_inbox import ConsumerInboxEntry
from sandloom.core.sql_alchemy.models.contract_command import ContractCommandRecord
from sandloom.core.sql_alchemy.models.http_idempotency import HttpIdempotencyRecord
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.models.projection_watermark import (
    ProjectionWatermark,
)
from sandloom.core.sql_alchemy.models.saga import SagaInstance
from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_outbox_store
from sandloom.core.sql_alchemy.portable import (
    WRITE_LOCK_HELD,
    UnsupportedSqlForm,
    UtcDateTime,
    classify_error,
    skips_locked_rows,
    transaction_lock,
    transaction_options_for,
)
from sandloom.core.sql_alchemy.projection_watermarks import RelationalWatermarkStore
from sandloom.core.sql_alchemy.request_log import sql_alchemy_request_log_store
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.sql_alchemy.write_admission import KernelSession
from sandloom.core.tasks import relay
from sandloom.core.testing import reload_test_env

KERNEL_TABLES = [
    m.__table__
    for m in (
        OutboxMessage,
        SagaInstance,
        HttpIdempotencyRecord,
        ContractCommandRecord,
        ConsumerInboxEntry,
        ProjectionWatermark,
    )
]
_ITEMS = MetaData()
ITEMS = Table("lite_items", _ITEMS, Column("id", Integer, primary_key=True))


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest.fixture(autouse=True)
def _reset_circuit() -> Iterator[None]:
    relay.broker_circuit().reset()
    yield
    relay.broker_circuit().reset()


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'sandloom.db'}"


@pytest_asyncio.fixture
async def engine(url: str) -> AsyncGenerator[AsyncEngine, None]:
    eng = driver._build_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(
            lambda c: OutboxMessage.metadata.create_all(c, tables=KERNEL_TABLES)
        )
        await conn.run_sync(_ITEMS.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        sync_session_class=KernelSession,
        expire_on_commit=False,
    )


@pytest.fixture
def factory(sessions: async_sessionmaker[AsyncSession]) -> Any:
    return lambda: SQLAlchemyUnitOfWork(session_factory=sessions)


@pytest.fixture
def read_only(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions of a read-only unit: the renderer's options, as ``di`` binds."""
    options = transaction_options_for(engine, read_only=True, snapshot=None)
    return async_sessionmaker(
        bind=engine.execution_options(**options),
        class_=AsyncSession,
        expire_on_commit=False,
    )


def _statements(engine: AsyncEngine) -> list[str]:
    seen: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _seen(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        seen.append(statement)

    return seen


async def _count(session: AsyncSession) -> int:
    return int(
        (await session.execute(text("SELECT count(*) FROM lite_items"))).scalar_one()
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# -- the connection ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_connection_opens_with_wal_busy_timeout_and_foreign_keys(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as conn:
        assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
        assert busy == settings.db_statement_timeout_ms
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1


def test_sqlite_is_served_and_declares_its_features(url: str) -> None:
    assert "sqlite" in driver.SQLALCHEMY_SQL_DIALECTS
    features = driver.sqlalchemy_db_features({"url": url})
    assert {"transaction_locks", "snapshot_reads", "readonly_transactions"} <= features
    assert "skip_locked" not in features


def test_an_in_memory_database_is_refused_but_a_test_engine(tmp_path: Path) -> None:
    for memory in (
        "sqlite+aiosqlite://",
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite:///file:x?mode=memory&uri=true",
    ):
        assert in_memory(memory)
        with pytest.raises(SqliteUnavailable, match="SANDLOOM-B032"):
            driver._build_engine(memory)
        withheld = driver.sqlalchemy_db_features({"url": memory})
        assert not {"snapshot_reads", "transaction_locks"} & withheld
    assert not in_memory(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    assert "SANDLOOM-B032" in (sqlite_url_problem("sqlite:///x.db") or "")
    assert sqlite_url_problem("postgresql+asyncpg://h/db") is None


@pytest.mark.asyncio
async def test_the_test_engine_shares_one_in_memory_connection() -> None:
    eng = driver.build_disposable_engine("sqlite+aiosqlite://")
    try:
        async with eng.begin() as conn:
            await conn.run_sync(_ITEMS.create_all)
            await conn.execute(insert(ITEMS).values(id=1))
        async with eng.connect() as conn:
            assert await _count(AsyncSession(bind=conn)) == 1
    finally:
        await eng.dispose()


def test_a_missing_aiosqlite_is_refused(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setattr(
        "sandloom.core.drivers.dbapi_aiosqlite.aiosqlite_installed", lambda: False
    )
    problem = sqlite_url_problem(url) or ""
    assert "sandloom[sqlite]" in problem and "SANDLOOM-B032" in problem


# -- units ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_units_commit_and_roll_back(factory: Any) -> None:
    async with factory() as uow:
        await uow.session.execute(insert(ITEMS).values(id=1))
    with pytest.raises(RuntimeError):
        async with factory() as uow:
            await uow.session.execute(insert(ITEMS).values(id=2))
            raise RuntimeError("undo")
    async with factory() as uow:
        nested = await uow.session.begin_nested()
        await uow.session.execute(insert(ITEMS).values(id=3))
        await nested.rollback()
        await uow.session.execute(insert(ITEMS).values(id=4))
    async with factory() as uow:
        ids = (await uow.session.execute(select(ITEMS.c.id))).scalars().all()
    assert sorted(ids) == [1, 4]


@pytest.mark.asyncio
async def test_a_unit_begins_immediate_even_when_it_reads_first(
    engine: AsyncEngine, factory: Any
) -> None:
    seen = _statements(engine)
    async with factory() as uow:
        await _count(uow.session)
        raw = await (await uow.session.connection()).get_raw_connection()
        # pysqlite begins nothing itself: the unit's BEGIN is the only one.
        assert raw.dbapi_connection is not None
        assert raw.dbapi_connection.isolation_level is None
    assert seen[0] == "BEGIN IMMEDIATE"


@pytest.mark.asyncio
async def test_a_second_writer_waits_then_succeeds(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async def write(n: int) -> None:
        async with sessions() as session, session.begin():
            await session.execute(insert(ITEMS).values(id=n))

    async with sessions() as first, first.begin():
        await _count(first)  # holds the write lock from its BEGIN
        second = asyncio.create_task(write(2))
        await asyncio.sleep(0.3)
        assert not second.done()
        await first.execute(insert(ITEMS).values(id=1))
    await second
    async with sessions() as session:
        assert await _count(session) == 2


@pytest.mark.asyncio
async def test_a_writer_past_busy_timeout_gets_a_lock_error(
    monkeypatch: pytest.MonkeyPatch, url: str, engine: AsyncEngine
) -> None:
    monkeypatch.setattr(settings, "db_statement_timeout_ms", 100)
    impatient = driver._build_engine(url)
    try:

        async def other_writer() -> None:  # another task: it waits its turn
            async with impatient.connect() as conn:
                await conn.execute(text("SELECT 1"))

        async with engine.connect() as holder:
            await holder.execute(text("SELECT 1"))  # BEGIN IMMEDIATE
            with pytest.raises(exc.OperationalError) as raised:
                await asyncio.create_task(other_writer())
            await holder.rollback()
    finally:
        await impatient.dispose()
    assert classify_error("sqlite", raised.value) == "lock_not_available"


@pytest.mark.asyncio
async def test_a_read_only_unit_refuses_writes_then_the_connection_writes(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    read_only: async_sessionmaker[AsyncSession],
) -> None:
    seen = _statements(engine)
    async with read_only() as session:
        assert await _count(session) == 0
        with pytest.raises(exc.OperationalError, match="readonly"):
            await session.execute(insert(ITEMS).values(id=1))
    assert seen[:2] == ["BEGIN DEFERRED", "PRAGMA query_only = ON"]
    async with read_only() as session:
        await _count(session)
        await session.commit()
    # The same pooled connection (LIFO) writes again in a writable unit.
    async with sessions() as session, session.begin():
        await session.execute(insert(ITEMS).values(id=2))
    async with sessions() as session:
        assert await _count(session) == 1


@pytest.mark.asyncio
async def test_a_read_only_unit_reads_one_snapshot(
    sessions: async_sessionmaker[AsyncSession],
    read_only: async_sessionmaker[AsyncSession],
) -> None:
    async with read_only() as reader:
        assert await _count(reader) == 0
        async with sessions() as writer, writer.begin():
            await writer.execute(insert(ITEMS).values(id=1))
        assert await _count(reader) == 0  # the snapshot of its first read
    async with read_only() as reader:
        assert await _count(reader) == 1


@pytest.mark.asyncio
async def test_transaction_lock_holds_inside_a_writable_unit_only(
    sessions: async_sessionmaker[AsyncSession],
    read_only: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        await transaction_lock(7, session=session)
        await session.execute(insert(ITEMS).values(id=1))
    async with read_only() as session:
        with pytest.raises(UnsupportedSqlForm, match="SANDLOOM-R015"):
            await transaction_lock(7, session=session)


@pytest.mark.asyncio
async def test_an_autocommit_statement_commits_alone(engine: AsyncEngine) -> None:
    seen = _statements(engine)
    view = engine.execution_options(isolation_level="AUTOCOMMIT")
    async with view.connect() as conn:
        await conn.execute(insert(ITEMS).values(id=1))
    assert not any(s.startswith("BEGIN") for s in seen)
    async with engine.connect() as conn:
        assert await _count(AsyncSession(bind=conn)) == 1
        # SQLAlchemy's isolation reset of that connection turned pysqlite's
        # implicit BEGIN back on; the unit's BEGIN turned it off again.
        raw = await conn.get_raw_connection()
        assert raw.dbapi_connection is not None
        assert raw.dbapi_connection.isolation_level is None
        await conn.rollback()
    assert seen[-2:] == ["BEGIN IMMEDIATE", "SELECT count(*) FROM lite_items"]


@pytest.mark.asyncio
async def test_an_aware_time_reads_back_in_utc(engine: AsyncEngine) -> None:
    meta = MetaData()
    stamps = Table(
        "lite_stamps",
        meta,
        Column("id", Integer, primary_key=True),
        Column("at", UtcDateTime()),
    )
    at = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)
        await conn.execute(insert(stamps).values(id=1, at=at))
        found = (await conn.execute(select(stamps.c.at))).scalar_one()
    assert found == at and found.tzinfo is UTC


# -- the stores ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_log_claims_once_and_replays(factory: Any) -> None:
    store = sql_alchemy_request_log_store()
    key = uuid.uuid4().hex  # a digest, as the HTTP guard writes it
    args = {"scope": "POST /x", "tenant": None, "fingerprint": "f", "ttl_seconds": 60}
    async with factory() as uow:
        first = await store.claim(uow.session, dedupe_key=key, **args)
        await store.complete(uow.session, dedupe_key=key, status="200", result="{}")
    async with factory() as uow:
        again = await store.claim(uow.session, dedupe_key=key, **args)
    assert first.outcome == "claimed"
    assert (again.outcome, again.status) == ("replay", "200")


@pytest.mark.asyncio
async def test_command_log_and_inbox_claim_once(factory: Any) -> None:
    store = sql_alchemy_command_log_store()
    args = {"contract": "c.v1", "tenant": None, "args_digest": "d", "ttl_seconds": 60}
    inbox = durable_inbox("sqlite@test.v1")
    async with factory() as uow:
        claimed = await store.claim(uow.session, dedupe_key="k", **args)
        assert await inbox.claim(uow.session, "event-1")
    async with factory() as uow:
        assert not await inbox.claim(uow.session, "event-1")
    assert claimed.outcome == "claimed"


class _Bus:
    def __init__(self) -> None:
        self.published: list[int] = []

    async def publish(
        self, _channel: str, payload: dict[str, Any], _version: str, _id: str
    ) -> None:
        await asyncio.sleep(0)
        self.published.append(payload["n"])


async def _seed(factory: Any, n: int) -> list[uuid.UUID]:
    base = _now() - timedelta(minutes=5)
    ids = [uuid.uuid4() for _ in range(n)]
    async with factory() as uow:
        for i, msg_id in enumerate(ids):
            uow.session.add(
                OutboxMessage(
                    id=msg_id,
                    channel=f"core.test_event_{i}.v1",
                    schema_version="v1",
                    payload={"n": i},
                    emitted_at=base + timedelta(milliseconds=i),
                )
            )
    return ids


@pytest.mark.asyncio
async def test_parallel_relays_publish_each_row_once(
    engine: AsyncEngine, factory: Any
) -> None:
    assert not skips_locked_rows(engine)  # the compare-and-set claim
    await _seed(factory, 8)
    bus = _Bus()
    store = sql_alchemy_outbox_store()
    results = await asyncio.gather(
        *(
            store.deliver_batch(
                factory, bus.publish, max_attempts=5, grace_seconds=0, limit=8
            )
            for _ in range(3)
        )
    )
    assert sorted(bus.published) == list(range(8))
    assert sum(r.delivered for r in results) == 8


@pytest.mark.asyncio
async def test_mark_delivered_runs_as_one_autocommit_statement(
    engine: AsyncEngine, factory: Any
) -> None:
    ids = await _seed(factory, 2)
    seen = _statements(engine)
    await sql_alchemy_outbox_store().mark_delivered(factory, ids)
    assert not any(s.startswith("BEGIN") for s in seen)
    async with factory() as uow:
        delivered = (
            await uow.session.execute(
                select(OutboxMessage.delivered_at).where(OutboxMessage.id.in_(ids))
            )
        ).scalars()
        assert all(at is not None for at in delivered)


@pytest.mark.asyncio
async def test_a_saga_sweep_claims_an_instance_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    now = _now()
    instance_id = uuid.uuid4()
    async with sessions() as session, session.begin():
        await session.execute(
            insert(SagaInstance.__table__).values(
                id=instance_id,
                saga_type="core_test.sqlite",
                definition_version=1,
                saga_id=str(instance_id),
                tenant_id=None,
                status="running",
                step_index=0,
                direction="forward",
                resolving=False,
                attempt=1,
                next_attempt_at=now - timedelta(seconds=10),
                deadline_at=now + timedelta(hours=1),
                input={},
                results={},
                applied=[],
                version=3,
                created_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(minutes=1),
            )
        )
    swept_before = now - timedelta(seconds=5)

    async def sweep() -> list[Any]:
        async with sessions() as session, session.begin():
            return await _saga_store.claim_due(
                session, now=now, swept_before=swept_before, limit=10
            )

    first, second = await asyncio.gather(sweep(), sweep())
    claimed = [d.id for d in [*first, *second] if d.id == instance_id]
    assert claimed == [instance_id]


@pytest.mark.asyncio
async def test_projection_watermarks(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    def _open() -> Any:
        return _committing(sessions)

    store = RelationalWatermarkStore(_open)
    earlier = datetime(2026, 1, 1)
    later = datetime(2026, 2, 1)
    assert await store.raise_purged_before("p", later) == later
    assert await store.raise_purged_before("p", earlier) == later  # never lowers
    await store.begin_rebuild("p", earlier)
    async with store.purge_window("p") as mark:
        assert mark.purged_before == later and mark.rebuild_started_at == earlier
    await store.end_rebuild("p")
    assert (await store.get("p")).rebuild_started_at is None


class _committing:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._session = sessions()

    async def __aenter__(self) -> AsyncSession:
        await self._session.__aenter__()
        return self._session

    async def __aexit__(self, *exc_info: Any) -> None:
        if exc_info[0] is None:
            await self._session.commit()
        await self._session.__aexit__(*exc_info)


def test_the_renderer_begins_each_unit() -> None:
    assert SQLITE.begin_statement(write=True, read_only=False, snapshot=False) == (
        "BEGIN IMMEDIATE"
    )
    assert SQLITE.begin_statement(write=False, read_only=False, snapshot=True) == (
        "BEGIN IMMEDIATE"
    )
    for snapshot in (False, True):
        assert SQLITE.begin_statement(
            write=False, read_only=True, snapshot=snapshot
        ) == ("BEGIN DEFERRED")


class _SqliteError(Exception):
    def __init__(self, name: str | None, code: int | None = None) -> None:
        super().__init__(name)
        self.sqlite_errorname = name
        self.sqlite_errorcode = code


@pytest.mark.parametrize(
    ("name", "code", "kind"),
    [
        ("SQLITE_BUSY", 5, "lock_not_available"),
        ("SQLITE_LOCKED", 6, "lock_not_available"),
        ("SQLITE_BUSY_SNAPSHOT", 517, "serialization_failure"),
        ("SQLITE_CONSTRAINT_UNIQUE", 2067, "unique_violation"),
        ("SQLITE_CONSTRAINT_PRIMARYKEY", 1555, "unique_violation"),
        ("SQLITE_CONSTRAINT_NOTNULL", 1299, "other"),
        (None, 5, "lock_not_available"),
        (None, 517, "serialization_failure"),
        (None, 2067, "unique_violation"),
    ],
)
def test_sqlite_errors_are_classified(name: str | None, code: int, kind: str) -> None:
    wrapped = exc.OperationalError("stmt", {}, _SqliteError(name, code))
    assert SQLITE.classify_error(wrapped) == kind


def test_startup_refuses_an_in_memory_database_build_free(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    from types import SimpleNamespace

    from sandloom.core import app_factory

    def _load(database: str) -> None:
        instances = {"lite": SimpleNamespace(options={"url": database})}
        monkeypatch.setattr(
            app_factory, "load_infra_config", lambda _cfg: {"database": instances}
        )

    cfg = SimpleNamespace(database_url="postgresql+asyncpg://u:p@h/db")
    _load("sqlite+aiosqlite:///:memory:")
    problem = app_factory.dbapi_config_problem(cfg)
    assert problem is not None and "database/lite" in problem
    assert "SANDLOOM-B032" in problem
    # one of the checks every entry point runs (enforce_configuration)
    assert "dbapi_config_problem" in (
        app_factory.configuration_problems.__code__.co_names
    )
    _load(url)
    assert app_factory.dbapi_config_problem(cfg) is None


def test_b032_and_c029_are_catalogued() -> None:
    from sandloom.core import error_codes

    entry = error_codes.lookup("SANDLOOM-B032")
    assert entry is not None and entry.slug == "sqlite-unavailable"
    assert error_codes.EXCEPTION_CODES["SqliteUnavailable"] == "SANDLOOM-B032"
    check = error_codes.lookup("SANDLOOM-C029")
    assert check is not None and check.slug == "utc-datetime-columns"


# -- review fixes ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_nested_writer_of_the_same_task_is_refused_at_once(
    factory: Any,
) -> None:
    """A unit holding the write lock, then a fresh unit in the same task (a
    local contract call's own unit): refused at its BEGIN, not after
    busy_timeout."""
    async with factory() as outer:
        await _count(outer.session)  # BEGIN IMMEDIATE: the write lock
        started = asyncio.get_running_loop().time()
        with pytest.raises(NestedWriterRefused, match="SANDLOOM-R016"):
            async with factory() as inner:
                await inner.session.execute(insert(ITEMS).values(id=2))
        assert asyncio.get_running_loop().time() - started < 1.0
        await outer.session.execute(insert(ITEMS).values(id=1))
    # Once the outer unit ended, the same task opens units again.
    async with factory() as after:
        await after.session.execute(insert(ITEMS).values(id=3))
    async with factory() as uow:
        ids = (await uow.session.execute(select(ITEMS.c.id))).scalars().all()
    assert sorted(ids) == [1, 3]


@pytest.mark.asyncio
async def test_a_read_only_unit_inside_a_writer_is_not_refused(
    factory: Any, read_only: async_sessionmaker[AsyncSession]
) -> None:
    async with factory() as outer:
        await outer.session.execute(insert(ITEMS).values(id=1))
        async with read_only() as reader:
            assert await _count(reader) == 0  # deferred: no write lock


@pytest.fixture
def one_connection(monkeypatch: pytest.MonkeyPatch, url: str) -> Iterator[None]:
    monkeypatch.setattr(settings, "db_pool_size", 1)
    monkeypatch.setattr(settings, "db_max_overflow", 0)
    yield


@pytest.mark.asyncio
@pytest.mark.usefixtures("one_connection")
async def test_a_failed_read_only_unit_then_a_writer_on_one_connection(
    url: str, engine: AsyncEngine
) -> None:
    lone = driver._build_engine(url)
    try:
        options = transaction_options_for(lone, read_only=True, snapshot=None)
        reading = lone.execution_options(**options)
        async with AsyncSession(reading) as session:
            with pytest.raises(exc.OperationalError, match="readonly"):
                await session.execute(insert(ITEMS).values(id=1))
        # Rolled back, the pragma still on: the next BEGIN turns it off.
        async with AsyncSession(lone) as session, session.begin():
            await session.execute(insert(ITEMS).values(id=2))
        async with AsyncSession(lone) as session:
            assert await _count(session) == 1
    finally:
        await lone.dispose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("one_connection")
async def test_a_stale_write_lock_mark_is_dropped_at_begin(
    url: str, engine: AsyncEngine
) -> None:
    lone = driver._build_engine(url)
    try:
        async with lone.connect() as conn:
            conn.info[WRITE_LOCK_HELD] = True  # an end event that never ran
            await conn.rollback()
        options = transaction_options_for(lone, read_only=True, snapshot=None)
        async with AsyncSession(lone.execution_options(**options)) as session:
            with pytest.raises(UnsupportedSqlForm, match="SANDLOOM-R015"):
                await transaction_lock(7, session=session)
        view = lone.execution_options(isolation_level="AUTOCOMMIT")
        async with view.connect() as conn:
            conn.info[WRITE_LOCK_HELD] = True
            await conn.execute(text("SELECT 1"))
            assert WRITE_LOCK_HELD not in conn.info
    finally:
        await lone.dispose()


@pytest.mark.asyncio
async def test_the_in_memory_test_engine_has_no_transaction_lock() -> None:
    eng = driver.build_disposable_engine("sqlite+aiosqlite://")
    try:
        async with AsyncSession(eng) as session, session.begin():
            with pytest.raises(UnsupportedSqlForm, match="SANDLOOM-R015"):
                await transaction_lock(7, session=session)
    finally:
        await eng.dispose()
