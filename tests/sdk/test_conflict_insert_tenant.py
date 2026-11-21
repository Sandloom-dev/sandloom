"""``conflict_insert`` on a TenantScoped model, against real PostgreSQL.

With a tenant bound, the upsert is admitted for that tenant's rows only: an
absent ``tenant_id`` is set, the bound tenant's own is kept, another tenant's
is refused, and the ``DO UPDATE`` can never touch another tenant's row. With no
tenant bound it is refused. A plain INSERT of a TenantScoped model stays
forbidden.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import Text, insert, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core.di import UnsafeTenantOperationError, bind_tenant, unscoped
from sandloom.core.orm import TenantScoped
from sandloom.core.testing import reload_test_env
from sandloom.sdk.data import conflict_insert


class _Base(DeclarativeBase):
    pass


class _Item(TenantScoped, _Base):
    """Keyed on ``id`` alone: a conflict can reach another tenant's row."""

    __tablename__ = "test_conflict_tenant_items"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[2] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def items() -> AsyncGenerator[
    tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID, uuid.UUID], None
]:
    """(sessions, tenant A, tenant B, id of A's existing row ``a-original``)."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.fail("DATABASE_URL not set: the tenant upsert tests need PostgreSQL")
    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
        await conn.run_sync(_Base.metadata.create_all)
    tenant_a, tenant_b, a_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    with unscoped():
        async with sessions() as session:
            session.add(_Item(id=a_id, tenant_id=tenant_a, name="a-original"))
            await session.commit()
    yield sessions, tenant_a, tenant_b, a_id
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
    await engine.dispose()


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> dict[uuid.UUID, tuple]:
    with unscoped():
        async with sessions() as session:
            found = (await session.execute(select(_Item))).scalars().all()
    return {row.id: (row.tenant_id, row.name) for row in found}


def _upsert(session: AsyncSession, **values: object):
    return (
        conflict_insert(_Item, session=session)
        .values(**values)
        .on_conflict_do_update(index_elements=["id"], set_={"name": "set-by-upsert"})
    )


@pytest.mark.asyncio
async def test_upsert_without_tenant_id_inserts_then_updates_for_the_bound_tenant(
    items,
) -> None:
    sessions, tenant_a, _, _ = items
    new_id = uuid.uuid4()
    with bind_tenant(tenant_a):
        async with sessions() as session:
            await session.execute(_upsert(session, id=new_id, name="first"))
            await session.commit()
        assert (await _rows(sessions))[new_id] == (tenant_a, "first")
        async with sessions() as session:
            await session.execute(_upsert(session, id=new_id, name="second"))
            await session.commit()
    assert (await _rows(sessions))[new_id] == (tenant_a, "set-by-upsert")


@pytest.mark.asyncio
async def test_upsert_with_the_bound_tenants_own_id_is_admitted(items) -> None:
    sessions, tenant_a, _, _ = items
    new_id = uuid.uuid4()
    with bind_tenant(tenant_a):
        async with sessions() as session:
            await session.execute(
                _upsert(session, id=new_id, tenant_id=tenant_a, name="own")
            )
            await session.commit()
    assert (await _rows(sessions))[new_id] == (tenant_a, "own")


@pytest.mark.asyncio
async def test_upsert_naming_another_tenant_is_refused(items) -> None:
    sessions, tenant_a, tenant_b, _ = items
    new_id = uuid.uuid4()
    with bind_tenant(tenant_a):
        async with sessions() as session:
            with pytest.raises(UnsafeTenantOperationError, match="not the bound"):
                await session.execute(
                    _upsert(session, id=new_id, tenant_id=tenant_b, name="foreign")
                )
    assert new_id not in await _rows(sessions)


@pytest.mark.asyncio
async def test_a_multi_row_upsert_with_one_foreign_row_is_refused_whole(items) -> None:
    sessions, tenant_a, tenant_b, _ = items
    ids = [uuid.uuid4(), uuid.uuid4()]
    rows = [
        {"id": ids[0], "name": "mine"},
        {"id": ids[1], "tenant_id": tenant_b, "name": "foreign"},
    ]
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = (
                conflict_insert(_Item, session=session)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            with pytest.raises(UnsafeTenantOperationError, match="not the bound"):
                await session.execute(statement)
    assert not set(ids) & set(await _rows(sessions))


@pytest.mark.asyncio
async def test_execution_parameters_are_checked_and_filled_per_row(items) -> None:
    sessions, tenant_a, tenant_b, _ = items
    mine, foreign = uuid.uuid4(), uuid.uuid4()
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = conflict_insert(_Item, session=session).on_conflict_do_nothing(
                index_elements=["id"]
            )
            with pytest.raises(UnsafeTenantOperationError, match="not the bound"):
                await session.execute(
                    statement,
                    [
                        {"id": mine, "name": "mine"},
                        {"id": foreign, "tenant_id": tenant_b, "name": "foreign"},
                    ],
                )
        async with sessions() as session:
            await session.execute(statement, [{"id": mine, "name": "mine"}])
            await session.commit()
    found = await _rows(sessions)
    assert found[mine] == (tenant_a, "mine")
    assert foreign not in found


@pytest.mark.asyncio
async def test_a_conflict_with_another_tenants_row_does_not_update_it(items) -> None:
    sessions, _, tenant_b, a_id = items
    with bind_tenant(tenant_b):
        async with sessions() as session:
            await session.execute(_upsert(session, id=a_id, name="b-takeover"))
            await session.commit()
    # Tenant A's row is untouched and still A's; tenant B gained no row.
    assert await _rows(sessions) == {a_id: (items[1], "a-original")}


@pytest.mark.asyncio
async def test_do_nothing_is_admitted_and_skips_a_conflict(items) -> None:
    sessions, tenant_a, _, a_id = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            await session.execute(
                conflict_insert(_Item, session=session)
                .values(id=a_id, name="ignored")
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.commit()
    assert (await _rows(sessions))[a_id] == (tenant_a, "a-original")


@pytest.mark.asyncio
async def test_a_caller_where_is_kept_beside_the_tenant_scope(items) -> None:
    sessions, tenant_a, _, a_id = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            await session.execute(
                conflict_insert(_Item, session=session)
                .values(id=a_id, name="x")
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={"name": "never"},
                    where=_Item.name == "not-a-match",
                )
            )
            await session.commit()
    assert (await _rows(sessions))[a_id] == (tenant_a, "a-original")


@pytest.mark.asyncio
async def test_upsert_with_no_tenant_bound_is_refused(items) -> None:
    sessions, _, _, _ = items
    async with sessions() as session:
        with pytest.raises(UnsafeTenantOperationError, match="no tenant bound"):
            await session.execute(_upsert(session, id=uuid.uuid4(), name="orphan"))


@pytest.mark.asyncio
async def test_upsert_under_unscoped_is_untouched_by_the_guard(items) -> None:
    sessions, tenant_a, tenant_b, a_id = items
    with unscoped():
        async with sessions() as session:
            await session.execute(
                _upsert(session, id=a_id, tenant_id=tenant_b, name="system")
            )
            await session.commit()
    assert (await _rows(sessions))[a_id] == (tenant_a, "set-by-upsert")


@pytest.mark.asyncio
async def test_set_cannot_change_tenant_id(items) -> None:
    sessions, tenant_a, tenant_b, a_id = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = (
                conflict_insert(_Item, session=session)
                .values(id=a_id, name="x")
                .on_conflict_do_update(
                    index_elements=["id"], set_={"tenant_id": tenant_b}
                )
            )
            with pytest.raises(UnsafeTenantOperationError, match="tenant_id"):
                await session.execute(statement)


@pytest.mark.asyncio
async def test_a_subquery_in_the_upsert_is_refused(items) -> None:
    sessions, tenant_a, _, a_id = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            leak = select(_Item.name).where(_Item.id == a_id).scalar_subquery()
            statement = (
                conflict_insert(_Item, session=session)
                .values(id=uuid.uuid4(), name="x")
                .on_conflict_do_update(index_elements=["id"], set_={"name": leak})
            )
            with pytest.raises(UnsafeTenantOperationError, match="SELECT"):
                await session.execute(statement)


@pytest.mark.asyncio
async def test_from_select_is_refused(items) -> None:
    sessions, tenant_a, _, _ = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = (
                conflict_insert(_Item, session=session)
                .from_select(["id", "name"], select(_Item.id, _Item.name))
                .on_conflict_do_nothing(index_elements=["id"])
            )
            with pytest.raises(UnsafeTenantOperationError, match="SELECT"):
                await session.execute(statement)


@pytest.mark.asyncio
async def test_the_table_form_of_a_tenant_model_stays_refused(items) -> None:
    sessions, tenant_a, _, _ = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            statement = (
                conflict_insert(_Item.__table__, session=session)
                .values(id=uuid.uuid4(), name="x")
                .on_conflict_do_nothing(index_elements=["id"])
            )
            with pytest.raises(UnsafeTenantOperationError, match="Core statements"):
                await session.execute(statement)


@pytest.mark.asyncio
async def test_a_plain_insert_stays_forbidden(items) -> None:
    sessions, tenant_a, _, _ = items
    with bind_tenant(tenant_a):
        async with sessions() as session:
            with pytest.raises(UnsafeTenantOperationError, match="bulk INSERT"):
                await session.execute(insert(_Item).values(id=uuid.uuid4(), name="x"))
            plain = conflict_insert(_Item, session=session).values(
                id=uuid.uuid4(), name="x"
            )
            with pytest.raises(UnsafeTenantOperationError, match="bulk INSERT"):
                await session.execute(plain)
