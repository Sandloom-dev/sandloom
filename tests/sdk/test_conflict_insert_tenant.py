"""``conflict_insert`` on a TenantScoped model stays a refused bulk INSERT.

The portable upsert of a tenant-scoped model is not admitted by the tenant
guard: under a bound tenant it raises ``UnsafeTenantOperationError`` (in both
forms, ``DO NOTHING`` and ``DO UPDATE``), and only explicit ``unscoped()``
system work may run it. The guard refuses before any statement reaches the
database, so nothing here connects.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core.di import UnsafeTenantOperationError, bind_tenant
from sandloom.core.orm import TenantScoped
from sandloom.core.testing import reload_test_env
from sandloom.sdk.data import conflict_insert


class _Base(DeclarativeBase):
    pass


class _Item(TenantScoped, _Base):
    __tablename__ = "test_conflict_tenant_items"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    env_test = Path(__file__).resolve().parents[2] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest.mark.asyncio
async def test_conflict_insert_on_a_tenant_scoped_model_is_refused() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.fail("DATABASE_URL not set: the guard test needs a PostgreSQL URL")
    engine = create_async_engine(database_url)
    try:
        async with AsyncSession(engine) as session:
            do_nothing = (
                conflict_insert(_Item, session=session)
                .values(id=uuid.uuid4(), name="x")
                .on_conflict_do_nothing(index_elements=["id"])
            )
            do_update = (
                conflict_insert(_Item, session=session)
                .values(id=uuid.uuid4(), name="x")
                .on_conflict_do_update(index_elements=["id"], set_={"name": "y"})
            )
            with bind_tenant(uuid.uuid4()):
                for statement in (do_nothing, do_update):
                    with pytest.raises(UnsafeTenantOperationError, match="bulk INSERT"):
                        await session.execute(statement)
    finally:
        await engine.dispose()
