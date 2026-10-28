"""``PlannedReads`` / ``SnapshotReads`` bind the tenant like the request UoW.

With ``TENANCY_MODE=single`` the request Unit of Work (``DbSession``) binds the
canonical ``SINGLE_TENANT_ID`` for the whole request (and refuses another
bound tenant). A route moved from ``DbSession`` to declared reads must keep
that: the read scope applies the same ``configured_tenant_scope``. In
multi-tenant mode it binds nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import APIRouter

from sandloom.core.config.settings import settings
from sandloom.core.di import (
    DbSession,
    PlannedReads,
    SnapshotReads,
    current_tenant_id_optional,
    public,
)
from sandloom.core.testing.fixtures import *  # noqa: F403


def _router() -> APIRouter:
    r = APIRouter(dependencies=[public(reason="test probe")])

    @r.get("/_t/tenant/planned")
    async def planned(reads: PlannedReads) -> dict[str, Any]:
        return {"tenant": str(current_tenant_id_optional())}

    @r.get("/_t/tenant/snapshot")
    async def snapshot(reads: SnapshotReads) -> dict[str, Any]:
        return {"tenant": str(current_tenant_id_optional())}

    @r.get("/_t/tenant/uow")
    async def uow(db: DbSession) -> dict[str, Any]:
        return {"tenant": str(current_tenant_id_optional())}

    return r


@pytest.fixture
def tenant_routes(sandloom_app: Any) -> Iterator[None]:
    before = list(sandloom_app.router.routes)
    sandloom_app.include_router(_router())
    try:
        yield
    finally:
        sandloom_app.router.routes[:] = before


@pytest.mark.parametrize("mode", ["single", "multi"])
async def test_read_scopes_bind_the_configured_tenant_like_the_uow(
    tenant_routes: None,
    real_uow_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    canonical = uuid.uuid4()
    monkeypatch.setattr(settings, "tenancy_mode", mode)
    monkeypatch.setattr(
        settings, "single_tenant_id", canonical if mode == "single" else None
    )
    expect = str(canonical) if mode == "single" else "None"
    for kind in ("uow", "planned", "snapshot"):
        resp = await real_uow_client.get(f"/_t/tenant/{kind}")
        assert resp.status_code == 200, (kind, resp.text)
        assert resp.json() == {"tenant": expect}, kind
