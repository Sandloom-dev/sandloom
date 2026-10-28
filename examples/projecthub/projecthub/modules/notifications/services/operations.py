"""Declared read operations of the notifications module (execution planner).

``NotificationRecord`` is ``TenantScoped``: the planner infers the scoping, so
a declared read without a bound tenant fails closed before any I/O, and both
executors filter on the bound tenant (and set the RLS GUC).
"""

from sandloom.sdk.data import ListOperation  # sandloom: advanced declared planner read

from ..models import NotificationRecord
from ..schemas import NotificationOut

#: Rows of the in-app notification list (``GET /api/v1/notifications``).
RECENT_LIMIT = 50

#: The bound tenant's newest notifications — ``ORDER BY created_at DESC`` with
#: the primary key as the tie-breaker, served by
#: ``ix_notifications_inapp_tenant_created``.
recent_notifications = ListOperation(
    entity=NotificationRecord,
    result=NotificationOut,
    order_by=("created_at",),
    descending=True,
    max_limit=RECENT_LIMIT,
    name="notifications.recent",
)

#: Everything the module declares (registered at startup, see ``__init__``).
OPERATIONS = (recent_notifications,)

__all__ = ["OPERATIONS", "RECENT_LIMIT", "recent_notifications"]
