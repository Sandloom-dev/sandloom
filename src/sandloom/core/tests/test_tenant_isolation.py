"""Tenant-isolation tests (sandloom.core.tenant) against real PostgreSQL.

Proves the three guarantees: fail-closed explicit scoping, automatic filtering of
TenantScoped models to the bound tenant, and the explicit unscoped() escape.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Text, delete, insert, select, update
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sandloom.core.config.settings import Settings
from sandloom.core.di import (
    MissingTenantError,
    TenantMismatchError,
    TenantScope,
    UnsafeTenantOperationError,
    bind_tenant,
    create_access_token,
    scoped,
    unscoped,
)
from sandloom.core.orm import TenantScoped
from sandloom.core.tenant_context import configured_tenant_scope, current_tenant_id
from sandloom.core.testing import reload_test_env


class _Base(DeclarativeBase):
    pass


class _Widget(TenantScoped, _Base):
    __tablename__ = "test_tenant_widgets"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)


def _tenant_app(
    *, tenancy_mode: str = "multi", single_tenant_id: uuid.UUID | None = None
) -> FastAPI:
    app = FastAPI()
    app.state.core = SimpleNamespace(
        settings=SimpleNamespace(
            tenancy_mode=tenancy_mode,
            single_tenant_id=single_tenant_id,
        )
    )

    @app.get("/tenant")
    async def tenant_endpoint(
        tenant_id: Annotated[uuid.UUID | None, TenantScope()],
    ) -> dict[str, str | None]:
        return {"tenant_id": str(tenant_id) if tenant_id else None}

    return app


@pytest.fixture(scope="module", autouse=True)
def _load_env() -> None:
    backend_dir = Path(__file__).resolve().parents[4]
    env_test = backend_dir / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def widgets() -> AsyncGenerator[
    tuple[async_sessionmaker, uuid.UUID, uuid.UUID], None
]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not set; skipping tenant-isolation DB tests")
    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
        await conn.run_sync(_Base.metadata.create_all)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    with unscoped():
        async with sessionmaker() as session:
            session.add_all(
                [
                    _Widget(tenant_id=tenant_a, name="a1"),
                    _Widget(tenant_id=tenant_a, name="a2"),
                    _Widget(tenant_id=tenant_b, name="b1"),
                ]
            )
            await session.commit()
    yield sessionmaker, tenant_a, tenant_b
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_automatic_filtering_to_bound_tenant(widgets) -> None:
    sessionmaker, tenant_a, _tenant_b = widgets
    with bind_tenant(tenant_a):
        async with sessionmaker() as session:
            rows = (await session.execute(select(_Widget))).scalars().all()
    assert {r.name for r in rows} == {"a1", "a2"}  # tenant B invisible


@pytest.mark.asyncio
async def test_unscoped_sees_all_tenants(widgets) -> None:
    sessionmaker, tenant_a, _ = widgets
    with bind_tenant(tenant_a), unscoped():
        async with sessionmaker() as session:
            rows = (await session.execute(select(_Widget))).scalars().all()
    assert {r.name for r in rows} == {"a1", "a2", "b1"}


@pytest.mark.asyncio
async def test_explicit_scoped_is_fail_closed(widgets) -> None:
    sessionmaker, _tenant_a, tenant_b = widgets
    # No tenant bound -> explicit scoped() refuses rather than leaking.
    with pytest.raises(MissingTenantError):
        scoped(select(_Widget), _Widget)
    # Bound -> returns only that tenant's rows.
    with bind_tenant(tenant_b):
        async with sessionmaker() as session:
            rows = (
                (await session.execute(scoped(select(_Widget), _Widget)))
                .scalars()
                .all()
            )
    assert {r.name for r in rows} == {"b1"}


@pytest.mark.asyncio
async def test_no_tenant_bound_fails_closed(widgets) -> None:
    # Forgetting to bind a tenant must never turn a tenant-owned query into a
    # cross-tenant query. System/admin code has the explicit unscoped() escape.
    sessionmaker, _, _ = widgets
    async with sessionmaker() as session:
        with pytest.raises(MissingTenantError):
            await session.execute(select(_Widget))


@pytest.mark.asyncio
async def test_insert_inherits_bound_tenant(widgets) -> None:
    sessionmaker, tenant_a, _ = widgets
    with bind_tenant(tenant_a):
        async with sessionmaker() as session:
            widget = _Widget(name="created")
            session.add(widget)
            await session.flush()
            assert widget.tenant_id == tenant_a
            await session.rollback()


@pytest.mark.asyncio
async def test_insert_without_tenant_fails_closed(widgets) -> None:
    sessionmaker, _, _ = widgets
    async with sessionmaker() as session:
        session.add(_Widget(name="unowned"))
        with pytest.raises(MissingTenantError):
            await session.flush()


@pytest.mark.asyncio
async def test_insert_cannot_target_another_tenant(widgets) -> None:
    sessionmaker, tenant_a, tenant_b = widgets
    with bind_tenant(tenant_a):
        async with sessionmaker() as session:
            session.add(_Widget(tenant_id=tenant_b, name="foreign"))
            with pytest.raises(TenantMismatchError):
                await session.flush()


@pytest.mark.asyncio
async def test_bulk_insert_requires_explicit_system_scope(widgets) -> None:
    sessionmaker, tenant_a, _ = widgets
    with bind_tenant(tenant_a):
        async with sessionmaker() as session:
            with pytest.raises(UnsafeTenantOperationError):
                await session.execute(insert(_Widget).values(name="bulk"))


@pytest.mark.asyncio
async def test_bulk_update_cannot_change_tenant_id(widgets) -> None:
    sessionmaker, tenant_a, tenant_b = widgets
    with bind_tenant(tenant_a):
        async with sessionmaker() as session:
            with pytest.raises(UnsafeTenantOperationError):
                await session.execute(update(_Widget).values(tenant_id=tenant_b))


@pytest.mark.asyncio
async def test_bulk_update_scoped_to_bound_tenant(widgets) -> None:
    # A bound request issuing a bulk UPDATE must touch ONLY its tenant's rows —
    # the auto-guard now covers ORM UPDATE/DELETE, not just SELECT.
    sessionmaker, tenant_a, tenant_b = widgets
    with bind_tenant(tenant_a):
        async with sessionmaker() as session:
            await session.execute(update(_Widget).values(name="renamed"))
            await session.commit()
    with unscoped():
        async with sessionmaker() as session:
            rows = (await session.execute(select(_Widget))).scalars().all()
    by_tenant = {r.tenant_id: r.name for r in rows}
    assert by_tenant[tenant_b] == "b1"  # other tenant untouched
    assert all(r.name == "renamed" for r in rows if r.tenant_id == tenant_a)


@pytest.mark.asyncio
async def test_bulk_delete_scoped_to_bound_tenant(widgets) -> None:
    sessionmaker, tenant_a, _tenant_b = widgets
    with bind_tenant(tenant_a):
        async with sessionmaker() as session:
            await session.execute(delete(_Widget))  # "delete all" — but scoped
            await session.commit()
    with unscoped():
        async with sessionmaker() as session:
            rows = (await session.execute(select(_Widget))).scalars().all()
    assert {r.name for r in rows} == {"b1"}  # only tenant A's rows were deleted


def test_header_cannot_override_token_tenant() -> None:
    token_tenant, requested_tenant = uuid.uuid4(), uuid.uuid4()
    token = create_access_token(
        data={
            "sub": str(uuid.uuid4()),
            "token_type": "access",
            "tenant_id": str(token_tenant),
        }
    )

    response = TestClient(_tenant_app()).get(
        "/tenant",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": str(requested_tenant),
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "tenant is not authorized by the access token"


def test_header_can_select_tenant_from_token_allowlist() -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    token = create_access_token(
        data={
            "sub": str(uuid.uuid4()),
            "token_type": "access",
            "tenant_ids": [str(tenant_a), str(tenant_b)],
        }
    )

    response = TestClient(_tenant_app()).get(
        "/tenant",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": str(tenant_b),
        },
    )

    assert response.status_code == 200
    assert response.json() == {"tenant_id": str(tenant_b)}


def test_tenant_ids_parse_is_memoised_per_verified_token(monkeypatch) -> None:
    from sandloom.core.http import tenant_scope as tenant_mod

    tenants = [uuid.uuid4() for _ in range(5)]
    token = create_access_token(
        data={
            "sub": str(uuid.uuid4()),
            "token_type": "access",
            "tenant_ids": [str(t) for t in tenants],
        }
    )
    tenant_mod._parsed_tenant_ids.pop(token, None)
    client = TestClient(_tenant_app())
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenants[3])}

    assert client.get("/tenant", headers=headers).json() == {
        "tenant_id": str(tenants[3])
    }
    assert tenant_mod._parsed_tenant_ids[token] == frozenset(tenants)

    parsed: list[str] = []
    real_parse = tenant_mod._parse_uuid

    def counting_parse(raw: str) -> uuid.UUID:
        parsed.append(raw)
        return real_parse(raw)

    monkeypatch.setattr(tenant_mod, "_parse_uuid", counting_parse)
    response = client.get("/tenant", headers=headers)
    assert response.status_code == 200
    assert parsed == [str(tenants[3])]  # only the header; the claim is memoised
    # The memo never widens the grant: a tenant outside it is still refused.
    outsider = {**headers, "X-Tenant-Id": str(uuid.uuid4())}
    assert client.get("/tenant", headers=outsider).status_code == 403


def test_malformed_signed_tenant_ids_entry_is_400_on_every_request() -> None:
    from sandloom.core.http import tenant_scope as tenant_mod

    good = uuid.uuid4()
    token = create_access_token(
        data={
            "sub": str(uuid.uuid4()),
            "token_type": "access",
            # The tenant_id claim would answer alone; the malformed grant entry
            # must still fail the request (no string-membership shortcut).
            "tenant_id": str(good),
            "tenant_ids": [str(good), "not-a-uuid"],
        }
    )
    client = TestClient(_tenant_app())
    for _ in range(2):
        response = client.get("/tenant", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 400
        assert response.json()["detail"] == "invalid tenant id"
    assert token not in tenant_mod._parsed_tenant_ids


def test_tenant_ids_memo_is_bounded(monkeypatch) -> None:
    from sandloom.core.http import tenant_scope as tenant_mod

    monkeypatch.setattr(tenant_mod, "_TENANT_IDS_MEMO_SIZE", 2)
    monkeypatch.setattr(tenant_mod, "_parsed_tenant_ids", {})
    ids = [str(uuid.uuid4())]
    for name in ("a", "b", "c"):
        tenant_mod._claimed_tenants({"tenant_ids": ids}, ("tenant_id",), name)
    assert list(tenant_mod._parsed_tenant_ids) == ["b", "c"]
    # Claims of unknown origin (no token) are never memoised.
    tenant_mod._claimed_tenants({"tenant_ids": ids}, ("tenant_id",), None)
    assert list(tenant_mod._parsed_tenant_ids) == ["b", "c"]


def test_single_tenant_mode_binds_canonical_tenant() -> None:
    canonical = uuid.uuid4()

    response = TestClient(
        _tenant_app(tenancy_mode="single", single_tenant_id=canonical)
    ).get("/tenant")

    assert response.status_code == 200
    assert response.json() == {"tenant_id": str(canonical)}


def test_single_tenant_mode_rejects_other_tenant_selector() -> None:
    canonical = uuid.uuid4()

    response = TestClient(
        _tenant_app(tenancy_mode="single", single_tenant_id=canonical)
    ).get("/tenant", headers={"X-Tenant-Id": str(uuid.uuid4())})

    assert response.status_code == 403
    assert response.json()["detail"] == "tenant does not match the configured tenant"


def test_single_tenant_mode_requires_canonical_tenant() -> None:
    with pytest.raises(ValidationError, match="SINGLE_TENANT_ID"):
        Settings(
            DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost/sandloom",
            CACHE_URL="redis://localhost:6379/0",
            BROKER_URL="redis://localhost:6379/1",
            JWT_SECRET="test-secret",
            TENANCY_MODE="single",
        )


def test_single_tenant_runtime_scope_binds_canonical_tenant() -> None:
    canonical = uuid.uuid4()
    configured = SimpleNamespace(tenancy_mode="single", single_tenant_id=canonical)

    with configured_tenant_scope(configured):
        assert current_tenant_id() == canonical

    with pytest.raises(MissingTenantError):
        current_tenant_id()
