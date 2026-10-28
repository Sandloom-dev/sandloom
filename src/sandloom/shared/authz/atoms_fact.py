"""Fact-backed atom: ``Entitled`` resolves a registered :class:`AccessFact`.

Unlike the fixed-tier atoms (``Subscribed`` is always claim, ``Credits`` is
always live), ``Entitled("subscription.active")`` defers *how* the fact is
answered to configuration — claim / projection / live / auto — so the same
policy travels from monolith to a split deployment without an edit: in a
process without the owner the fact is looked up through the owner's bound
``<owner>.facts.v1`` contract (same claim rule, live answer from the owner).
It is tiered ``TIER_LIVE`` for composition ordering (worst case), so a cheaper
sibling atom still decides an ``And`` / ``Or`` before any fact resolution runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    """

    tier = TIER_LIVE

    def __init__(
        self,
        fact_name: str,
        *,
        category: str | None = None,
        code: str | None = None,
        mode: str | None = None,
    ) -> None:
        self.fact_name = fact_name
        # Explicit override; None defers to the fact's deny_category.
        self._explicit_category = category
        self.category = category or Category.FORBIDDEN
        self.code = code
        self._mode = mode

    def contracts(self) -> frozenset[str]:
        return frozenset({fact_owner_contract(self.fact_name)})

    async def evaluate(self, ctx: AuthzContext) -> bool:
        # Raises PolicyIndeterminate on misconfiguration — never a bare False
        # that a negation could invert into an allow.
        return await self._evaluate_via_decide(ctx)

    async def decide(self, ctx: AuthzContext) -> AccessDecision:
        try:
            # Local registration, else the owner's public <owner>.facts.v1.
            fact = await lookup_access_fact(ctx, self.fact_name)
        except FactOwnerUnreachable as exc:
            return AccessDecision.error(
                code="fact_owner_unavailable",
                category=Category.POLICY_MISCONFIGURED,
                http_status=503,
                reason=str(exc),
                failed_requirement=self.describe(),
            )
        if fact is None:
            # Indeterminate: neither registered here nor described by any
            # bound owner — not implemented in THIS deployment (501). It must
            # deny even when negated (``~Entitled("account.suspended")``).
            return AccessDecision.error(
                code="fact_unregistered",
                category=Category.POLICY_MISCONFIGURED,
                http_status=501,
                reason=(
                    f"access fact {self.fact_name!r} is not registered and no "
                    "bound <owner>.facts.v1 contract describes it"
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
