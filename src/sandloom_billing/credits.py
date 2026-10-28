"""Credits: billing-internal command helpers + the ``billing.credits.v1`` contract.

Registered as ``billing.credits.v1`` (spec §5 ``spend_credits``) is
:class:`sandloom_billing.contracts.CreditsContract`. Consuming modules spend a
tenant's credits without importing billing — and WITHOUT passing their session::

    credits = core.get_service("billing.credits.v1")
    balance = await credits.spend(
        "user", user_id, 10, f"report:{report_id}", reason="report export"
    )

Semantics (BILLING_SPEC §8/§9), identical locally and over internal RPC:

- runs in billing's OWN Unit of Work on billing's database (ledger insert +
  guarded balance decrement), committed when the call returns;
- **NOT atomic with the caller's transaction** (saga semantics): if the
  caller's own transaction later rolls back, the spend stays committed — the
  caller compensates (refund/grant through billing) or drives the spend from
  an event it publishes in its own transaction instead;
- the ``operation_key`` is mandatory and is the idempotency key: a retry (or
  a replay after a timeout) returns the ORIGINAL resulting balance; reusing the
  key for a different amount → 409;
- insufficient balance (or no billing customer yet) → 402, nothing written;
- **tenant binding**: when the caller has a tenant bound (``TenantScope`` /
  ``bind_tenant``; over RPC the signed context re-binds it), an
  ``"organization"`` spend must target THAT organization — another org → 403
  (``BillingTenantMismatchError``). A *personal* spend (``tenant_type="user"``)
  is not checked against the bound tenant (a user's own wallet is not a
  tenancy tenant) but against the end user the call runs for: when the call
  context carries one (the calling request's subject locally, the signed
  ``usr`` over RPC), ``tenant_id`` must be that user (403 otherwise). System
  and service callers pass any id, as before. A single-tenant
  deployment (``TENANCY_MODE=single``) binds the canonical deployment tenant,
  which is not an organization selector, so the check does not apply there;
- the framework additionally dedupes the command on ``operation_key`` (replay
  of the stored result, fail-closed when the idempotency store is down);
- errors reach consumers as ``HTTPException`` with the same status locally and
  over RPC (domain errors are converted at this boundary);
- emits ``billing.credits.spent.v1`` through billing's transactional outbox.

Granting is deliberately NOT part of the contract: credits are minted only by
billing itself (paid checkout webhooks, operator tooling).
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.core.module_api import contract_session
from sandloom.sdk.infra import settings
from sandloom.sdk.tenant import current_tenant_id_optional
from sandloom_billing.entitlements import EntitlementsService
from sandloom_billing.exceptions import (
    BillingTenantMismatchError,
    InsufficientCreditsError,
)
from sandloom_billing.services.billing_service import BillingService
from sandloom_billing.services.tenant_resolver import TENANT_ORGANIZATION

CREDITS_SERVICE_V1 = "billing.credits.v1"
#: ``contract_session().info`` key: the end user a contract call runs for
#: (stamped by the contract runtime; the signed ``usr`` over RPC).
CALLER_USER_INFO_KEY = "sandloom_contract_caller_user"


class CreditsService:
    """Billing-internal command helpers (take billing's own session)."""

    @classmethod
    async def spend(
        cls,
        tenant_type: str,
        tenant_id: UUID,
        amount: int,
        operation_key: str,
        db: AsyncSession,
        *,
        reason: str = "",
    ) -> int:
        """Spend ``amount`` credits of the tenant; return the new balance.

        ``amount=0`` (a free item, a promo code) succeeds: it is recorded like
        any spend (a ledger row with delta 0, idempotent on the key, the spent
        event) and leaves the balance unchanged; an account without a billing
        customer yet gets one. A negative amount is refused. Raises billing
        domain errors (:mod:`sandloom_billing.exceptions`).
        """
        BillingService.validate_credit_operation(amount, operation_key, allow_zero=True)
        customer = await EntitlementsService.find_customer(tenant_type, tenant_id, db)
        if customer is None:
            if amount != 0:
                raise InsufficientCreditsError()
            customer = await BillingService.get_or_create_customer(
                db,
                **(
                    {"organization_id": tenant_id}
                    if tenant_type == TENANT_ORGANIZATION
                    else {"user_id": tenant_id}
                ),
            )
        return await BillingService.spend_credits(
            customer, amount, operation_key, db, reason=reason
        )


def ensure_tenant_matches(tenant_type: str, tenant_id: UUID) -> None:
    """Reject a spend / balance read for another account than the caller's (403).

    An organization must be the bound tenant; a PERSONAL account must be the
    end user the call runs for, when the (signed) call context carries one
    (Public API 1.0 spec §7.4). System and service callers are unchanged. See
    the module docstring for the rule (single-tenant).
    """
    if tenant_type != TENANT_ORGANIZATION:
        caller = contract_session().info.get(CALLER_USER_INFO_KEY)
        if caller is not None and caller != tenant_id:
            raise BillingTenantMismatchError()
        return
    if getattr(settings, "tenancy_mode", "multi") == "single":
        return
    bound = current_tenant_id_optional()
    if bound is not None and bound != tenant_id:
        raise BillingTenantMismatchError()
