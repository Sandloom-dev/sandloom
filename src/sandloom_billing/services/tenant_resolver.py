"""Tenant context resolution (BILLING_SPEC §6, §8).

Rules:
- explicit org_id  → organization context; caller must be a member (403 if not,
  501 if the membership contract is absent from the deployment);
- no org_id        → the caller's PERSONAL context, always. The account is
                     stated, never inferred (Public API 1.0 spec §7.4, owner
                     decision 1): membership of exactly one organization no
                     longer selects that organization's account.

Membership is resolved through the service registry contract
`tenancy.membership.v1` — billing never imports tenancy code. The contract
runs in tenancy's OWN read-only Unit of Work (locally or over internal RPC when
tenancy is a separate service bound via ``SERVICE_BINDINGS``), never in
billing's transaction. An explicit ``org_id`` without that contract answers
**501** (the deployment forgot the binding).

Any OTHER failure (tenancy unreachable, provider error) propagates instead of
silently degrading to the personal tenant.

Reading an organization's billing (subscription, credits) needs membership;
MANAGING it (checkout, customer portal) needs ``minimum_role`` — owner/admin
(:data:`BILLING_MANAGER_ROLE`) — so a plain member cannot buy plans or open the
org's payment portal.

Class F (ADR-0003, gap G1): checkout / customer portal call an external PSP,
which no database transaction can roll back. They authorize with
``capture_authority=True`` — the role AND the per-(org, user) membership
``version`` from ``get_member_state`` — and call :func:`reverify_authority`
AFTER the PSP call, right before the operation intent is issued
(``services.operation_intents``). A demotion / removal committed meanwhile is
``403``; any other membership change (the ``version`` moved) is ``409``
(retry). The intent is then revoked, a checkout session expired at the PSP,
and the URL never delivered. The re-check runs immediately before the
one-statement issue transaction: serializable, not linearizable — a change
committing between it and that COMMIT is not seen (ADR-0003 "Guarantees,
precisely").

Isolation decision (finding: billing models are not ``TenantScoped``): billing
customers are keyed by ``user_id`` XOR ``organization_id`` — two different
tenant kinds — and every billing read starts from a customer resolved HERE (or
by the PSP customer id in system webhook code). This resolver is therefore the
single tenant gate; tests pin that one tenant can never read another's billing.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.contracts import invalidate_contract_read_memo
from sandloom.sdk.errors import ServiceNotFound
from sandloom.shared.contracts.tenancy_dto import MemberState
from sandloom.shared.subject import UserContext
from sandloom_billing.models import BillingCustomer
from sandloom_billing.services.billing_service import BillingService

TENANT_USER = "user"
TENANT_ORGANIZATION = "organization"

MEMBERSHIP_SERVICE_V1 = "tenancy.membership.v1"

#: Minimum organization role allowed to manage the org's billing.
BILLING_MANAGER_ROLE = "admin"

#: The ``tenancy.membership.v1`` role ladder, lowest first, used to compare the
#: role ONE ``get_role`` call returned instead of a second
#: ``has_role_at_least`` contract call (each is its own Unit of Work on the
#: tenancy database). A role or minimum this table does not know falls back to
#: the authoritative contract call, so a new tenancy role can never be
#: misjudged here (fail-closed: the contract decides).
_ROLE_RANK = {"member": 0, "admin": 1, "owner": 2}


@dataclass(frozen=True, slots=True)
class MembershipAuthority:
    """The organization authority a class-F action was authorized on."""

    organization_id: UUID
    user_id: UUID
    minimum_role: str
    #: ``tenancy.membership.v1.get_member_state(...).version`` at authorization.
    version: int


@dataclass(slots=True)
class TenantContext:
    tenant_type: str  # "user" | "organization"
    tenant_id: UUID
    user_id: UUID  # acting user (always)
    #: Set by ``resolve_tenant(capture_authority=True)`` for an organization
    #: context with a ``minimum_role``; re-checked by :func:`reverify_authority`.
    authority: MembershipAuthority | None = None


def _get_membership_service(core: Any) -> Any | None:
    """The membership contract, or None when absent from this deployment."""
    try:
        return core.get_service(MEMBERSHIP_SERVICE_V1)
    except ServiceNotFound:  # incl. ServiceUnavailableInDeployment (501)
        return None


def _membership_not_configured() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Organization context is not available in this deployment: "
            f"{MEMBERSHIP_SERVICE_V1!r} is neither provided locally nor bound "
            "(SERVICE_BINDINGS)"
        ),
    )


async def resolve_tenant(
    *,
    core: Any,
    user_id: UUID,
    org_id: UUID | None,
    db: AsyncSession | None = None,
    minimum_role: str | None = None,
    subject: UserContext | None = None,
    capture_authority: bool = False,
) -> TenantContext:
    """Resolve the billing tenant for the acting user.

    ``db`` (billing's own session, optional and unused here) is NOT passed to
    the tenancy contract (which runs in tenancy's own Unit of Work).
    ``minimum_role`` (e.g. :data:`BILLING_MANAGER_ROLE`) is required for an
    organization context; ``None`` accepts any member (read access).
    ``subject`` (the authenticated token context) is accepted for the
    callers' convenience; the account no longer depends on it.
    ``capture_authority`` (class F, with a ``minimum_role``): read the role
    together with the membership ``version`` (``get_member_state``, one call)
    and return it as ``authority`` for :func:`reverify_authority`.
    """
    membership = _get_membership_service(core)
    capture = capture_authority and minimum_role is not None

    if org_id is not None:
        if membership is None:
            raise _membership_not_configured()
        version: int | None = None
        if capture:
            state = await _member_state(membership, org_id, user_id)
            role, version = state.role, state.version
        else:
            role = await membership.get_role(org_id, user_id)
        if role is None:
            raise _no_access()
        await _require_role(membership, org_id, user_id, minimum_role, held=role)
        return TenantContext(
            tenant_type=TENANT_ORGANIZATION,
            tenant_id=org_id,
            user_id=user_id,
            authority=_authority(org_id, user_id, minimum_role, version),
        )

    # No org_id: the PERSONAL account, always (spec §7.4, owner decision 1).
    # A member of exactly one organization is never silently given the
    # organization's account; an organization account needs org_id.
    return TenantContext(tenant_type=TENANT_USER, tenant_id=user_id, user_id=user_id)


async def _member_state(membership: Any, org_id: UUID, user_id: UUID) -> MemberState:
    """``get_member_state`` as the generated DTO copy, locally and over RPC.

    ``membership`` is the L2 provider (``service``): a local call returns
    tenancy's own class, an RPC call the generated copy (or plain data, an
    undescribed contract); either is validated into the copy this module
    imports rather than trusted as-is.
    """
    state = await membership.get_member_state(org_id, user_id)
    if isinstance(state, MemberState):
        return state
    if isinstance(state, BaseModel):
        state = state.model_dump()
    return MemberState.model_validate(state)


def _no_access() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="No access to the requested organization",
    )


def _authority(
    org_id: UUID, user_id: UUID, minimum_role: str | None, version: int | None
) -> MembershipAuthority | None:
    if minimum_role is None or version is None:
        return None
    return MembershipAuthority(
        organization_id=org_id,
        user_id=user_id,
        minimum_role=minimum_role,
        version=version,
    )


async def reverify_authority(core: Any, ctx: TenantContext) -> None:
    """Re-check a class-F authorization AFTER its external effect (G1).

    Re-reads the owner's CURRENT membership state — never the request's
    memoised answer (the contract-read memo is cleared first) — and refuses
    to hand out the result when it no longer holds: ``403`` when the user lost
    access / the required role, ``409`` when the membership changed in any
    other way (the ``version`` moved). A context without ``authority``
    (personal tenant, read routes) is a no-op.
    """
    authority = ctx.authority
    if authority is None:
        return
    membership = _get_membership_service(core)
    if membership is None:  # the binding vanished mid-request: fail closed
        raise _membership_not_configured()
    invalidate_contract_read_memo()
    state = await _member_state(
        membership, authority.organization_id, authority.user_id
    )
    if state.role is None:
        raise _no_access()
    await _require_role(
        membership,
        authority.organization_id,
        authority.user_id,
        authority.minimum_role,
        held=state.role,
    )
    if state.version != authority.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The organization membership changed while the request was "
                "processed; retry"
            ),
        )


def _local_role_verdict(held: str | None, minimum: str) -> bool | None:
    """``held >= minimum`` from the known ladder, or ``None`` (ask the contract)."""
    if held is None or held not in _ROLE_RANK or minimum not in _ROLE_RANK:
        return None
    return _ROLE_RANK[held] >= _ROLE_RANK[minimum]


async def _require_role(
    membership: Any,
    org_id: UUID,
    user_id: UUID,
    minimum_role: str | None,
    *,
    held: str | None = None,
) -> None:
    """403 unless the user holds at least ``minimum_role`` in the organization.

    ``held`` is the role a ``get_role`` call of THIS request already returned:
    compared locally when both roles are on the known ladder (one contract
    call instead of two); otherwise the contract's ``has_role_at_least``
    decides.
    """
    if minimum_role is None:
        return
    allowed = _local_role_verdict(held, minimum_role)
    if allowed is None:
        allowed = bool(
            await membership.has_role_at_least(org_id, user_id, minimum_role)
        )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Managing organization billing requires the '{minimum_role}' role"
            ),
        )


async def resolve_customer(
    *,
    core: Any,
    user_id: UUID,
    org_id: UUID | None,
    db: AsyncSession,
    minimum_role: str | None = None,
    subject: UserContext | None = None,
) -> BillingCustomer:
    """Resolve tenant context and return (creating if needed) its customer."""
    _, customer = await resolve_customer_context(
        core=core,
        user_id=user_id,
        org_id=org_id,
        db=db,
        minimum_role=minimum_role,
        subject=subject,
    )
    return customer


async def resolve_customer_context(
    *,
    core: Any,
    user_id: UUID,
    org_id: UUID | None,
    db: AsyncSession,
    minimum_role: str | None = None,
    subject: UserContext | None = None,
    capture_authority: bool = False,
) -> tuple[TenantContext, BillingCustomer]:
    """:func:`resolve_customer` that also returns the tenant context (with the
    captured ``authority`` for :func:`reverify_authority`)."""
    ctx = await resolve_tenant(
        core=core,
        user_id=user_id,
        org_id=org_id,
        db=db,
        minimum_role=minimum_role,
        subject=subject,
        capture_authority=capture_authority,
    )
    if ctx.tenant_type == TENANT_USER:
        customer = await BillingService.get_or_create_customer(
            db, user_id=ctx.tenant_id
        )
    else:
        customer = await BillingService.get_or_create_customer(
            db, organization_id=ctx.tenant_id
        )
    return ctx, customer
