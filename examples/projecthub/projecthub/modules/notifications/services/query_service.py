"""Read side — relies on automatic tenant scoping (NotificationRecord is
TenantScoped), so a missing tenant scope cannot leak another tenant's rows."""

from __future__ import annotations

from typing import Any

from ..schemas import NotificationOut
from .operations import RECENT_LIMIT, recent_notifications


class NotificationQueryService:
    @staticmethod
    async def list_recent(
        reads: Any, *, limit: int = RECENT_LIMIT
    ) -> list[NotificationOut]:
        """The bound tenant's ``limit`` newest notifications.

        A declared read (``reads``: the route's ``PlannedReads``, or
        ``sandloom.sdk.data.session_reads(session)`` on an open session). No
        explicit tenant_id filter: the operation is tenant-scoped because its
        entity is — no tenant bound, no query.
        """
        page = await reads.page(recent_notifications, limit=limit)
        return list(page.items)
