"""Rebuild ``project_task_query_index`` from the source modules' read contracts.

The projection is a cache: this reconstructs it WITHOUT touching the source
modules' private tables — it reads through ``projects.read.v1`` / ``tasks.read.v1``
and applies rows through the SAME version-guarded snapshot + upsert path the
live consumers use, so a rebuild and steady-state delivery converge (a rebuild
never rolls a row that live delivery has already advanced past) — and it
refreshes the project fields of existing task rows from the project snapshot.

Run it as a one-off job once the source services are up (it reads them over
their read contracts / ``SERVICE_BINDINGS``), not in the migrate one-shot.
The read contracts are called through their generated stubs
(``Contract(PROJECTS_READ_V1)``, ``Contract(TASKS_READ_V1)``).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import distinct, select

from projecthub.shared.contracts.projects_read_v1 import PROJECTS_READ_V1
from projecthub.shared.contracts.tasks_read_v1 import TASKS_READ_V1
from sandloom.sdk import Contract
from sandloom.sdk.data import (
    open_uow,  # sandloom: advanced rebuild runs outside a request
)
from sandloom.sdk.projections import (  # sandloom: advanced a hand-written rebuilder
    ProjectionRebuildResult,
    monotonic_upsert,
)
from sandloom.sdk.tenant import (  # sandloom: advanced rebuild per tenant
    bind_tenant,
    current_tenant_id_optional,
    unscoped,
)

from .consumers import (
    PROJECTION_NAME,
    lock_project_snapshot,
    refresh_task_rows,
    upsert_project_snapshot,
)
from .models import ProjectTaskQueryIndex

projects = Contract(PROJECTS_READ_V1)
tasks = Contract(TASKS_READ_V1)


class TaskboardRebuilder:
    """Implements the ``ProjectionRebuilder`` contract for the taskboard index."""

    async def rebuild_tenant(self, tenant_id: UUID) -> ProjectionRebuildResult:
        with bind_tenant(tenant_id):
            return await self._rebuild_tenant(tenant_id)

    async def _rebuild_tenant(self, tenant_id: UUID) -> ProjectionRebuildResult:
        project_rows = await projects.list_for_tenant(tenant_id)
        task_rows = await tasks.list_for_tenant(tenant_id)
        rows = 0
        async with open_uow() as session:
            # 1) Project snapshots first (version-guarded: a rebuild never
            #    rolls back a newer state live delivery already applied), then
            #    re-fan their CURRENT values over existing task rows — this is
            #    what repairs a row left with stale project fields.
            for project in project_rows:
                project_id = UUID(project["project_id"])
                await upsert_project_snapshot(
                    session,
                    project_id=project_id,
                    tenant_id=tenant_id,
                    name=project["name"],
                    status=project["status"],
                    version=int(project["version"]),
                )
                snapshot = await lock_project_snapshot(
                    session, project_id=project_id, tenant_id=tenant_id
                )
                await refresh_task_rows(
                    session,
                    project_id=project_id,
                    name=snapshot.name,
                    status=snapshot.status,
                    version=snapshot.version,
                )
            # 2) Task rows, with project fields copied from the locked snapshot.
            for task in task_rows:
                project_id = UUID(task["project_id"])
                snapshot = await lock_project_snapshot(
                    session, project_id=project_id, tenant_id=tenant_id
                )
                await monotonic_upsert(
                    session,
                    ProjectTaskQueryIndex,
                    key={"task_id": UUID(task["task_id"])},
                    values={
                        "tenant_id": UUID(task["tenant_id"]),
                        "task_title": task["title"],
                        "task_status": task["status"],
                        "project_id": project_id,
                        "project_name": snapshot.name,
                        "project_status": snapshot.status,
                        "project_version": snapshot.version,
                    },
                    version_column="task_version",
                    incoming_version=int(task["version"]),
                )
                rows += 1
        return ProjectionRebuildResult(projection=PROJECTION_NAME, rows=rows)

    async def rebuild_all(self) -> ProjectionRebuildResult:
        # Re-sync every tenant already present in the index. A production system
        # would add a tenant-enumeration contract for cold backfill of tenants
        # with no rows yet; this keeps the demo within the read contracts above.
        # A deliberate cross-tenant read of this module's OWN index (the table
        # is TenantScoped). Under tenant_rls it sees no rows: rebuild --tenant.
        async with open_uow() as session:
            with unscoped():
                tenant_ids = (
                    (
                        await session.execute(
                            select(distinct(ProjectTaskQueryIndex.tenant_id))
                        )
                    )
                    .scalars()
                    .all()
                )
        total = 0
        for tenant_id in tenant_ids:
            total += (await self.rebuild_tenant(tenant_id)).rows
        return ProjectionRebuildResult(projection=PROJECTION_NAME, rows=total)

    async def rebuild_entity(self, ref: str) -> ProjectionRebuildResult:
        # ref form ``project:<uuid>`` — refresh that project's fields across its
        # task rows from the read contract.
        kind, _, raw = ref.partition(":")
        if kind != "project" or not raw:
            return ProjectionRebuildResult(
                projection=PROJECTION_NAME, details={"skipped": ref}
            )
        if current_tenant_id_optional() is None:
            # This read module can discover a cached entity's tenant from its
            # OWN index, without an unscoped query of another module's source.
            # A cold entity has no known tenant: rebuild its tenant explicitly.
            async with open_uow() as session:
                with unscoped():
                    tenants = (
                        await session.scalars(
                            select(ProjectTaskQueryIndex.tenant_id)
                            .where(ProjectTaskQueryIndex.project_id == UUID(raw))
                            .distinct()
                        )
                    ).all()
            if len(tenants) != 1:
                return ProjectionRebuildResult(
                    projection=PROJECTION_NAME,
                    details={
                        "skipped": ref,
                        "reason": "use rebuild --tenant for a cold entity",
                    },
                )
            with bind_tenant(tenants[0]):
                return await self.rebuild_entity(ref)
        project = await projects.get(UUID(raw))
        if project is None:
            return ProjectionRebuildResult(projection=PROJECTION_NAME, rows=0)
        async with open_uow() as session:
            await upsert_project_snapshot(
                session,
                project_id=UUID(raw),
                tenant_id=UUID(project["tenant_id"]),
                name=project["name"],
                status=project["status"],
                version=int(project["version"]),
            )
            snapshot = await lock_project_snapshot(
                session,
                project_id=UUID(raw),
                tenant_id=UUID(project["tenant_id"]),
            )
            rows = await refresh_task_rows(
                session,
                project_id=UUID(raw),
                name=snapshot.name,
                status=snapshot.status,
                version=snapshot.version,
            )
        return ProjectionRebuildResult(projection=PROJECTION_NAME, rows=rows)
