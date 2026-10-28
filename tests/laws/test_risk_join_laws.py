"""The risk join (spec §8.2; ADR-0009 "Risk is declared at its source").

A route's risk is the JOIN (set union) of the risks its sources declare:
framework gates (an admin role), risk-tagged dependencies
(``@sensitive_effect``) and ``sensitive(risk)`` markers. Pinned:

* on requirements: ``risks(a & b) = risks(a | b) = risks(a) ∪ risks(b)``,
  ``risks(~a) = ∅`` (a negation grants no authority), hence commutative,
  associative and idempotent;
* on routes: the resolved risk keys are the union of the contributors' risks,
  whatever their order or repetition, and each risk names its sources;
* the obligations are a homomorphism: the step-up the route derives is the
  minimum (the strictest) of its contributors' step-ups, so
  ``obligations(A ∪ B) = obligations(A) ⊓ obligations(B)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.routing import APIRoute
from hypothesis import given, settings
from hypothesis import strategies as st

from sandloom.core.route_policy import resolve_route_policy
from sandloom.sdk.authz import Role, require
from sandloom.sdk.effects import sensitive_effect
from sandloom.sdk.web import CurrentUser, sensitive
from sandloom.shared.authz import And, Not, Or, Requirement

LAWS = settings(max_examples=200, deadline=None)

# --------------------------------------------------------------------------- #
# Requirements
# --------------------------------------------------------------------------- #


class Tagged(Requirement):
    def __init__(self, risks: frozenset[str]) -> None:
        self._risks = risks

    async def evaluate(self, ctx: Any) -> bool:
        return True

    def risks(self) -> frozenset[str]:
        return self._risks


risk_sets = st.frozensets(st.sampled_from(["admin", "money", "security"]))
requirements = st.builds(Tagged, risk_sets)


@LAWS
@given(requirements, requirements, requirements)
def test_requirement_risks_are_a_union(
    a: Requirement, b: Requirement, c: Requirement
) -> None:
    union = a.risks() | b.risks()
    assert And(a, b).risks() == Or(a, b).risks() == union
    assert And(b, a).risks() == union  # commutative
    assert And(And(a, b), c).risks() == And(a, And(b, c)).risks()  # associative
    assert Or(a, a).risks() == a.risks()  # idempotent
    assert Not(a).risks() == frozenset()
    assert And(a, Not(b)).risks() == a.risks()


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Source:
    """One contributor: the dependency it adds, its risk and its step-up."""

    name: str
    risk: str
    step_up: int | None
    dependency: Callable[[], Any]


def _effect(risk: str, step_up: int | None, name: str) -> Source:
    @sensitive_effect(risk, step_up=step_up)  # type: ignore[arg-type]
    async def dep() -> None:
        return None

    dep.__qualname__ = name
    effective = (
        step_up if step_up is not None else (300 if risk == "security" else None)
    )
    return Source(name, risk, effective, lambda: Depends(dep))


SOURCES = [
    Source("admin gate", "admin", None, lambda: Depends(require(Role("admin")))),
    Source("sensitive money", "money", None, lambda: sensitive("money")),
    Source("sensitive security", "security", 300, lambda: sensitive("security")),
    _effect("money", None, "money_effect"),
    _effect("security", 120, "security_effect_120"),
    _effect("security", None, "security_effect_default"),
    _effect("admin", 60, "admin_effect_60"),
]


def _route(sources: list[Source]) -> APIRoute:
    router = APIRouter()

    @router.post("/probe", dependencies=[s.dependency() for s in sources])
    async def probe(user: CurrentUser) -> None:
        return None

    route = router.routes[0]
    assert isinstance(route, APIRoute)
    return route


@LAWS
@given(st.lists(st.sampled_from(SOURCES), max_size=6), st.randoms())
def test_route_risk_is_the_join_of_its_sources(sources: list[Source], rnd: Any) -> None:
    policy = resolve_route_policy(_route(sources))
    assert set(policy.risk) == {s.risk for s in sources}
    assert all(policy.risk[r] for r in policy.risk)  # every risk names a source
    shuffled = list(sources) * 2
    rnd.shuffle(shuffled)
    again = resolve_route_policy(_route(shuffled))
    assert again.risk == policy.risk  # order and repetition never matter
    assert again.step_up_max_age_s == policy.step_up_max_age_s


@LAWS
@given(
    st.lists(st.sampled_from(SOURCES), max_size=4),
    st.lists(st.sampled_from(SOURCES), max_size=4),
)
def test_obligations_are_a_homomorphism(a: list[Source], b: list[Source]) -> None:
    """The step-up of ``A ∪ B`` is the strictest of the two (``min``), the
    risks of ``A ∪ B`` the union of the two."""

    def meet(x: int | None, y: int | None) -> int | None:
        return y if x is None else x if y is None else min(x, y)

    pa, pb = resolve_route_policy(_route(a)), resolve_route_policy(_route(b))
    joined = resolve_route_policy(_route(a + b))
    assert set(joined.risk) == set(pa.risk) | set(pb.risk)
    assert joined.step_up_max_age_s == meet(pa.step_up_max_age_s, pb.step_up_max_age_s)
    steps = [s.step_up for s in a + b if s.step_up is not None]
    assert joined.step_up_max_age_s == (min(steps) if steps else None)
