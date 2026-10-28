"""``notifications.mail.v1`` — record a message for a recipient (a command)."""

from uuid import UUID

from sandloom.sdk import command, contract, db

from .services import Outbox


@contract("notifications.mail.v1")
class Mail:
    @command(idempotency_key="operation_key")
    async def send(self, recipient_id: UUID, template: str, operation_key: str) -> UUID:
        """Record ``template`` for ``recipient_id``; the message id."""
        return await Outbox.send(db(), recipient_id, template, operation_key)
