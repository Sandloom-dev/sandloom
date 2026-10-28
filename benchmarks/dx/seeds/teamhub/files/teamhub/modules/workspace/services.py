"""The workspace use cases. ``db()`` is the session of the running unit."""

from uuid import UUID

import sqlalchemy as sa

from sandloom.sdk import db

from .models.settings import WorkspaceSettings
from .schemas import SettingsIn, SettingsOut

DEFAULTS = SettingsOut(display_name="", timezone="UTC")


async def _row() -> WorkspaceSettings | None:
    """The bound organization's row (``TenantScoped``)."""
    return await db().scalar(sa.select(WorkspaceSettings))


async def get_settings() -> SettingsOut:
    row = await _row()
    return DEFAULTS if row is None else SettingsOut.model_validate(row)


async def update_settings(body: SettingsIn, *, user_id: UUID) -> SettingsOut:
    row = await _row()
    if row is None:
        row = WorkspaceSettings(updated_by=user_id, **body.model_dump())
        db().add(row)
    else:
        row.display_name = body.display_name
        row.timezone = body.timezone
        row.updated_by = user_id
    await db().flush()
    return SettingsOut.model_validate(row)
