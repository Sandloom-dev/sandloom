"""The composable authorization algebra.

A :class:`Requirement` is a predicate over an :class:`AuthzContext`.
Requirements compose with ``&`` / ``|`` / ``~`` and evaluate **cheapest-tier
first** with short-circuit, so claim checks (tier 1, zero I/O) run before any
resource load (tier 2) or live service call (tier 3). A combinator only touches
an expensive atom when the cheap ones haven't already decided the outcome.

Tiers:
- ``TIER_CLAIM``    (1) — pure check over JWT claims, zero I/O.
- ``TIER_RESOURCE`` (2) — needs the request resource (loaded once, lazily).
- ``TIER_LIVE``     (3) — needs a live service call (billing, …).

Three-valued logic (fail closed): an atom that cannot be evaluated at all
(fact unregistered, service absent, tenant context missing) yields an
*indeterminate* :class:`AccessDecision` (``decide``) or raises
:class:`PolicyIndeterminate` (``evaluate``). Combinators propagate it:

- ``Not(x)``  — indeterminate stays indeterminate (never inverted into allow).
- ``And``     — any indeterminate operand reached ⇒ indeterminate (deny).
- ``Or``      — a *genuinely* satisfied operand allows; otherwise, if any
  operand was indeterminate the result is indeterminate (carrying its
  category/status, e.g. 501/500) rather than a plain deny.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

from sandloom.shared.authz.results import AccessDecision, Category, PolicyIndeterminate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sandloom.shared.authz.context import AuthzContext

TIER_CLAIM = 1
TIER_RESOURCE = 2
TIER_LIVE = 3


class Requirement(abc.ABC):
    """An authorization predicate, composable with ``& | ~``.

    ``evaluate`` is the primitive boolean check (it raises
    :class:`PolicyIndeterminate` when it cannot answer). ``decide`` wraps it
    into a structured :class:`AccessDecision` (the engine/enforcement path uses
    ``decide`` so a denial carries a machine-readable code/category and the
    failing requirement); the two never disagree.
    """

    #: Evaluation cost class; combinators evaluate lower tiers first.
    tier: int = TIER_CLAIM
    #: Denial category reported when this requirement fails (override per atom).
    category: str = Category.FORBIDDEN
    #: Machine-readable denial code; defaults to the class name when unset.
    code: str | None = None

    @abc.abstractmethod
    async def evaluate(self, ctx: AuthzContext) -> bool:
        """Return True iff the subject satisfies this requirement.

        Raise :class:`PolicyIndeterminate` (never return ``False``) when the
        requirement cannot be evaluated at all.
        """

    async def decide(self, ctx: AuthzContext) -> AccessDecision:
        """Structured result. Default: allow iff ``evaluate`` is True."""
        try:
            satisfied = await self.evaluate(ctx)
        except PolicyIndeterminate as exc:
            return exc.decision
        if satisfied:
            return AccessDecision.allow()
        return self._deny()

    async def _evaluate_via_decide(self, ctx: AuthzContext) -> bool:
        """``evaluate`` for requirements whose primitive is ``decide``."""
        decision = await self.decide(ctx)
        if decision.indeterminate:
            raise PolicyIndeterminate(decision)
        return decision.allowed

    def _deny(self) -> AccessDecision:
        return AccessDecision.deny(
            code=self.code or self.__class__.__name__,
            category=self.category,
            reason=f"requires {self.describe()}",
            failed_requirement=self.describe(),
        )

    def describe(self) -> str:
        """Human-readable label used in 403 detail messages."""
        return self.__class__.__name__

    def mentions_resource(self) -> bool:
        """True if evaluating this requirement needs the request resource.

        Used by ``require()`` to reject resource-scoped policies at wiring time
        (they belong in ``authorize_resource``/``authorize``), turning a runtime
        500 into a clear startup error.
        """
        return self.tier == TIER_RESOURCE

    def org_params(self) -> frozenset[str]:
        """Path parameters this requirement may read an organisation from.

        Non-empty for an atom that resolves an organisation (path parameter,
        ``X-Tenant-Id``, else the bound tenant): ``create_app`` then refuses a
        route that solves the gate before its ``TenantScope()`` (SANDLOOM-B007).
        """
        return frozenset()

    def contracts(self) -> frozenset[str]:
        """Sync contracts evaluating this requirement may call (wiring time).

        A module's derived ``consumes`` includes them (optional), so the
        startup preflight reports an unbound one and ``sandloom add deploy``
        binds it in a split deployment. Default: none (a claims-only atom).
        """
        return frozenset()

    def risks(self) -> frozenset[str]:
        """The framework risks a gate enforcing this requirement carries
        (ADR-0009 item 6): ``{"admin"}`` for an admin role, else none. An
        ``Or`` carries its branches' risks; a ``Not`` grants no authority."""
        return frozenset()

    def __and__(self, other: Requirement) -> Requirement:
        return And(self, other)

    def __or__(self, other: Requirement) -> Requirement:
        return Or(self, other)

    def __invert__(self) -> Requirement:
        return Not(self)


