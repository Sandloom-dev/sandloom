"""Fact-backed atom: ``Entitled`` resolves a registered :class:`AccessFact`.

Unlike a generated atom (its tier is the provider's declaration),
``Entitled("subscription.active", owner="billing")`` defers *how* the fact is
answered to configuration — claim / projection / live / auto — so the same
policy travels from monolith to a split deployment without an edit: in a
process without the owner the fact is looked up through the owner's bound
``<owner>.facts.v1`` contract (same claim rule, live answer from the owner).
It is tiered ``TIER_LIVE`` for composition ordering (worst case), so a cheaper
sibling atom still decides an ``And`` / ``Or`` before any fact resolution runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sandloom.core.descriptors.module_names import require_fact_owner
from sandloom.shared.authz.algebra import TIER_LIVE, Requirement
from sandloom.shared.authz.facts import (
    FactDenied,
    FactOwnerUnreachable,
    FactUnavailable,
    fact_owner_contract,
    lookup_access_fact,
    resolve_fact,
)
from sandloom.shared.authz.results import AccessDecision, Category

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.shared.authz.context import AuthzContext


class Entitled(Requirement):
    """Subject satisfies the registered access fact ``fact_name``.

    Args:
        fact_name: name of an :class:`AccessFact` registered via
            ``core.register_access_fact(...)``.
        category: denial category when the fact is False. Defaults to the
            fact's own ``deny_category`` (billing's subscription facts declare
            ``Category.PAYMENT_REQUIRED`` → 402), else ``forbidden`` (403).
            A resolver raising ``FactDenied`` picks its own category.
        mode: force a resolution mode, overriding the ``ACCESS`` setting (for
            tests / special endpoints). Normally left ``None``.
        owner: required, the module that owns the fact (its
            ``owner_module``): a fact is identified by its owner and its
            name, never by its name alone. Only that module's fact is
            accepted, local or described by that module's bound
            ``<owner>.facts.v1`` (the contract the policy names); another
            module's fact of the same name never satisfies it. Missing,
            empty or not a module name raises ``InvalidFactOwner``
            (SANDLOOM-B038) here, before any request. The kernel keeps no
            table of fact owners: ``Entitled("subscription.active",
            owner="billing")``.
    """

    tier = TIER_LIVE

    def __init__(
        self,
        fact_name: str,
        *,
        owner: str,
        category: str | None = None,
        code: str | None = None,
        mode: str | None = None,
    ) -> None:
        self.owner = require_fact_owner(owner, fact_name, "Entitled")
        self.fact_name = fact_name
        # Explicit override; None defers to the fact's deny_category.
        self._explicit_category = category
        self.category = category or Category.FORBIDDEN
        self.code = code
        self._mode = mode

    def contracts(self) -> frozenset[str]:
        return frozenset({fact_owner_contract(self.owner)})

    async def evaluate(self, ctx: AuthzContext) -> bool:
        # Raises PolicyIndeterminate on misconfiguration — never a bare False
        # that a negation could invert into an allow.
        return await self._evaluate_via_decide(ctx)

    async def decide(self, ctx: AuthzContext) -> AccessDecision:
        try:
            # The owner's local registration, else its <owner>.facts.v1; no
            # other module's fact of this name.
            fact = await lookup_access_fact(ctx, self.fact_name, owner=self.owner)
        except FactOwnerUnreachable as exc:
            return AccessDecision.error(
                code="fact_owner_unavailable",
                category=Category.POLICY_MISCONFIGURED,
                http_status=503,
                reason=str(exc),
                failed_requirement=self.describe(),
            )
        if fact is None:
            # Indeterminate: the owner neither registered it here nor
            # describes it over its bound contract — not implemented in THIS
            # deployment (501). It must deny even when negated
            # (``~Entitled("account.suspended", owner="accounts")``).
            return AccessDecision.error(
                code="fact_unregistered",
                category=Category.POLICY_MISCONFIGURED,
                http_status=501,
                reason=(
                    f"access fact {self.fact_name!r} of module {self.owner!r} "
                    "is not registered here and its bound "
                    f"{fact_owner_contract(self.owner)} does not describe it"
                ),
                failed_requirement=self.describe(),
            )
        try:
            satisfied = await resolve_fact(fact, ctx, mode=self._mode)
        except FactDenied as exc:
            return AccessDecision.deny(
                code=exc.code or self.code or self.fact_name,
                category=exc.category,
                reason=exc.reason,
                failed_requirement=self.describe(),
            )
        except FactUnavailable as exc:
            return AccessDecision.error(
                code="fact_unavailable",
                category=Category.POLICY_MISCONFIGURED,
                http_status=501,
                reason=str(exc),
                failed_requirement=self.describe(),
            )
        if satisfied:
            return AccessDecision.allow()
        category = self._explicit_category or fact.deny_category
        return AccessDecision.deny(
            code=self.code or self.fact_name,
            category=category or Category.FORBIDDEN,
            reason=f"requires {self.describe()}",
            failed_requirement=self.describe(),
        )

    def describe(self) -> str:
        return f"entitlement '{self.fact_name}'"
