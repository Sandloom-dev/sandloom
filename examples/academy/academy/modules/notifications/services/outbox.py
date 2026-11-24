"""Record messages (idempotent per operation key) and list a user's inbox."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select

from sandloom.sdk import db

from ..models import NotificationsMessage


class Outbox:
    @classmethod
    async def send(cls, recipient_id: UUID, template: str, operation_key: str) -> UUID:
        existing = (
            await db().execute(
                select(NotificationsMessage).where(
                    NotificationsMessage.operation_key == operation_key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id
        message = NotificationsMessage(
            recipient_id=recipient_id, template=template, operation_key=operation_key
        )
        db().add(message)
        await db().flush()
        return message.id

    @classmethod
    async def inbox(cls, recipient_id: UUID) -> Sequence[NotificationsMessage]:
        return (
            (
                await db().execute(
                    select(NotificationsMessage)
                    .where(NotificationsMessage.recipient_id == recipient_id)
                    .order_by(NotificationsMessage.created_at, NotificationsMessage.id)
                )
            )
            .scalars()
            .all()
        )
