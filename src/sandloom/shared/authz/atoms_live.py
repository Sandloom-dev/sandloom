"""Tier-3 atoms: checks that need a live service call.

These resolve a sync service contract at evaluation time. They are only reached
when cheaper tiers haven't already decided the policy (the algebra evaluates
tier 1/2 first), so e.g. ``Role("admin") | Credits(10)`` never calls billing for
an admin.

Resolution goes through ``core.get_service`` — the local provider when the
module is co-deployed, otherwise the generated RPC proxy from
``SERVICE_BINDINGS``. When neither exists the atom is *indeterminate* with
status 501 (Not Implemented): an explicit deny decision (never an
``HTTPException`` raised from the atom — these run in workers too), which
``Not``/``Or`` cannot turn into an allow.

Which contract an atom talks to, and how the provider's methods are called, is
an adapter (:mod:`sandloom.shared.authz.live_contracts`): the defaults bind the
bundled ``billing.entitlements.v1`` / ``tenancy.membership.v1`` contracts.

The atoms never hand the provider the request's session: a contract call runs
in the provider's OWN Unit of Work on its own database, so the answer is the
same whether the provider is co-deployed or a separate service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sandloom.shared.authz._resolve import (
    ContractUnavailable,
    resolve_contract,
    resolve_org_id,
)
from sandloom.shared.authz.account import (
    _MISSING,
    Account,
    account_org_params,
    require_account,
    resolve_account,
)
from sandloom.shared.authz.algebra import TIER_LIVE, Requirement
from sandloom.shared.authz.live_contracts import (
    ENTITLEMENTS,
    MEMBERSHIP,
    get_live_adapter,
)
from sandloom.shared.authz.results import AccessDecision, Category, PolicyIndeterminate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from uuid import UUID

    from sandloom.shared.authz.context import AuthzContext

TENANT_USER = "user"
TENANT_ORGANIZATION = "organization"


def _resolve_service(ctx: AuthzContext, name: str, what: str, requirement: str) -> Any:
    """Resolve a sync service, or raise an indeterminate 501 decision.

    Only a genuinely *absent* service (no local provider, no remote binding)
    degrades to 501; an error raised *inside* a resolved service is allowed to
    propagate rather than being misreported as "not configured".
    """
    try:
        return resolve_contract(ctx, name)
    except ContractUnavailable as exc:
        raise PolicyIndeterminate(
            AccessDecision.error(
                code="service_unavailable",
                category=Category.POLICY_MISCONFIGURED,
                http_status=501,
                reason=f"{what} is not configured",
                failed_requirement=requirement,
                metadata={"service": name, "error": str(exc)},
            )
        ) from exc


class _TenantScopedLiveAtom(Requirement):
    """Shared account handling for entitlement atoms: the billing account is
    stated with ``of=`` (``Account.user()`` / ``Account.org(param=...)`` /
    ``Account.bound_tenant()``, spec §7), never inferred."""

    tier = TIER_LIVE
    category = Category.PAYMENT_REQUIRED

    def __init__(self, *, of: Account = _MISSING, adapter: Any = None) -> None:
        self.account = require_account(of, type(self).__name__)
        self._adapter = adapter

    @property
    def tenant_type(self) -> str:
        """``"user"`` for ``Account.user()``, else ``"organization"`` (the kind
        of a bound tenant is resolved per request)."""
        return TENANT_USER if self.account.kind == "user" else TENANT_ORGANIZATION

    def adapter(self) -> Any:
        return (
            self._adapter
            if self._adapter is not None
            else get_live_adapter(ENTITLEMENTS)
        )

    def org_params(self) -> frozenset[str]:
        return account_org_params(self.account)

    def contracts(self) -> frozenset[str]:
        return frozenset({str(self.adapter().service)})

    async def _tenant(self, ctx: AuthzContext) -> tuple[str, UUID]:
        # An organization comes from the request / tenant context, validated
        # against the token — NEVER the caller's user id.
        return await resolve_account(ctx, self.account, requirement=self.describe())

    def _service(self, ctx: AuthzContext) -> Any:
        adapter = self.adapter()
        service = _resolve_service(ctx, adapter.service, "Billing", self.describe())
        return adapter, service


class SubscriptionActive(_TenantScopedLiveAtom):
    """Subject's subscription is active right now (live, authoritative).

    Live counterpart to the claim-based :class:`~sandloom.shared.authz.atoms_claims.\
Subscribed`. Use this when staleness is unacceptable (e.g. gating immediately
    after a purchase); use ``Subscribed`` for the cheap, zero-I/O common path.
    ``of=`` names the billing account (spec §7).
    """

    async def evaluate(self, ctx: AuthzContext) -> bool:
        tenant_type, tenant_id = await self._tenant(ctx)
        adapter, service = self._service(ctx)
        return bool(
            await adapter.subscription_active(
                service, tenant_type=tenant_type, tenant_id=tenant_id, ctx=ctx
            )
        )

    def describe(self) -> str:
        return "active subscription"


class Credits(_TenantScopedLiveAtom):
    """Subject's live credit balance is at least ``minimum``.

    Reads the real-time balance via the entitlements contract (never a stale
    token claim — this guards money). ``of=`` names the billing account.
    """

    def __init__(
        self, minimum: int, *, of: Account = _MISSING, adapter: Any = None
    ) -> None:
        super().__init__(of=of, adapter=adapter)
        self.minimum = minimum

    async def evaluate(self, ctx: AuthzContext) -> bool:
        tenant_type, tenant_id = await self._tenant(ctx)
        adapter, service = self._service(ctx)
        balance = await adapter.credit_balance(
            service, tenant_type=tenant_type, tenant_id=tenant_id, ctx=ctx
        )
        return bool(balance >= self.minimum)

    def describe(self) -> str:
        return f"at least {self.minimum} credits"


class OrgRole(Requirement):
    """Subject holds at least ``minimum`` role in the request's organization.

    Relationship (ReBAC) authorization: org membership/role is per-organization
    and not carried in the global JWT, so it is resolved live through the
    membership contract (local call in a monolith, an RPC when tenancy is a
    separate service; 501 when neither). The organization id is read from the
    request path parameter ``org_param``, else ``X-Tenant-Id``, else the bound
    tenant; a missing organization is indeterminate (denies even under ``Not``).

    For a hot path, a module may instead maintain an authorization projection of
    memberships and gate on a projection-backed :class:`Entitled` fact.
    """

    tier = TIER_LIVE

    def __init__(
        self, minimum: str, *, org_param: str = "org_id", adapter: Any = None
    ) -> None:
        if minimum not in {"member", "admin", "owner"}:
            raise ValueError("OrgRole minimum must be member, admin or owner")
        self.minimum = minimum
        self.org_param = org_param
        self._adapter = adapter

    def org_params(self) -> frozenset[str]:
        return frozenset({self.org_param})

    def risks(self) -> frozenset[str]:
        return frozenset({"admin"}) if self.minimum != "member" else frozenset()

    def contracts(self) -> frozenset[str]:
        return frozenset({str(self.adapter().service)})

    def adapter(self) -> Any:
        return (
            self._adapter if self._adapter is not None else get_live_adapter(MEMBERSHIP)
        )

    async def evaluate(self, ctx: AuthzContext) -> bool:
        org_id = resolve_org_id(
            ctx,
            org_param=self.org_param,
            requirement=self.describe(),
            # Membership itself is the authoritative check (the token's tenant
            # allowlist may lag a fresh join), so no token validation here.
            validate_against_token=False,
        )
        adapter = self.adapter()
        service = _resolve_service(
            ctx, adapter.service, "Tenancy membership", self.describe()
        )
        return bool(
            await adapter.has_role_at_least(
                service,
                org_id=org_id,
                user_id=ctx.subject.user_id,
                minimum=self.minimum,
                ctx=ctx,
            )
        )

    def describe(self) -> str:
        return f"organization role '{self.minimum}'"
