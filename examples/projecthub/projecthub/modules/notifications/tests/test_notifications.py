"""End-to-end notifications test.

Drives the task-created event through the notifications consumer (writes an
in-app record + dispatches via the notification capability), then proves the
read path is automatically tenant-scoped.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from projecthub.modules.notifications.consumers import on_task_created
from projecthub.modules.notifications.services import NotificationQueryService
from sandloom.sdk.data import (
    session_reads,  # sandloom: advanced declared reads on a session
)
from sandloom.sdk.tenant import bind_tenant  # sandloom: advanced read as one tenant
from sandloom.sdk.testing import deliver


def _event(tenant_id: uuid.UUID) -> dict:
    return {
        "id": f"evt-{uuid.uuid4()}",
        "channel": "tasks.task.created.v1",
        "payload": {
            "task_id": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "project_id": str(uuid.uuid4()),
            "title": "Ship the notifier",
            "status": "active",
            "version": 1,
        },
    }


@pytest.mark.asyncio
async def test_task_created_notifies_and_is_tenant_scoped(
    db_engine: AsyncEngine,
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    # The consumer reacts to a task-created event: persists a record + dispatches.
    async with sessionmaker() as session:
        await on_task_created(session, _event(tenant_a))
        await session.commit()

    # Read path is auto-filtered to the bound tenant (TenantScoped model).
    with bind_tenant(tenant_a):
        async with sessionmaker() as session:
            rows = await NotificationQueryService.list_recent(session_reads(session))
    assert len(rows) == 1
    assert rows[0].kind == "task.created"
    assert "Ship the notifier" in rows[0].body

    # A different tenant sees nothing — no cross-tenant leak.
    with bind_tenant(tenant_b):
        async with sessionmaker() as session:
            other = await NotificationQueryService.list_recent(session_reads(session))
    assert other == []


@pytest.mark.asyncio
async def test_a_redelivered_event_notifies_once(client, db) -> None:
    """Through the real consumer runtime (schema preflight, durable claim,
    tenant binding): a redelivery of the same envelope is applied once."""
    tenant = uuid.uuid4()
    payload = _event(tenant)["payload"]
    assert await deliver("tasks.task.created.v1", payload, times=2, session=db)
    with bind_tenant(tenant):
        rows = await NotificationQueryService.list_recent(session_reads(db))
    assert [r.kind for r in rows] == ["task.created"]
