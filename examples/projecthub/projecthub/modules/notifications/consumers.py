"""Event consumer: turn a task-created event into a notification.

``TaskCreated`` is a VIEW of the tasks module's channel (only the fields
this module reads; ``sandloom check`` verifies them against the producer). The
consumer reacts to it WITHOUT importing the tasks module, persists an
in-app record in the delivery's own unit (claimed once per envelope id) and
dispatches through the notification capability — never importing an
email/SMS SDK.

It is registered in the module's ``setup`` in its L2 form
``(session, envelope)``: the record is written in the delivery's session,
which an ``@on(TaskCreated)`` handler reaches only through ``db()``
(Public API 1.0 hand-off H1).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk import Event
from sandloom.sdk.events import (
    EventEnvelope,  # sandloom: advanced L2 consumer until db()
)
from sandloom.sdk.infra import (  # sandloom: advanced the notification capability
    Notification,
    notifier,
)
from sandloom.sdk.tenant import bind_tenant  # sandloom: advanced L2 consumer until db()

from .models import NotificationRecord

#: The durable consumer's name (its inbox identity: never rename it).
CONSUMER_NAME = "notifications@task.created"


class TaskCreated(Event, channel="tasks.task.created.v1"):
    """A view of the tasks module's event: the fields this module reads."""

    task_id: UUID
    tenant_id: UUID
    title: str


def decode(envelope: EventEnvelope) -> TaskCreated:
    """The view of a delivered envelope (unknown payload fields ignored)."""
    payload = envelope["payload"]
    return TaskCreated.model_validate(
        {k: v for k, v in payload.items() if k in TaskCreated.model_fields}
    )


async def on_task_created(session: AsyncSession, envelope: EventEnvelope) -> None:
    event = decode(envelope)
    title = "New task created"
    body = f"Task '{event.title}' was created."

    # 1) Persist an in-app notification (this module owns the table). A
    #    consumer serves every tenant, so it binds the event's tenant: writes
    #    to a TenantScoped model fail closed without one.
    with bind_tenant(event.tenant_id):
        record = NotificationRecord(
            tenant_id=event.tenant_id,
            recipient_id=event.tenant_id,  # demo: notify the owning tenant
            kind="task.created",
            title=title,
            body=body,
        )
        session.add(record)
        await session.flush()

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