def _flatten(kind: type, parts: tuple[Requirement, ...]) -> list[Requirement]:
    """Collapse nested And/Or of the same kind into one flat operand list."""
    flat: list[Requirement] = []
    for part in parts:
        if isinstance(part, kind):
            flat.extend(part.parts)  # type: ignore[attr-defined]
        else:
            flat.append(part)
    return flat


class And(Requirement):
    """Conjunction: all operands must hold. Short-circuits on first non-allow.

    A definite deny or an indeterminate operand both stop evaluation and are
    returned as-is (both deny; the indeterminate keeps its 5xx category).
    """

    def __init__(self, *parts: Requirement) -> None:
        self.parts = _flatten(And, parts)
        self.tier = max((p.tier for p in self.parts), default=TIER_CLAIM)

    async def evaluate(self, ctx: AuthzContext) -> bool:
        return await self._evaluate_via_decide(ctx)

    async def decide(self, ctx: AuthzContext) -> AccessDecision:
        # Cheapest-tier first; the first non-allowing operand decides.
        for part in sorted(self.parts, key=lambda r: r.tier):
            decision = await part.decide(ctx)
            if not decision.allowed:
                return decision
        return AccessDecision.allow()

    def mentions_resource(self) -> bool:
        return any(p.mentions_resource() for p in self.parts)

    def org_params(self) -> frozenset[str]:
        return frozenset().union(*(p.org_params() for p in self.parts))

    def contracts(self) -> frozenset[str]:
        return frozenset().union(*(p.contracts() for p in self.parts))

    def risks(self) -> frozenset[str]:
        return frozenset().union(*(p.risks() for p in self.parts))

    def describe(self) -> str:
        return " AND ".join(p.describe() for p in self.parts)


class Or(Requirement):
    """Disjunction: any operand may hold. Short-circuits on first allow.

    Only a genuinely satisfied operand allows — and one that does allows even
    if a sibling is indeterminate (``Role("admin") | Entitled("x")`` admits an
    admin while the ``x`` fact is unregistered): the grant comes from the
    satisfied operand, never from inverting or guessing the broken one. When
    none allows and at least one operand was indeterminate, the disjunction is
    indeterminate (the first such decision is returned, preserving its
    category/status) — a broken branch is reported, not silently folded into a
    plain 403.
    """

    def __init__(self, *parts: Requirement) -> None:
        self.parts = _flatten(Or, parts)
        self.tier = max((p.tier for p in self.parts), default=TIER_CLAIM)

    async def evaluate(self, ctx: AuthzContext) -> bool:
        return await self._evaluate_via_decide(ctx)

    async def decide(self, ctx: AuthzContext) -> AccessDecision:
        denials: list[AccessDecision] = []
        for part in sorted(self.parts, key=lambda r: r.tier):
            decision = await part.decide(ctx)
            if decision.allowed:
                return decision
            denials.append(decision)
        for decision in denials:
            if decision.indeterminate:
                return decision
        # All operands definitively failed. If they agree on a category, keep
        # it; otherwise the disjunction is a generic forbidden.
        categories = {d.category for d in denials}
        category = denials[0].category if len(categories) == 1 else Category.FORBIDDEN
        return AccessDecision.deny(
            code="any_of_failed",
            category=category or Category.FORBIDDEN,
            reason=f"requires {self.describe()}",
            failed_requirement=self.describe(),
        )

    def mentions_resource(self) -> bool:
        return any(p.mentions_resource() for p in self.parts)

    def org_params(self) -> frozenset[str]:
        return frozenset().union(*(p.org_params() for p in self.parts))

    def contracts(self) -> frozenset[str]:
        return frozenset().union(*(p.contracts() for p in self.parts))

    def risks(self) -> frozenset[str]:
        return frozenset().union(*(p.risks() for p in self.parts))

    def describe(self) -> str:
        return "(" + " OR ".join(p.describe() for p in self.parts) + ")"


class Not(Requirement):
    """Negation of a single requirement.

    Negates only a *definite* answer: an indeterminate inner decision is
    returned unchanged (deny), so ``~Entitled("account.suspended")`` never
    allows because the fact is unregistered or its service is down.
    """

    def __init__(self, inner: Requirement) -> None:
        self.inner = inner
        self.tier = inner.tier

    async def evaluate(self, ctx: AuthzContext) -> bool:
        return await self._evaluate_via_decide(ctx)

    async def decide(self, ctx: AuthzContext) -> AccessDecision:
        inner = await self.inner.decide(ctx)
        if inner.indeterminate:
            return inner
        if inner.allowed:
            return AccessDecision.deny(
                code="negation_failed",
                category=Category.FORBIDDEN,
                reason=f"requires {self.describe()}",
                failed_requirement=self.describe(),
            )
        return AccessDecision.allow()

    def mentions_resource(self) -> bool:
        return self.inner.mentions_resource()

    def org_params(self) -> frozenset[str]:
        return self.inner.org_params()

    def contracts(self) -> frozenset[str]:
        return self.inner.contracts()

    def describe(self) -> str:
        return f"NOT {self.inner.describe()}"
