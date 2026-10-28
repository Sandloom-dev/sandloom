"""Projection write helpers: NULL versions and tenant ownership (Low items)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import BigInteger, Column, DateTime, MetaData, Table, Text, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sandloom.core.projections import (
    monotonic_delete,
    monotonic_update,
    monotonic_upsert,
)
from sandloom.core.tenant_context import TenantMismatchError, bind_tenant
from sandloom.core.testing import reload_test_env

_MD = MetaData()
_ROWS = Table(
    "test_projection_guard_rows",
    _MD,
    Column("item_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", PG_UUID(as_uuid=True), nullable=False),
    Column("group_id", PG_UUID(as_uuid=True), nullable=False),
    Column("title", Text, nullable=False),
    Column("group_name", Text, nullable=True),
    Column("item_version", BigInteger, nullable=True),
    Column("group_version", BigInteger, nullable=True),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
)


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def sm() -> AsyncGenerator[async_sessionmaker, None]:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    engine = create_async_engine(url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_MD.drop_all)
        await conn.run_sync(_MD.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(_MD.drop_all)
    await engine.dispose()


async def _row(sm: async_sessionmaker, item_id: uuid.UUID):
    async with sm() as s:
        return (await s.execute(select(_ROWS).where(_ROWS.c.item_id == item_id))).one()


@pytest.mark.asyncio
async def test_null_stored_version_is_updatable(sm: async_sessionmaker) -> None:
    tenant, group, item = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sm() as s, s.begin():
        # Seeded without a group version (NULL): the first group event must
        # apply, not be blocked forever by NULL < v being NULL.
        await monotonic_upsert(
            s,
            _ROWS,
            key={"item_id": item},
            values={"tenant_id": tenant, "group_id": group, "title": "t"},
            version_column="item_version",
            incoming_version=1,
        )
    async with sm() as s, s.begin():
        assert (
            await monotonic_update(
                s,
                _ROWS,
                match={"group_id": group},
                values={"group_name": "G1"},
                version_column="group_version",
                incoming_version=3,
            )
            == 1
        )
    assert (await _row(sm, item)).group_name == "G1"

    # NULL item_version row: an upsert must apply too.
    async with sm() as s, s.begin():
        await s.execute(_ROWS.update().values(item_version=None))
        assert await monotonic_upsert(
            s,
            _ROWS,
            key={"item_id": item},
            values={"tenant_id": tenant, "group_id": group, "title": "t2"},
            version_column="item_version",
            incoming_version=2,
        )
    assert (await _row(sm, item)).title == "t2"


@pytest.mark.asyncio
async def test_tombstone_prevents_old_rebuild_from_resurrecting_deleted_entity(
    sm: async_sessionmaker,
) -> None:
    tenant, group, item = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seed = {"tenant_id": tenant, "group_id": group, "title": "deleted"}
    # Deletion can arrive before even the first create/backfill for the entity.
    with bind_tenant(tenant):
        async with sm() as session, session.begin():
            assert await monotonic_delete(
                session,
                _ROWS,
                key={"item_id": item},
                values=seed,
                version_column="item_version",
                incoming_version=3,
            )
        async with sm() as session, session.begin():
            assert not await monotonic_upsert(
                session,
                _ROWS,
                key={"item_id": item},
                values={**seed, "title": "stale rebuild", "deleted_at": None},
                version_column="item_version",
                incoming_version=2,
            )
        async with sm() as session, session.begin():
            assert not await monotonic_delete(
                session,
                _ROWS,
                key={"item_id": item},
                values=seed,
                version_column="item_version",
                incoming_version=3,
            )
    row = await _row(sm, item)
    assert (
        row.deleted_at is not None and row.item_version == 3 and row.title == "deleted"
    )


@pytest.mark.asyncio
async def test_upsert_requires_tenant_and_never_crosses_tenants(
    sm: async_sessionmaker,
) -> None:
    tenant_a, tenant_b, group, item = (uuid.uuid4() for _ in range(4))
    async with sm() as s, s.begin():
        with pytest.raises(ValueError, match="tenant_id"):
            await monotonic_upsert(
                s,
                _ROWS,
                key={"item_id": item},
                values={"group_id": group, "title": "x"},
                version_column="item_version",
                incoming_version=1,
            )
    async with sm() as s, s.begin():
        await monotonic_upsert(
            s,
            _ROWS,
            key={"item_id": item},
            values={"tenant_id": tenant_a, "group_id": group, "title": "a"},
            version_column="item_version",
            incoming_version=1,
        )
    # Same key, other tenant: must NOT overwrite tenant A's row.
    async with sm() as s, s.begin():
        changed = await monotonic_upsert(
            s,
            _ROWS,
            key={"item_id": item},
            values={"tenant_id": tenant_b, "group_id": group, "title": "b"},
            version_column="item_version",
            incoming_version=9,
        )
        assert changed is False
    row = await _row(sm, item)
    assert (row.tenant_id, row.title) == (tenant_a, "a")

    # A bound tenant cannot write another tenant's projection row.
    with bind_tenant(tenant_b):
        async with sm() as s, s.begin():
            with pytest.raises(TenantMismatchError):
                await monotonic_upsert(
                    s,
                    _ROWS,
                    key={"item_id": uuid.uuid4()},
                    values={"tenant_id": tenant_a, "group_id": group, "title": "z"},
                    version_column="item_version",
                    incoming_version=1,
                )


@pytest.mark.asyncio
async def test_update_is_scoped_to_bound_tenant(sm: async_sessionmaker) -> None:
    tenant_a, tenant_b, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    item_a, item_b = uuid.uuid4(), uuid.uuid4()
    async with sm() as s, s.begin():
        for item, tenant in ((item_a, tenant_a), (item_b, tenant_b)):
            await monotonic_upsert(
                s,
                _ROWS,
                key={"item_id": item},
                values={"tenant_id": tenant, "group_id": group, "title": "t"},
                version_column="item_version",
                incoming_version=1,
            )
    with bind_tenant(tenant_a):
        async with sm() as s, s.begin():
            updated = await monotonic_update(
                s,
                _ROWS,
                match={"group_id": group},
                values={"group_name": "only-a"},
                version_column="group_version",
                incoming_version=1,
            )
    assert updated == 1
    assert (await _row(sm, item_a)).group_name == "only-a"
    assert (await _row(sm, item_b)).group_name is None
    with bind_tenant(tenant_a):
        async with sm() as s, s.begin():
            with pytest.raises(TenantMismatchError):
                await monotonic_update(
                    s,
                    _ROWS,
                    match={"tenant_id": tenant_b},
                    values={"group_name": "forbidden"},
                    version_column="group_version",
                    incoming_version=2,
                )
    assert (await _row(sm, item_b)).group_name is None
