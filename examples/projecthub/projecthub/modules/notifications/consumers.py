"""Event consumer: turn a task-created event into a notification.

``TaskCreated`` is a VIEW of the tasks module's channel (only the fields
this module reads; ``sandloom check`` verifies them against the producer). The
consumer reacts to it WITHOUT importing the tasks module, persists an
in-app record in the delivery's own unit (``db()``, claimed once per envelope
id) and dispatches through the notification capability — never importing an
email/SMS SDK.

``@on(TaskCreated)`` binds the event's ``tenant_id`` for the delivery, so a
write to a ``TenantScoped`` model needs no explicit tenant.
"""

from __future__ import annotations

from uuid import UUID

from sandloom.sdk import Event, db, on
from sandloom.sdk.infra import (  # sandloom: advanced the notification capability
    Notification,
    notifier,
)

from .models import NotificationRecord

#: The durable consumer's name (its inbox identity: never rename it).
CONSUMER_NAME = "notifications@task.created"


class TaskCreated(Event, channel="tasks.task.created.v1"):
    """A view of the tasks module's event: the fields this module reads."""

    task_id: UUID
    tenant_id: UUID
    title: str


@on(TaskCreated, name=CONSUMER_NAME)
async def on_task_created(event: TaskCreated) -> None:
    title = "New task created"
    body = f"Task '{event.title}' was created."

    # 1) Persist an in-app notification (this module owns the table).
    db().add(
        NotificationRecord(
            recipient_id=event.tenant_id,  # demo: notify the owning tenant
            kind="task.created",
            title=title,
            body=body,
        )
    )
    await db().flush()

    # 2) Dispatch through the notification capability (log driver by default).
    await notifier().send(
        Notification(
            channel="inapp",
            recipient=str(event.tenant_id),
            subject=title,
            body=body,
            data={"task_id": str(event.task_id)},
            dedupe_key=f"task.created:{event.task_id}",
        )
    )
