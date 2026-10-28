"""The taskboard read API is scoped to the CALLER's tenant (signed token), never
to a tenant the caller names."""

from __future__ import annotations

import uuid

import pytest

from projecthub.modules.taskboard.models import ProjectTaskQueryIndex
from sandloom.sdk.tenant import unscoped  # sandloom: advanced seed rows of two tenants
from sandloom.sdk.testing import as_user


def _row(tenant_id: uuid.UUID, title: str) -> ProjectTaskQueryIndex:
    return ProjectTaskQueryIndex(
        task_id=uuid.uuid4(),
        tenant_id=tenant_id,
        task_title=title,
        task_status="active",
        task_version=1,
        project_id=uuid.uuid4(),
        project_name="Dev board",
        project_status="active",
        project_version=1,
    )


@pytest.mark.asyncio
async def test_board_is_the_callers_tenant_only(client, db) -> None:
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    # Seeds rows of several tenants: system code, no tenant bound.
    with unscoped():
        db.add_all([_row(mine, "mine"), _row(theirs, "theirs")])
        await db.flush()
    _, headers = as_user(tenant_id=mine)

    resp = await client.get("/api/v1/taskboard/tasks", headers=headers)
    assert resp.status_code == 200
    assert [r["task_title"] for r in resp.json()] == ["mine"]

    # The removed query parameter cannot select another tenant any more.
    resp = await client.get(
        f"/api/v1/taskboard/tasks?tenant_id={theirs}", headers=headers
    )
    assert resp.status_code == 200
    assert [r["task_title"] for r in resp.json()] == ["mine"]

    # Nor can the X-Tenant-Id selector name a tenant outside the token.
    resp = await client.get(
        "/api/v1/taskboard/tasks", headers={**headers, "X-Tenant-Id": str(theirs)}
    )
    assert resp.status_code in (400, 403)
