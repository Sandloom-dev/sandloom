"""Events of the tasks module (versioned channels with a monotonic ``version``)."""

from uuid import UUID

from sandloom.sdk import Event


class TaskCreated(Event, channel="tasks.task.created.v1"):
    """A task was created (``version`` 1)."""

    task_id: UUID
    tenant_id: UUID
    project_id: UUID
    title: str
    status: str
    version: int


class TaskUpdated(Event, channel="tasks.task.updated.v1"):
    """A task changed; ``version`` grew by one."""

    task_id: UUID
    tenant_id: UUID
    project_id: UUID
    title: str
    status: str
    version: int
