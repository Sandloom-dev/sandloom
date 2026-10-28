"""Tests for the Cross-Module Query / Projection layer (``sandloom.core.projections``).

Covers the registry + transactional registration (pure), the consumer wiring
through the bus (in-memory), and the end-to-end consumer apply path against a
real PostgreSQL projection table: monotonic version-guarded upsert, exactly-once
via the durable inbox, and out-of-order tolerance.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import BigInteger, Column, MetaData, Table, Text, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sandloom.core.eventing.memory_bus import InMemoryEventBus
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import ModuleRegistrationConflict
from sandloom.core.projections import (
    ProjectionConsumerDescriptor,
    QueryProjectionDescriptor,
    QueryProjectionRegistry,
    make_consumer_callback,
    monotonic_upsert,
    start_projection_consumers,
)
from sandloom.core.projections.registry import ProjectionRegistryError
from sandloom.core.testing import reload_test_env


async def _noop_handler(session: object, envelope: object) -> None:  # pragma: no cover
    return None


# --------------------------------------------------------------------------- #
# Registry + transactional registration (pure, no I/O)
# --------------------------------------------------------------------------- #
def test_registry_register_and_duplicate() -> None:
    registry = QueryProjectionRegistry()
    registry.register_projection(
        QueryProjectionDescriptor(name="ptqi", owner_module="m")
    )
    with pytest.raises(ProjectionRegistryError):
        registry.register_projection(
            QueryProjectionDescriptor(name="ptqi", owner_module="m")
        )
    consumer = ProjectionConsumerDescriptor(
        name="ptqi@task",
        source_channel="tasks.task.updated.v1",
        projection="ptqi",
        handler=_noop_handler,
    )
    registry.register_consumer(consumer)
    assert registry.consumers_for("ptqi") == [consumer]
    with pytest.raises(ProjectionRegistryError):
        registry.register_consumer(consumer)


def test_registry_snapshot_restore_rewinds() -> None:
    registry = QueryProjectionRegistry()
    registry.register_projection(QueryProjectionDescriptor(name="a", owner_module="m"))
    snapshot = registry.snapshot()
    registry.register_projection(QueryProjectionDescriptor(name="b", owner_module="m"))
    assert {p.name for p in registry.list_projections()} == {"a", "b"}
    registry.restore(snapshot)
    assert [p.name for p in registry.list_projections()] == ["a"]


def test_module_registration_applies_projections(core_facade: CoreFacade) -> None:
    with core_facade.module_registration("read_mod") as api:
        api.register_query_projection(
            QueryProjectionDescriptor(name="ptqi", owner_module="read_mod")
        )
        api.register_projection_consumer(
            ProjectionConsumerDescriptor(
                name="ptqi@task",
                source_channel="tasks.task.updated.v1",
                projection="ptqi",
                handler=_noop_handler,
            )
        )
    assert core_facade.projection_registry.get_projection("ptqi") is not None
    assert core_facade.projection_registry.get_consumer("ptqi@task") is not None


def test_module_registration_rejects_consumer_for_unknown_projection(
    core_facade: CoreFacade,
) -> None:
    with pytest.raises(ModuleRegistrationConflict):
        with core_facade.module_registration("read_mod") as api:
            api.register_projection_consumer(
                ProjectionConsumerDescriptor(
                    name="orphan",
                    source_channel="tasks.task.updated.v1",
                    projection="does_not_exist",
                    handler=_noop_handler,
                )
            )
    # Nothing leaked into the live registry.
    assert core_facade.projection_registry.get_consumer("orphan") is None


def test_projection_consumer_group_is_namespaced_by_module(
    core_facade: CoreFacade,
) -> None:
    with core_facade.module_registration("read_mod") as api:
        api.register_query_projection(
            QueryProjectionDescriptor(name="ptqi", owner_module="read_mod")
        )
        api.register_projection_consumer(
            ProjectionConsumerDescriptor(
                name="ptqi@task",
                source_channel="tasks.task.updated.v1",
                projection="ptqi",
                handler=_noop_handler,
            )
        )
    consumer = core_facade.projection_registry.get_consumer("read_mod:ptqi@task")
    assert consumer is not None and consumer.group_name == "read_mod:ptqi@task"


@pytest.mark.asyncio
async def test_rebuild_cli_does_not_start_consumers_or_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M3: ``sandloom projection rebuild`` must not join live consumer groups.

    Entering the ASGI lifespan started the ConsumerRuntime (stealing events
    from the running service's groups) and, in prod, the outbox relay. The CLI
    runs the startup tasks only.
    """
    from types import SimpleNamespace

    from fastapi import FastAPI

    from sandloom.core.cli import projections as cli
    from sandloom.core.projections import ProjectionRebuildResult

    core = CoreFacade(app=FastAPI())
    started: list[str] = []

    async def _startup() -> None:
        started.append("startup")

    core.add_startup_task(_startup)
    core.register_query_projection(
        QueryProjectionDescriptor(name="ptqi", owner_module="read_mod")
    )

    class _Rebuilder:
        async def rebuild_all(self) -> ProjectionRebuildResult:
            started.append("rebuild")
            return ProjectionRebuildResult(projection="ptqi", rows=3)

        async def rebuild_tenant(self, _tenant: object) -> ProjectionRebuildResult:
            raise AssertionError("not called")

    core.register_projection_rebuilder("ptqi", _Rebuilder())

    @asynccontextmanager
    async def _lifespan(_app: object) -> AsyncGenerator[None, None]:
        raise AssertionError("rebuild entered the ASGI lifespan (consumers/relay)")
        yield  # pragma: no cover

    fake_app = SimpleNamespace(
        state=SimpleNamespace(core=core),
        router=SimpleNamespace(lifespan_context=_lifespan),
    )
    monkeypatch.setattr(cli, "_build_app", lambda: fake_app)
    import sandloom.core.projections.operations as ops
    from sandloom.core.projections.purge import MemoryWatermarkStore

    marks = MemoryWatermarkStore()
    monkeypatch.setattr(ops, "watermark_store", lambda _storage: marks)
    assert await cli._run_rebuild("ptqi", None) == 0
    assert (await marks.get("ptqi")).rebuild_started_at is None  # marked, cleared
    assert started == ["startup", "rebuild"]


