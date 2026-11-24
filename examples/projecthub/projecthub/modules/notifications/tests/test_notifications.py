"""End-to-end notifications test.

Drives the task-created event through the real consumer runtime (schema
preflight, durable claim, tenant binding): the consumer writes an in-app
record and dispatches via the notification capability. Then it proves the
read path is automatically tenant-scoped.
"""

from __future__ import annotations

import uuid

import pytest

from projecthub.modules.notifications.services import NotificationQueryService
from sandloom.sdk import bind_tenant
from sandloom.sdk.data import (
    session_reads,  # sandloom: advanced declared reads on a session
)
from sandloom.sdk.testing import deliver


def _payload(tenant_id: uuid.UUID) -> dict:
    return {
        "task_id": str(uuid.uuid4()),
        "tenant_id": str(tenant_id),
        "project_id": str(uuid.uuid4()),
        "title": "Ship the notifier",
        "status": "active",
        "version": 1,
    }


@pytest.mark.asyncio
async def test_task_created_notifies_and_is_tenant_scoped(client, db) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    # The consumer reacts to a task-created event: persists a record + dispatches.
    assert await deliver("tasks.task.created.v1", _payload(tenant_a), session=db)

    # Read path is auto-filtered to the bound tenant (TenantScoped model).
    with bind_tenant(tenant_a):
        rows = await NotificationQueryService.list_recent(session_reads(db))
    assert len(rows) == 1
    assert rows[0].kind == "task.created"
    assert "Ship the notifier" in rows[0].body

    # A different tenant sees nothing — no cross-tenant leak.
    with bind_tenant(tenant_b):
        other = await NotificationQueryService.list_recent(session_reads(db))
    assert other == []


@pytest.mark.asyncio
async def test_a_redelivered_event_notifies_once(client, db) -> None:
    """A redelivery of the same envelope is applied once."""
    tenant = uuid.uuid4()
    assert await deliver("tasks.task.created.v1", _payload(tenant), times=2, session=db)
    with bind_tenant(tenant):
        rows = await NotificationQueryService.list_recent(session_reads(db))
    assert [r.kind for r in rows] == ["task.created"]
