"""Declared read operations of the taskboard module (execution planner).

The module states WHAT it reads; the kernel's planner decides HOW (a generated
statement in a read-only transaction on the optimized executor, or the ORM
general form). Only the taskboard's own projection table is referenced.
"""

from sandloom.sdk.data import ListOperation  # sandloom: advanced declared planner read

from ..models import ProjectTaskQueryIndex
from ..schemas import TaskboardRowOut

#: Upper bound of one taskboard page (the route's ``limit`` cap).
MAX_TASKBOARD_PAGE = 200

#: ``GET /api/v1/taskboard/tasks``: a tenant's projection rows, most recently
#: updated first (``updated_at DESC``, the task id breaking ties), with the
#: optional status filters and the case-insensitive project-name prefix
#: (``ILIKE``, the prefix matched literally). ``tenant_id`` is the caller's
#: bound tenant (``TenantScope``), always applied; the page walks
#: ``ix_taskboard_ptqi_tenant_updated (tenant_id, updated_at, task_id)``
#: backwards and applies the optional filters as residual predicates.
taskboard_rows = ListOperation(
    entity=ProjectTaskQueryIndex,
    result=TaskboardRowOut,
    by=("tenant_id",),
    optional={"task_status": "task_status", "project_status": "project_status"},
    prefix={"project_prefix": "project_name"},
    prefix_ignore_case=True,
    order_by=("updated_at",),
    descending=True,
    max_limit=MAX_TASKBOARD_PAGE,
    name="taskboard.rows.recent",
)

#: Everything the module declares (registered at startup, see ``__init__``).
OPERATIONS = (taskboard_rows,)

__all__ = ["MAX_TASKBOARD_PAGE", "OPERATIONS", "taskboard_rows"]
