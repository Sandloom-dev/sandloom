"""The board's optional filters: applied only when given, prefixes literal and
case-insensitive, newest update first (the task id breaking ties)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from projecthub.modules.taskboard.models import ProjectTaskQueryIndex
from sandloom.sdk.tenant import unscoped  # sandloom: advanced seed rows of two tenants
from sandloom.sdk.testing import as_user

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _row(
    tenant_id: uuid.UUID, i: int, task_status: str, project: str, project_status: str
) -> ProjectTaskQueryIndex:
    return ProjectTaskQueryIndex(
        task_id=uuid.uuid4(),
        tenant_id=tenant_id,
        task_title=f"t{i}",
        task_status=task_status,
        task_version=1,
        project_id=uuid.uuid4(),
        project_name=project,
        project_status=project_status,
        project_version=1,
        updated_at=T0 + timedelta(minutes=i),
    )


@pytest.mark.asyncio
async def test_filters_prefix_and_limit(client, db) -> None:
    tenant = uuid.uuid4()
    # Seeds rows of several tenants: system code, no tenant bound.
    with unscoped():
        db.add_all(
            [
                _row(tenant, 0, "active", "Dev board", "active"),
                _row(tenant, 1, "done", "dev tools", "active"),
                _row(tenant, 2, "active", "DEVOPS", "archived"),
                _row(tenant, 3, "active", "Design", "active"),
                _row(tenant, 4, "active", "D_v lab", "active"),
                _row(tenant, 5, "active", "100% done", "active"),
                _row(uuid.uuid4(), 6, "active", "Dev board", "active"),
            ]
        )
        await db.flush()
    _, headers = as_user(tenant_id=tenant)

    async def titles(query: str = "") -> list[str]:
        resp = await client.get(f"/api/v1/taskboard/tasks{query}", headers=headers)
        assert resp.status_code == 200, resp.text
        return [r["task_title"] for r in resp.json()]

    assert await titles() == ["t5", "t4", "t3", "t2", "t1", "t0"]
    assert await titles("?task_status=active") == ["t5", "t4", "t3", "t2", "t0"]
    assert await titles("?project_status=archived") == ["t2"]
    # case-insensitive prefix, matched literally (``_`` / ``%`` are not wildcards)
    assert await titles("?project_prefix=dev") == ["t2", "t1", "t0"]
    assert await titles("?project_prefix=D_v") == ["t4"]
    assert await titles("?project_prefix=100%25") == ["t5"]
    assert await titles("?project_prefix=") == ["t5", "t4", "t3", "t2", "t1", "t0"]
    assert await titles(
        "?task_status=active&project_status=active&project_prefix=DE"
    ) == ["t3", "t0"]
    assert await titles("?limit=2") == ["t5", "t4"]
    assert await titles("?limit=0") == []
    resp = await client.get("/api/v1/taskboard/tasks?limit=201", headers=headers)
    assert resp.status_code == 422
