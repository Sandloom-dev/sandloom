"""``projects.read.v2`` — the public read contract other modules consume.

The sanctioned cross-module read path: the taskboard read model uses it to
denormalise the current project state at projection-build time, and its
rebuilder uses it to backfill, neither ever touching projects' tables.

Every call runs in the projects module's OWN read-only Unit of Work on its
own database, so it behaves identically whether called locally (monolith)
or through an internal RPC proxy (microservices) — never the caller's
transaction. ``v2`` returns the ``ProjectState`` DTO, never ORM models.
``sandloom generate`` writes the typed stub callers import
(``projecthub.shared.contracts.projects_read_v2``).

``projects.read.v1`` returned plain dicts. It was released, and a breaking
change of a released contract is a new major, so ``v1`` stays unchanged
beside ``v2`` until no caller of it is left (``sandloom generate --new-major``;
``sandloom explain SANDLOOM-T004``). The app has no ``v1`` caller any more.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select

from sandloom.sdk import contract, db

from .models import Project
from .schemas import ProjectState
from .services import snapshot


@contract("projects.read.v1")
class ProjectsRead:
    async def get(self, project_id: UUID) -> dict[str, Any] | None:
        """The project's state, or ``None``."""
        project = (
            await db().execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        return snapshot(project).model_dump(mode="json") if project else None

    async def list_for_tenant(self, tenant_id: UUID) -> list[dict[str, Any]]:
        """Every project of ``tenant_id``."""
        rows = (
            await db().execute(select(Project).where(Project.tenant_id == tenant_id))
        ).scalars()
        return [snapshot(p).model_dump(mode="json") for p in rows]


@contract("projects.read.v2")
class ProjectsReadV2:
    """``v1`` with a typed answer: callers read ``project.name``, not
    ``project.get("name", "")``. ``sandloom generate`` copies ``ProjectState``
    into ``projecthub.shared.contracts``."""

    async def get(self, project_id: UUID) -> ProjectState | None:
        """The project's state, or ``None``."""
        project = (
            await db().execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        return snapshot(project) if project is not None else None

    async def list_for_tenant(self, tenant_id: UUID) -> list[ProjectState]:
        """Every project of ``tenant_id``."""
        rows = (
            await db().execute(select(Project).where(Project.tenant_id == tenant_id))
        ).scalars()
        return [snapshot(p) for p in rows]
