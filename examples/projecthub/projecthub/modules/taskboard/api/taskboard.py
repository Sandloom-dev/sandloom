"""Taskboard read API — serves the cross-module projection (no runtime join).

``module_router()`` authenticates every route and binds the tenant from the
signed token (the projection is ``TenantScoped``).
"""

from fastapi import Query

from sandloom.sdk import module_router
from sandloom.sdk.data import PlannedReads  # sandloom: advanced read-only planner route
from sandloom.sdk.web import TenantId  # sandloom: advanced the bound tenant as a filter

from ..schemas import TaskboardRowOut
from ..services.operations import MAX_TASKBOARD_PAGE, taskboard_rows

router = module_router()


@router.get("/tasks", response_model=list[TaskboardRowOut])
async def list_tasks(
    reads: PlannedReads,
    # The tenant comes ONLY from the signed token (a ``tenant_id`` claim, or an
    # ``X-Tenant-Id`` selector present in the token's ``tenant_ids``): a
    # caller-chosen query parameter here let any user read another tenant's
    # board.
    tenant_id: TenantId,
    task_status: str | None = Query(default=None),
    project_status: str | None = Query(default=None),
    project_prefix: str | None = Query(default=None),
    limit: int = Query(default=50, le=MAX_TASKBOARD_PAGE),
) -> list[TaskboardRowOut]:
    """List tasks joined with their project — e.g. active tasks in active
    "Dev*" projects — from one indexed projection read."""
    # One declared keyset read (``taskboard.rows.recent``): an optional
    # filter is applied only when given; an empty prefix is no filter.
    if limit < 1:
        if limit == 0:
            return []
        # what the database answered to a negative LIMIT (a masked 500)
        raise ValueError("LIMIT must not be negative")
    rows: list[TaskboardRowOut] = []
    cursor: str | None = None
    while len(rows) < limit:  # one page unless PLANNER_LIST_MAX_LIMIT < limit
        page = await reads.page(
            taskboard_rows,
            limit=min(limit - len(rows), taskboard_rows.page_cap()),
            cursor=cursor,
            tenant_id=tenant_id,
            task_status=task_status,
            project_status=project_status,
            project_prefix=project_prefix or None,
        )
        rows.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    return rows
