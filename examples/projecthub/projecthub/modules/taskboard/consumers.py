"""Projection consumers: apply source events to ``project_task_query_index``.

The source events are VIEWS of the projects / tasks channels (declared here,
verified against the producers by ``sandloom check``); the module never imports
those modules. The projects read contract is called through its generated
stub (``Contract(PROJECTS_READ_V1)``).

Every handler is version-guarded, so at-least-once / out-of-order delivery is
safe, and the two writers of a project's denormalised fields are SERIALISED on
the project's row in the local ``taskboard_project_snapshot`` table (see
``models/project_snapshot.py``):

* ``on_project_updated`` — a project created/updated event. Upserts the
  snapshot (guarded by its ``version``; the upsert takes the row lock), then
  refreshes the project fields across every existing task row for that
  project (guarded by ``project_version``).
* ``on_task_upserted`` — a task created/updated event. Locks the project's
  snapshot row (seeding it from the ``projects.read.v1`` contract only when it
  does not exist yet) and upserts the task row (guarded by ``task_version``)
  with the snapshot's CURRENT project fields.

Because both take the same row lock, a project event can no longer commit in
the gap between a task handler's project read and its insert (its fan-out
UPDATE would match no row and the task row would keep stale project data
forever): whichever transaction locks the snapshot second observes the first.

The handlers keep the L2 form ``(session, envelope)`` of a projection
consumer (registered in the module's ``setup``): the fan-out over every task
row of a project is not a ``Projection`` source group (whose rows are keyed
by the event's aggregate).
"""

from __future__ import annotations

from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from projecthub.shared.contracts.projects_read_v1 import PROJECTS_READ_V1
from sandloom.sdk import Contract, Event
from sandloom.sdk.events import (
    EventEnvelope,  # sandloom: advanced L2 projection consumers
)
from sandloom.sdk.projections import (  # sandloom: advanced version-guarded writes
    monotonic_update,
    monotonic_upsert,
)

from .models import ProjectSnapshot, ProjectTaskQueryIndex

#: The projection this module owns.
PROJECTION_NAME = "project_task_query_index"

projects = Contract(PROJECTS_READ_V1)


class TaskCreated(Event, channel="tasks.task.created.v1"):
    """A view of the tasks module's event (the fields the board reads)."""

    task_id: UUID
    tenant_id: UUID
    project_id: UUID
    title: str
    status: str
    version: int


class TaskUpdated(Event, channel="tasks.task.updated.v1"):
    """A view of the tasks module's event (the fields the board reads)."""

    task_id: UUID
    tenant_id: UUID
    project_id: UUID
    title: str
    status: str
    version: int


class ProjectCreated(Event, channel="projects.project.created.v1"):
    """A view of the projects module's event (the fields the board reads)."""

    project_id: UUID
    tenant_id: UUID
    name: str
    status: str
    version: int


class ProjectUpdated(Event, channel="projects.project.updated.v1"):
    """A view of the projects module's event (the fields the board reads)."""

    project_id: UUID
    tenant_id: UUID
    name: str
    status: str
    version: int


V = TypeVar("V", bound=Event)


def decode(view: type[V], envelope: EventEnvelope) -> V:
    """The view of a delivered envelope (unknown payload fields ignored)."""
    payload = envelope["payload"]
    return view.model_validate(
        {k: v for k, v in payload.items() if k in view.model_fields}
    )


async def upsert_project_snapshot(
    session: AsyncSession,
    *,
    project_id: UUID,
    tenant_id: UUID,
    name: str,
    status: str,
    version: int,
) -> bool:
    """Version-guarded upsert of a project's snapshot (locks the row).

    Returns ``False`` when the snapshot already holds this or a newer version.
    """
    return await monotonic_upsert(
        session,
        ProjectSnapshot,
        key={"project_id": project_id},
        values={"tenant_id": tenant_id, "name": name, "status": status},
        version_column="version",
        incoming_version=version,
    )


async def refresh_task_rows(
    session: AsyncSession, *, project_id: UUID, name: str, status: str, version: int
) -> int:
    """Fan a project's fields out over its task rows (``project_version`` guard)."""
    return await monotonic_update(
        session,
        ProjectTaskQueryIndex,
        match={"project_id": project_id},
        values={"project_name": name, "project_status": status},
        version_column="project_version",
        incoming_version=version,
    )


async def _locked_snapshot(session: AsyncSession, project_id: UUID) -> Any:
    stmt = (
        select(ProjectSnapshot.name, ProjectSnapshot.status, ProjectSnapshot.version)
        .where(ProjectSnapshot.project_id == project_id)
        .with_for_update()
    )
    return (await session.execute(stmt)).first()


async def lock_project_snapshot(
    session: AsyncSession, *, project_id: UUID, tenant_id: UUID
) -> Any:
    """Return the project's snapshot row, LOCKED for this transaction.

    Seeds a missing snapshot from the projects read contract first (no lock is
    held during that call). The seed is version-guarded, so a project event
    that committed a newer snapshot meanwhile wins and is what gets locked.
    """
    snapshot = await _locked_snapshot(session, project_id)
    if snapshot is not None:
        return snapshot
    project = await projects.get(project_id) or {}
    await upsert_project_snapshot(
        session,
        project_id=project_id,
        tenant_id=tenant_id,
        name=str(project.get("name", "")),
        status=str(project.get("status", "unknown")),
        version=int(project.get("version", 0)),
    )
    return await _locked_snapshot(session, project_id)


async def on_task_upserted(session: AsyncSession, envelope: EventEnvelope) -> None:
    # created and updated carry the same fields
    task = decode(TaskUpdated, envelope)
    snapshot = await lock_project_snapshot(
        session, project_id=task.project_id, tenant_id=task.tenant_id
    )
    await monotonic_upsert(
        session,
        ProjectTaskQueryIndex,
        key={"task_id": task.task_id},
        values={
            "tenant_id": task.tenant_id,
            "task_title": task.title,
            "task_status": task.status,
            "project_id": task.project_id,
            # Copied from the LOCKED snapshot: always the newest project
            # state this read model has seen, never a stale RPC read.
            "project_name": snapshot.name,
            "project_status": snapshot.status,
            "project_version": snapshot.version,
        },
        version_column="task_version",
        incoming_version=task.version,
    )


async def on_project_updated(session: AsyncSession, envelope: EventEnvelope) -> None:
    # created and updated carry the same fields
    project = decode(ProjectUpdated, envelope)
    # Snapshot first: the upsert takes the row lock that task handlers
    # wait on, so the fan-out below sees every committed task row.
    applied = await upsert_project_snapshot(
        session,
        project_id=project.project_id,
        tenant_id=project.tenant_id,
        name=project.name,
        status=project.status,
        version=project.version,
    )
    if not applied:
        return  # duplicate / out-of-order: a newer state was already fanned out
    await refresh_task_rows(
        session,
        project_id=project.project_id,
        name=project.name,
        status=project.status,
        version=project.version,
    )