# --------------------------------------------------------------------------- #
# Consumer wiring through the bus (in-memory, no DB)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_start_projection_consumers_wires_and_delivers() -> None:
    from sandloom.core.tests._schemas import PermissiveSchemaRegistry

    registry = PermissiveSchemaRegistry()
    core = CoreFacade(app=__import__("fastapi").FastAPI())
    core.register_schema_registry(registry)
    bus = InMemoryEventBus(registry)
    core.set_event_bus(bus)

    received: list[dict] = []

    async def handler(session: object, envelope: dict) -> None:
        received.append(dict(envelope["payload"]))

    core.register_query_projection(
        QueryProjectionDescriptor(name="p", owner_module="m")
    )
    core.register_projection_consumer(
        ProjectionConsumerDescriptor(
            name="p@evt",
            source_channel="demo.thing.updated.v1",
            projection="p",
            handler=handler,
            durable=False,
        )
    )

    @asynccontextmanager
    async def opener(_name: str) -> AsyncGenerator[object, None]:
        yield object()

    wired = await start_projection_consumers(core, session_opener=opener)
    assert wired == 1

    await bus.publish_event("demo.thing.updated.v1", {"hello": "world"}, "v1")
    await bus.drain()
    assert received == [{"hello": "world"}]
    await core.app.state.consumer_runtime.stop()


# --------------------------------------------------------------------------- #
# End-to-end consumer apply against real PostgreSQL
# --------------------------------------------------------------------------- #
_PROJ_METADATA = MetaData()
_PTQI = Table(
    "test_project_task_query_index",
    _PROJ_METADATA,
    Column("task_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
    Column("task_title", Text, nullable=False),
    Column("task_status", Text, nullable=False),
    Column("task_version", BigInteger, nullable=False),
)


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    backend_dir = Path(__file__).resolve().parents[4]
    env_test = backend_dir / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def projection_db() -> AsyncGenerator[async_sessionmaker, None]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not set; skipping DB-backed projection tests")
    from sandloom.core.sql_alchemy.models.consumer_inbox import ConsumerInboxEntry

    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_PROJ_METADATA.drop_all)
        await conn.run_sync(_PROJ_METADATA.create_all)
        await conn.run_sync(ConsumerInboxEntry.__table__.create, checkfirst=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    # Isolate the inbox rows this test creates.
    async with sessionmaker() as cleanup:
        from sqlalchemy import delete

        await cleanup.execute(
            delete(ConsumerInboxEntry).where(
                ConsumerInboxEntry.consumer == "test.ptqi@task"
            )
        )
        await cleanup.commit()
    yield sessionmaker
    async with engine.begin() as conn:
        await conn.run_sync(_PROJ_METADATA.drop_all)
    await engine.dispose()


def _task_handler():
    async def handler(session, envelope) -> None:
        p = envelope["payload"]
        await monotonic_upsert(
            session,
            _PTQI,
            key={"task_id": uuid.UUID(p["task_id"])},
            values={
                "tenant_id": uuid.UUID(p["tenant_id"]),
                "task_title": p["title"],
                "task_status": p["status"],
            },
            version_column="task_version",
            incoming_version=p["version"],
        )

    return handler


def _envelope(event_id: str, payload: dict) -> dict:
    return {"id": event_id, "channel": "tasks.task.updated.v1", "payload": payload}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_consumer_applies_and_is_monotonic_and_idempotent(
    projection_db: async_sessionmaker,
) -> None:
    @asynccontextmanager
    async def opener(_name: str) -> AsyncGenerator[object, None]:
        async with projection_db() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    descriptor = ProjectionConsumerDescriptor(
        name="test.ptqi@task",
        source_channel="tasks.task.updated.v1",
        projection="ptqi",
        handler=_task_handler(),
        durable=True,
    )
    callback = make_consumer_callback(descriptor, "default", opener)

    task_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    def payload(version: int, status: str) -> dict:
        return {
            "task_id": str(task_id),
            "tenant_id": str(tenant_id),
            "title": "Do the thing",
            "status": status,
            "version": version,
        }

    # First apply (v2).
    await callback(_envelope("evt-v2", payload(2, "active")))
    # Out-of-order older event (v1) must NOT roll the row back.
    await callback(_envelope("evt-v1", payload(1, "stale")))
    # Duplicate of v2 (same event id) must be skipped by the durable inbox.
    await callback(_envelope("evt-v2", payload(2, "active")))
    # Newer event (v3) advances the row.
    await callback(_envelope("evt-v3", payload(3, "done")))

    async with projection_db() as session:
        row = (
            (await session.execute(select(_PTQI).where(_PTQI.c.task_id == task_id)))
            .mappings()
            .one()
        )
    assert row["task_version"] == 3
    assert row["task_status"] == "done"
