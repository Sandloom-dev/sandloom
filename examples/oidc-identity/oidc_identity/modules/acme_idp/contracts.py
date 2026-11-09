"""``acme_idp.security.v1``: what acme_idp really has for operator commands.

``sandloom security revoke-sessions`` finds it as the map's ``issuer``
(``Authority("acme_idp", module=IDP)`` in the root) and confirms each
method by describe before it acts. acme_idp lists its accounts
(``--all``) and ends what it holds (``end_sessions``); it keeps no
passwords, so ``force-password-reset`` is refused before anything is done
(no ``require_password_reset`` here: reset passwords at the provider).
"""

from __future__ import annotations

from uuid import UUID

from sandloom.sdk import command, contract, db, record_audit

from .schemas import SessionsEnded
from .services import account_ids_after, mark_accounts_signed_out

#: Most users one call lists or ends.
MAX_BATCH = 1000


@contract("acme_idp.security.v1")
class AcmeSecurityContract:
    """Operator commands of a stateless OIDC issuer: accounts and sessions."""

    async def list_user_ids(self, after: UUID | None, limit: int) -> list[UUID]:
        """Account ids in id order, strictly after ``after`` (keyset paging,
        at most 1000 per call)."""
        return await account_ids_after(db(), after, max(1, min(limit, MAX_BATCH)))

    @command(idempotency_key="operation_key")
    async def end_sessions(
        self, user_ids: list[UUID], reason: str, operator: str, operation_key: str
    ) -> SessionsEnded:
        """Record the accounts as signed out (audited per account).

        acme_idp holds no refresh session: its sessions are the access
        tokens it minted, which the kernel revokes for every issuer. The
        provider's own sessions are not ended here.
        """
        ended = await mark_accounts_signed_out(db(), user_ids[:MAX_BATCH])
        for user_id in ended:
            record_audit(
                "acme_idp.account.sessions_ended",
                actor_type="operator",
                resource_type="user",
                resource_id=str(user_id),
                data={
                    "operator": operator,
                    "reason": reason,
                    "operation_key": operation_key,
                },
                required=True,
            )
        return SessionsEnded(affected=len(ended), ended=["access_tokens"])


__all__ = ["AcmeSecurityContract"]
