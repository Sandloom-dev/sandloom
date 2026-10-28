"""Tier-1 atoms: pure checks over JWT claims (zero I/O).

Every atom here reads only :class:`UserContext` data carried in the access
token, so they evaluate without any database or network access. Claims are
populated at mint time by per-module contributors (see ``core.claims``).

The entitlement-style atoms (``Subscribed`` / ``Feature`` / ``WithinLimit``)
read a *named* claim (``claim_key``) from the subject's namespaced ``claims``
mapping; the defaults match the claims the bundled billing module contributes,
and any module may point them at its own claim.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sandloom.shared.authz.account import (
    _MISSING,
    Account,
    account_org_params,
    require_account,
    resolve_account,
)
from sandloom.shared.authz.algebra import TIER_CLAIM, TIER_LIVE, Requirement
from sandloom.shared.authz.atoms_live import SubscriptionActive
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.facts import fact_owner_contract
from sandloom.shared.authz.results import AccessDecision, Category, PolicyIndeterminate

#: Subscription statuses that count as "paying / entitled".
ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})


class Perm(Requirement):
    """Subject holds the fine-grained permission ``perm``.

    The permission is stored as ``perm`` — ``code`` stays the machine-readable
    denial code of the atom (``"Perm"``), not the permission name.
    """

    tier = TIER_CLAIM

    def __init__(self, perm: str) -> None:
        self.perm = perm

    async def evaluate(self, ctx: AuthzContext) -> bool:
        return self.perm in ctx.subject.permissions

    def describe(self) -> str:
        return f"permission '{self.perm}'"


class Role(Requirement):
    """Subject holds the global role ``role``.

    The role is stored as ``role`` — ``code`` stays the machine-readable denial
    code of the atom (``"Role"``), not the role name.
    """

    tier = TIER_CLAIM

    def __init__(self, role: str) -> None:
        self.role = role

    async def evaluate(self, ctx: AuthzContext) -> bool:
        return self.role in ctx.subject.roles

    def risks(self) -> frozenset[str]:
        return frozenset({"admin"}) if self.role == "admin" else frozenset()

    def describe(self) -> str:
        return f"role '{self.role}'"


#: Fact whose claim interpretation ``Subscribed()`` shares (billing registers it).
SUBSCRIPTION_FACT = "subscription.active"


class Subscribed(Requirement):
    """Subject's subscription grants access (coarse, claim-based).

    ``of=Account.user()``: reads the token's
    ``subscription_status`` claim with ZERO I/O, interpreted by the
    ``subscription.active`` access fact — billing's declarative rule, the same
    predicate as the live check (``active``/``trialing`` pass; ``past_due``
    passes during the grace period unless ``BILLING_DENY_ON_PAST_DUE``). The
    fact is the local registration or, in a process without billing, the one
    billing publishes over ``billing.facts.v1`` (described once, cached).

    Provenance is honoured: when the token's authoritative provenance shows
    billing did NOT contribute (it failed at mint, or the issuer could not
    reach it) the claim proves nothing, so the atom escalates to billing's LIVE
    answer instead of denying 402 for the token's whole lifetime (503
    indeterminate when no live path exists). With no subscription owner in
    the deployment at all, only ``active``/``trialing`` pass. May be up to the
    token TTL stale; for real-time / money-sensitive checks use
    :class:`~sandloom.shared.authz.atoms_live.SubscriptionActive` / ``Credits``.

    ``of=Account.org(...)``: the token carries NO per-organization
    subscription claim (claims are personal-only), so this atom resolves LIVE
    — it delegates to ``SubscriptionActive(of=...)`` and is a tier-3 atom.
    ``of=Account.bound_tenant()`` resolves the bound tenant's kind first
    (personal → the claim, organization → live).

    Passing explicit ``statuses`` pins the accepted set verbatim (no grace
    interpretation).
    """

    tier = TIER_CLAIM
    category = Category.PAYMENT_REQUIRED

    def __init__(
        self,
        statuses: frozenset[str] | None = None,
        *,
        of: Account = _MISSING,
        claim_key: str = "subscription_status",
    ) -> None:
        self.account = require_account(of, "Subscribed")
        self._explicit_statuses = statuses is not None
        self.statuses = (
            statuses if statuses is not None else ACTIVE_SUBSCRIPTION_STATUSES
        )
        self.claim_key = claim_key
        if self.account.kind != "user":
            self.tier = TIER_LIVE

    @property
    def tenant_type(self) -> str:
        return "user" if self.account.kind == "user" else "organization"

    def org_params(self) -> frozenset[str]:
        return account_org_params(self.account)

    def contracts(self) -> frozenset[str]:
        needed = {fact_owner_contract(SUBSCRIPTION_FACT)}
        if self.account.kind != "user":
            needed |= SubscriptionActive(of=self.account).contracts()
        return frozenset(needed)

    def _verbatim(self, status: object) -> bool:
        return isinstance(status, str) and status in self.statuses

    def _indeterminate(self, code: str, reason: str, status: int) -> Exception:
        return PolicyIndeterminate(
            AccessDecision.error(
                code=code,
                category=Category.POLICY_MISCONFIGURED,
                http_status=status,
                reason=reason,
                failed_requirement=self.describe(),
            )
        )

    async def _via_fact(self, ctx: AuthzContext) -> bool:
        """Answer through the ``subscription.active`` fact (local or remote).

        The fact owns the interpretation (billing's ``past_due`` grace rule);
        ``shared`` does not duplicate it. Provenance decides the path:

        * the owner contributed to this token → its claim, read with the
          fact's rule (zero I/O);
        * authoritative provenance WITHOUT the owner (it failed at mint, or was
          not reachable by the issuer) → the claim proves nothing, so escalate
          to the owner's LIVE answer instead of denying for the token's whole
          lifetime; no live path → indeterminate 503;
        * legacy token (no provenance) → the claim with the fact's rule.
        """
        from sandloom.shared.authz.facts import (  # noqa: PLC0415 - no cycle
            MODE_LIVE,
            FactDenied,
            FactOwnerUnreachable,
            FactUnavailable,
            _claim_trustworthy,
            lookup_access_fact,
            resolve_fact,
        )

        status = ctx.subject.claim(self.claim_key)
        try:
            fact = await lookup_access_fact(ctx, SUBSCRIPTION_FACT)
        except FactOwnerUnreachable as exc:
            raise self._indeterminate("fact_owner_unavailable", str(exc), 503) from exc
        if fact is None:
            # No subscription owner anywhere in this deployment: the strict
            # status set (the grace policy belongs to the owner's fact).
            return self._verbatim(status)
        subject = ctx.subject
        if _claim_trustworthy(fact, ctx) or not subject.provenance_authoritative:
            return fact.interpret_claim(status)
        try:
            return await resolve_fact(fact, ctx, mode=MODE_LIVE)
        except FactDenied:
            return False
        except FactUnavailable as exc:
            raise self._indeterminate(
                "subscription_unverifiable",
                f"subscription claim not minted by {fact.owner_module!r} and no "
                f"live check is available: {exc}",
                503,
            ) from exc

    async def evaluate(self, ctx: AuthzContext) -> bool:
        if self.account.kind != "user":
            tenant_type, _tenant = await resolve_account(
                ctx, self.account, requirement=self.describe()
            )
            if tenant_type != "user":
                live = SubscriptionActive(of=self.account)
                return await live.evaluate(ctx)
        if self._explicit_statuses or self.claim_key != "subscription_status":
            return self._verbatim(ctx.subject.claim(self.claim_key))
        return await self._via_fact(ctx)

    def describe(self) -> str:
        return "active subscription"


class _AccountClaimAtom(Requirement):
    """A plan atom read from the caller's claims for ``Account.user()`` and
    from the owner's organization fact for an organization account (spec
    §7.2): ``organization.features`` / ``organization.limits``, evaluated
    with the resource ``{"organization_id", "key", ...}``; without a
    registered fact the decision is indeterminate (501)."""

    tier = TIER_CLAIM
    org_fact: str = ""

    def __init__(self, atom: str, of: Any) -> None:
        self.account = require_account(of, atom)
        if self.account.kind != "user":
            self.tier = TIER_LIVE

    def org_params(self) -> frozenset[str]:
        return account_org_params(self.account)

    def _from_claims(self, ctx: AuthzContext) -> bool:
        raise NotImplementedError

    def _resource(self, org_id: UUID) -> dict[str, Any]:
        raise NotImplementedError

    async def evaluate(self, ctx: AuthzContext) -> bool:
        if self.account.kind == "user":
            return self._from_claims(ctx)
        tenant_type, tenant_id = await resolve_account(
            ctx, self.account, requirement=self.describe()
        )
        if tenant_type == "user":
            return self._from_claims(ctx)
        return await self._via_org_fact(ctx, tenant_id)

    async def _via_org_fact(self, ctx: AuthzContext, org_id: UUID) -> bool:
        from sandloom.shared.authz.facts import (  # noqa: PLC0415 - no cycle
            MODE_LIVE,
            FactDenied,
            FactOwnerUnreachable,
            FactUnavailable,
            lookup_access_fact,
            resolve_fact,
        )

        try:
            fact = await lookup_access_fact(ctx, self.org_fact)
        except FactOwnerUnreachable as exc:
            raise _indeterminate(self, "fact_owner_unavailable", str(exc), 503) from exc
        if fact is None:
            raise _indeterminate(
                self,
                "fact_unregistered",
                f"access fact {self.org_fact!r} is not registered: an "
                "organization's plan needs its owner's fact",
                501,
            )
        scoped = AuthzContext(
            ctx.subject,
            request=ctx.request,
            db=ctx.db,
            resource=self._resource(org_id),
            source=ctx.source,
            core=ctx.core(),
        )
        try:
            return await resolve_fact(fact, scoped, mode=MODE_LIVE)
        except FactDenied:
            return False
        except FactUnavailable as exc:
            raise _indeterminate(self, "fact_unavailable", str(exc), 503) from exc


def _indeterminate(atom: Requirement, code: str, reason: str, status: int) -> Exception:
    return PolicyIndeterminate(
        AccessDecision.error(
            code=code,
            category=Category.POLICY_MISCONFIGURED,
            http_status=status,
            reason=reason,
            failed_requirement=atom.describe(),
        )
    )


class Feature(_AccountClaimAtom):
    """The account's plan enables the feature flag ``key`` (``of=``, §7.2)."""

    org_fact = "organization.features"

    def __init__(
        self, key: str, *, of: Account = _MISSING, claim_key: str = "features"
    ) -> None:
        super().__init__("Feature", of)
        self.key = key
        self.claim_key = claim_key

    def _from_claims(self, ctx: AuthzContext) -> bool:
        flags = ctx.subject.claim(self.claim_key)
        return isinstance(flags, dict) and flags.get(self.key) is True

    def _resource(self, org_id: UUID) -> dict[str, Any]:
        return {"organization_id": org_id, "key": self.key}

    def describe(self) -> str:
        return f"feature '{self.key}'"


class WithinLimit(_AccountClaimAtom):
    """``used`` is strictly below the account's plan limit for ``key``.

    For ``Account.user()`` the limit comes from the token claim; ``used`` is
    the current count the caller already knows (e.g. number of seats). Absent
    / non-integer limit ⇒ treated as 0 (i.e. denied), so a missing entitlement
    fails closed.
    """

    category = Category.QUOTA_EXCEEDED
    org_fact = "organization.limits"

    def __init__(
        self,
        key: str,
        used: int,
        *,
        of: Account = _MISSING,
        claim_key: str = "limits",
    ) -> None:
        super().__init__("WithinLimit", of)
        self.key = key
        self.used = used
        self.claim_key = claim_key

    def _from_claims(self, ctx: AuthzContext) -> bool:
        limits = ctx.subject.claim(self.claim_key)
        raw = limits.get(self.key) if isinstance(limits, dict) else None
        if isinstance(raw, bool) or not isinstance(raw, int):
            return False
        return self.used < raw

    def _resource(self, org_id: UUID) -> dict[str, Any]:
        return {"organization_id": org_id, "key": self.key, "used": self.used}

    def describe(self) -> str:
        return f"within limit '{self.key}'"
