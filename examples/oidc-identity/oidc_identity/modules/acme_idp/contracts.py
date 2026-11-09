"""``acme_idp.security.v1``: what acme_idp really has for operator commands.

``sandloom security revoke-sessions`` finds it as the map's ``issuer``
(``Authority("acme_idp", module=IDP)`` in the root) and confirms each
method by describe before it acts. acme_idp lists its accounts
(``--all``) and ends what it holds (``end_sessions``); it keeps no
passwords, so ``force-password-reset`` is refused before anything is done
(no ``require_password_reset`` here: reset passwords at the provider).

acme_idp is also the authority of ``roles``, so ``sandloom security
grant-admin`` finds both halves here: the account by address (issuer) and
the ``admin`` role (roles owner). ``sandloom admin create`` and ``invite``
need an account the CLI creates and a password the CLI sets: acme_idp has
neither method, so they are refused before anything is done (people sign
in at the provider).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException

from sandloom.sdk import command, contract, db, record_audit

from .models import ExternalAccount
from .schemas import OperatorAccount, SessionsEnded
from .services import (
    ADMIN_ROLE,
    account_by_email,
    account_ids_after,
    accounts_by_id,
    add_admin_role,
    admin_ids,
    mark_accounts_signed_out,
    roles_of,
)

#: Most users one call lists or ends.
MAX_BATCH = 1000


@contract("acme_idp.security.v1")
class AcmeSecurityContract:
    """Operator commands of a stateless OIDC issuer: accounts, sessions and
    the platform role."""

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

    # --- the issuer's accounts (``sandloom admin`` / ``grant-admin``) ---------

    async def account_by_email(self, email: str) -> OperatorAccount | None:
        """The account that signed in with this address, or ``None``."""
        account = await account_by_email(db(), email)
        return None if account is None else _account(account)

    async def accounts_by_id(self, user_ids: list[UUID]) -> list[OperatorAccount]:
        """The known accounts among ``user_ids`` (at most 1000), by address."""
        found = await accounts_by_id(db(), user_ids[:MAX_BATCH])
        return [_account(account) for account in found]

    # --- the platform role (acme_idp is the authority of ``roles``) -----------

    async def is_platform_admin(self, user_id: UUID) -> bool:
        """Whether the account holds the platform ``admin`` role."""
        return ADMIN_ROLE in ((await roles_of(db(), user_id)) or ())

    async def platform_admin_ids(self) -> list[UUID]:
        """The accounts holding the ``admin`` role (at most 1000)."""
        return await admin_ids(db(), MAX_BATCH)

    async def platform_admin_exists(self) -> bool:
        """Whether any account holds the ``admin`` role."""
        return bool(await admin_ids(db(), 1))

    @command(idempotency_key="operation_key")
    async def appoint_platform_admin(
        self, user_id: UUID, reason: str, operator: str, operation_key: str
    ) -> bool:
        """Give the account the ``admin`` role (audited); whether it exists.

        The role set is the provider's groups, read again at every sign-in:
        a grant here lasts until the account's next sign-in, unless the
        provider's groups carry it too (grant it there to keep it).
        """
        if not reason.strip() or not operator.strip():
            raise HTTPException(422, "reason and operator are required")
        added = await add_admin_role(db(), user_id)
        if added is None:
            return False
        if added:
            record_audit(
                "acme_idp.account.platform_admin_granted",
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
        return True


def _account(account: ExternalAccount) -> OperatorAccount:
    return OperatorAccount(
        user_id=account.id,
        email=account.email,
        email_verified=bool(account.email_verified),
    )


__all__ = ["AcmeSecurityContract"]
