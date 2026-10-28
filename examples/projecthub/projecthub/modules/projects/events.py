"""Events of the projects module (versioned channels:
``<module>.<entity>.<action>.vN``).

The payload carries the full denormalisable state plus a monotonic
``version``, so a downstream projection can apply it without reading the
projects module's private tables (and dedupe/order on ``version``).
"""

from uuid import UUID

from sandloom.sdk import Event


class ProjectCreated(Event, channel="projects.project.created.v1"):
    """A project was created (``version`` 1)."""

    project_id: UUID
    tenant_id: UUID
    name: str
    status: str
    version: int


class ProjectUpdated(Event, channel="projects.project.updated.v1"):
    """A project changed; ``version`` grew by one."""

    project_id: UUID
    tenant_id: UUID
    name: str
    status: str
    version: int
