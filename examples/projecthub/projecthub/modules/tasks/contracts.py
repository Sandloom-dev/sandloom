"""``tasks.read.v1`` — the public read contract (used by the taskboard rebuild).

Runs in the tasks module's own read-only Unit of Work; returns plain dicts.
Live updates flow through the versioned events the write side publishes.
``sandloom test`` generates the typed stub callers import
(``projecthub.shared.contracts.tasks_read_v1``).
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select

from sandloom.sdk import contract, db

from .models import Task
from .services import snapshot


@contract("tasks.read.v1")
class TasksRead:
    async def list_for_tenant(self, tenant_id: UUID) -> list[dict[str, Any]]:
        """Every task of ``tenant_id``."""
        rows = (
            await db().execute(select(Task).where(Task.tenant_id == tenant_id))
        ).scalars()
        return [snapshot(t) for t in rows]
