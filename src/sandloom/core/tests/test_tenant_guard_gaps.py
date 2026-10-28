"""Tenant guard gaps (M1/M2) against real PostgreSQL.

M1: with NO tenant bound the guard must fail closed whenever a TenantScoped
table is reachable from the statement — relationship loads (selectin / lazy /
joined), relationship joins, EXISTS / IN subqueries, select_from — not only when
it is a top-level entity. With a tenant bound, relationship loads are scoped.

M2: bulk UPDATE must not SET tenant_id through ``ordered_values`` either, ORM
bulk UPDATE by primary key (unscopable executemany) is refused, and
``Session.get`` identity-map hits are re-checked against the bound tenant.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKey, Text, exists, select, update
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    joinedload,
    mapped_column,
    relationship,
    selectinload,
)

from sandloom.core.di import (
    MissingTenantError,
    UnsafeTenantOperationError,
    bind_tenant,
    unscoped,
)
from sandloom.core.orm import TenantScoped
from sandloom.core.testing import reload_test_env


class _Base(AsyncAttrs, DeclarativeBase):
    pass


class _Folder(_Base):
    """NOT tenant-scoped (e.g. a shared/global entity)."""

    __tablename__ = "test_tg_folders"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    docs: Mapped[list[_Doc]] = relationship(back_populates="folder")


class _Doc(TenantScoped, _Base):
    __tablename__ = "test_tg_docs"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    folder_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("test_tg_folders.id"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    folder: Mapped[_Folder] = relationship(back_populates="docs")


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def docs() -> AsyncGenerator[dict[str, Any], None]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not set")
    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
        await conn.run_sync(_Base.metadata.create_all)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    folder = _Folder(name="shared")
    doc_b = _Doc(tenant_id=tenant_b, name="b-secret", folder=folder)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    with unscoped():
        async with sm() as session:
            session.add_all(
                [folder, _Doc(tenant_id=tenant_a, name="a-doc", folder=folder), doc_b]
            )
            await session.commit()
    yield {
        "sm": sm,
        "a": tenant_a,
        "b": tenant_b,
        "folder_id": folder.id,
        "doc_b_id": doc_b.id,
    }
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
    await engine.dispose()


# --------------------------------------------------------------------- M1 ---
@pytest.mark.asyncio
async def test_non_tenant_query_without_tenant_still_works(docs) -> None:
    async with docs["sm"]() as session:
        rows = (await session.execute(select(_Folder))).scalars().all()
    assert [r.name for r in rows] == ["shared"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build",
    [
        lambda: select(_Folder).options(selectinload(_Folder.docs)),
        lambda: select(_Folder).options(joinedload(_Folder.docs)),
        lambda: select(_Folder).join(_Folder.docs),
        lambda: select(_Folder).join(_Doc, _Doc.folder_id == _Folder.id),
        lambda: select(_Folder).where(exists().where(_Doc.folder_id == _Folder.id)),
        lambda: select(_Folder).where(_Folder.id.in_(select(_Doc.folder_id))),
        lambda: select(_Folder).where(_Folder.docs.any()),
        lambda: select(_Folder.id).select_from(_Doc).join(_Doc.folder),
    ],
    ids=[
        "selectinload",
        "joinedload",
        "relationship-join",
        "entity-join",
        "exists",
        "in-subquery",
        "any",
        "select_from",
    ],
)
async def test_unbound_reach_into_tenant_table_fails_closed(docs, build) -> None:
    async with docs["sm"]() as session:
        with pytest.raises(MissingTenantError):
            result = await session.execute(build())
            result.unique().scalars().all()


@pytest.mark.asyncio
async def test_unbound_lazy_relationship_load_fails_closed(docs) -> None:
    async with docs["sm"]() as session:
        folder = (await session.execute(select(_Folder))).scalars().one()
        with pytest.raises(MissingTenantError):
            await folder.awaitable_attrs.docs


@pytest.mark.asyncio
async def test_bound_relationship_loads_are_scoped(docs) -> None:
    with bind_tenant(docs["a"]):
        async with docs["sm"]() as session:
            folder = (
                (
                    await session.execute(
                        select(_Folder).options(selectinload(_Folder.docs))
                    )
                )
                .scalars()
                .one()
            )
            assert [d.name for d in folder.docs] == ["a-doc"]
        async with docs["sm"]() as session:
            folder = (await session.execute(select(_Folder))).scalars().one()
            lazy = await folder.awaitable_attrs.docs
            assert [d.name for d in lazy] == ["a-doc"]
        async with docs["sm"]() as session:
            joined = (
                (
                    await session.execute(
                        select(_Folder).options(joinedload(_Folder.docs))
                    )
                )
                .unique()
                .scalars()
                .one()
            )
            assert [d.name for d in joined.docs] == ["a-doc"]


# --------------------------------------------------------------------- M2 ---
@pytest.mark.asyncio
async def test_ordered_values_cannot_change_tenant_id(docs) -> None:
    with bind_tenant(docs["a"]):
        async with docs["sm"]() as session:
            with pytest.raises(UnsafeTenantOperationError):
                await session.execute(
                    update(_Doc).ordered_values((_Doc.tenant_id, docs["b"]))
                )


@pytest.mark.asyncio
async def test_bulk_update_by_primary_key_is_refused(docs) -> None:
    with bind_tenant(docs["a"]):
        async with docs["sm"]() as session:
            # Tenant A writing tenant B's row by id: must not go through.
            with pytest.raises(UnsafeTenantOperationError):
                await session.execute(
                    update(_Doc), [{"id": docs["doc_b_id"], "name": "pwned"}]
                )
            with pytest.raises(UnsafeTenantOperationError):
                await session.execute(
                    update(_Doc),
                    [{"id": docs["doc_b_id"], "tenant_id": docs["a"]}],
                )
    with unscoped():
        async with docs["sm"]() as session:
            doc_b = await session.get(_Doc, docs["doc_b_id"])
            assert doc_b is not None and doc_b.name == "b-secret"


@pytest.mark.asyncio
async def test_bulk_update_by_primary_key_allowed_when_unscoped(docs) -> None:
    with unscoped():
        async with docs["sm"]() as session:
            await session.execute(
                update(_Doc), [{"id": docs["doc_b_id"], "name": "system-fix"}]
            )
            await session.commit()


@pytest.mark.asyncio
async def test_session_get_identity_map_hit_is_rechecked(docs) -> None:
    async with docs["sm"]() as session:
        with unscoped():
            leaked = await session.get(_Doc, docs["doc_b_id"])
            assert leaked is not None  # system code legitimately loaded it
        # Same session, now bound to tenant A: the identity-map hit must not
        # hand tenant B's object out.
        with bind_tenant(docs["a"]):
            assert await session.get(_Doc, docs["doc_b_id"]) is None
        # No tenant bound: fail closed like the SQL path.
        with pytest.raises(MissingTenantError):
            await session.get(_Doc, docs["doc_b_id"])
        with bind_tenant(docs["b"]):
            own = await session.get(_Doc, docs["doc_b_id"])
            assert own is leaked
