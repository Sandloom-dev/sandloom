"""List the bound tenant's notes, and the RLS setting the kernel bound."""

from typing import Any

from sqlalchemy import func, select

from sandloom.sdk import CurrentUser, db, module_router

from .models import NotesItem

router = module_router()


@router.get("/items")
async def items(user: CurrentUser) -> dict[str, Any]:
    rows = (
        await db().execute(select(NotesItem.body).order_by(NotesItem.body))
    ).scalars()
    guc = (
        await db().execute(select(func.current_setting("sandloom.tenant_id", True)))
    ).scalar()
    return {"rls_tenant_setting": guc, "bodies": list(rows)}
