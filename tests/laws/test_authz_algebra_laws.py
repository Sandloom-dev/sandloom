"""The authorization algebra is unchanged (spec §8.2, §8.3; G-AUTHZ-01/02).

Hypothesis builds random policy trees of ``And`` / ``Or`` / ``Not`` (the real
classes of ``sandloom.shared.authz``) over fixed-outcome atoms that count their
evaluations: allow (T), deny (F) or indeterminate (U), each with a tier. The
oracle is a model of today's documented rule, written independently of the
implementation:

* operands of a nested ``And`` (``Or``) collapse into their parent of the
  same kind, and a combinator's tier is its operands' maximum;
* ``And`` evaluates its operands cheapest tier first (declaration order within
  a tier) and stops at the first that does not allow, returning it;
* ``Or`` evaluates in the same order and stops at the first allow; if none
  allows, it is indeterminate when an operand was, else a deny;
* ``Not`` inverts a definite answer and keeps an indeterminate one.

Pinned: the value, the exact atoms evaluated and their order (so no extra
evaluation and the cheapest-tier short-circuit), fail-closed soundness (an
allow holds under every two-valued completion of the indeterminate atoms),
``Not(U) = U``, ``Or`` allowing on a genuine ``T`` beside an indeterminate
sibling, and ``evaluate`` agreeing with ``decide``.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sandloom.shared.authz import And, Not, Or, PolicyIndeterminate, Requirement
from sandloom.shared.authz.results import AccessDecision

# --------------------------------------------------------------------------- #
# Trees: a plain description, built into real requirements and into the model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Atom:
    ident: int
    outcome: str  # "T" | "F" | "U"
    tier: int
    risks: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Node:
    kind: str  # "and" | "or" | "not"
    parts: tuple[Tree, ...]


Tree = Atom | Node


class Fixed(Requirement):
    """An atom with a fixed outcome that logs every evaluation."""

    def __init__(self, atom: Atom, log: list[int]) -> None:
        self.atom = atom
        self.tier = atom.tier
        self._log = log

    async def evaluate(self, ctx: Any) -> bool:
        return await self._evaluate_via_decide(ctx)

    async def decide(self, ctx: Any) -> AccessDecision:
        self._log.append(self.atom.ident)
        if self.atom.outcome == "T":
            return AccessDecision.allow()
        if self.atom.outcome == "F":
            return AccessDecision.deny(code="fixed_deny")
        return AccessDecision.error(code="fixed_indeterminate")

    def risks(self) -> frozenset[str]:
        return self.atom.risks

    def describe(self) -> str:
        return f"{self.atom.outcome}{self.atom.ident}"


def build(tree: Tree, log: list[int]) -> Requirement:
    if isinstance(tree, Atom):
        return Fixed(tree, log)
    parts = [build(p, log) for p in tree.parts]
    if tree.kind == "and":
        return And(*parts)
    if tree.kind == "or":
        return Or(*parts)
    return Not(parts[0])


def _tier(tree: Tree) -> int:
    if isinstance(tree, Atom):
        return tree.tier
    return max(_tier(p) for p in tree.parts)


def _operands(tree: Node) -> list[Tree]:
    flat: list[Tree] = []
    for part in tree.parts:
        if isinstance(part, Node) and part.kind == tree.kind:
            flat.extend(_operands(part))
        else:
            flat.append(part)
    return flat


def model(tree: Tree, values: dict[int, str] | None = None) -> tuple[str, list[int]]:
    """``(value, atoms evaluated in order)`` under the documented rule.

    ``values`` overrides atom outcomes (a two-valued completion)."""
    if isinstance(tree, Atom):
        outcome = values.get(tree.ident, tree.outcome) if values else tree.outcome
        return outcome, [tree.ident]
    if tree.kind == "not":
        value, inner = model(tree.parts[0], values)
        return {"T": "F", "F": "T", "U": "U"}[value], inner
    ordered = sorted(_operands(tree), key=_tier)  # stable: declaration order
    seen: list[int] = []
    results: list[str] = []
    for part in ordered:
        value, evaluated = model(part, values)
        seen += evaluated
        if tree.kind == "and" and value != "T":
            return value, seen
        if tree.kind == "or" and value == "T":
            return "T", seen
        results.append(value)
    if tree.kind == "and":
        return "T", seen
    return ("U" if "U" in results else "F"), seen


def atoms(tree: Tree) -> Iterator[Atom]:
    if isinstance(tree, Atom):
        yield tree
    else:
        for part in tree.parts:
            yield from atoms(part)


def _value(decision: AccessDecision) -> str:
    if decision.indeterminate:
        assert not decision.allowed
        return "U"
    return "T" if decision.allowed else "F"


def decide(tree: Tree) -> tuple[str, list[int]]:
    log: list[int] = []
    decision = asyncio.run(build(tree, log).decide(None))  # type: ignore[arg-type]
    return _value(decision), log


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #

_ids = itertools.count()
RISKS = st.frozensets(st.sampled_from(["admin", "money", "security"]), max_size=2)


@st.composite
def atom(draw: st.DrawFn) -> Atom:
    return Atom(
        ident=next(_ids),
        outcome=draw(st.sampled_from("TFU")),
        tier=draw(st.integers(1, 3)),
        risks=draw(RISKS),
    )


def _extend(children: st.SearchStrategy[Tree]) -> st.SearchStrategy[Tree]:
    return st.one_of(
        st.builds(
            lambda ps: Node("and", tuple(ps)),
            st.lists(children, min_size=1, max_size=4),
        ),
        st.builds(
            lambda ps: Node("or", tuple(ps)), st.lists(children, min_size=1, max_size=4)
        ),
        st.builds(lambda p: Node("not", (p,)), children),
    )


trees: st.SearchStrategy[Tree] = st.recursive(atom(), _extend, max_leaves=10)

LAWS = settings(max_examples=400, deadline=None)


# --------------------------------------------------------------------------- #
# Laws
# --------------------------------------------------------------------------- #


@LAWS
@given(trees)
def test_value_and_evaluations_equal_the_documented_rule(tree: Tree) -> None:
    """Today's value, the atoms evaluated and their order (cheapest tier
    first, short-circuit, nothing evaluated twice or beyond the rule)."""
    value, evaluated = decide(tree)
    assert (value, evaluated) == model(tree)
    assert len(evaluated) == len(set(evaluated))


@LAWS
@given(trees, st.data())
def test_an_allow_holds_under_every_completion_of_the_indeterminate(
    tree: Tree, data: st.DataObject
) -> None:
    """Fail closed: an indeterminate never produces an allow that a definite
    answer in its place could turn into a deny (G-AUTHZ-01)."""
    value, _ = decide(tree)
    unknown = sorted(a.ident for a in atoms(tree) if a.outcome == "U")
    if value != "T" or not unknown:
        return
    for combo in itertools.islice(itertools.product("TF", repeat=len(unknown)), 64):
        completion = dict(zip(unknown, combo, strict=True))
        assert model(tree, completion)[0] == "T", completion


@LAWS
@given(trees)
def test_not_keeps_an_indeterminate(tree: Tree) -> None:
    """``Not(U) = U``: a negation never inverts what could not be evaluated."""
    value, _ = decide(tree)
    negated, _ = decide(Node("not", (tree,)))
    assert negated == {"T": "F", "F": "T", "U": "U"}[value]


@LAWS
@given(trees, st.integers(1, 3))
def test_or_allows_on_a_genuine_allow_beside_an_indeterminate(
    tree: Tree, tier: int
) -> None:
    """``Or`` allows when an operand genuinely allows, whatever its siblings
    (an indeterminate sibling included); it never allows otherwise."""
    allow = Atom(next(_ids), "T", tier)
    broken = Atom(next(_ids), "U", tier)
    assert decide(Node("or", (tree, broken, allow)))[0] == "T"
    alone, _ = decide(Node("or", (tree, broken)))
    assert alone in ("T", "U")
    assert alone == "T" or decide(tree)[0] != "T"


@LAWS
@given(trees)
def test_evaluate_agrees_with_decide(tree: Tree) -> None:
    value, _ = decide(tree)
    requirement = build(tree, [])
    if value == "U":
        with pytest.raises(PolicyIndeterminate):
            asyncio.run(requirement.evaluate(None))  # type: ignore[arg-type]
    else:
        assert asyncio.run(requirement.evaluate(None)) is (value == "T")  # type: ignore[arg-type]


def test_the_known_order_dependence_is_kept() -> None:
    """Spec §8.3 "known behaviour, kept": ``~(F & U)`` allows (the deny is
    seen first), ``~(U & F)`` is indeterminate."""
    f, u = Atom(next(_ids), "F", 1), Atom(next(_ids), "U", 1)
    assert decide(Node("not", (Node("and", (f, u)),)))[0] == "T"
    assert decide(Node("not", (Node("and", (u, f)),)))[0] == "U"


# --------------------------------------------------------------------------- #
# The status of an indeterminate (ADR-0014 decision 3): 401 > 503 > 501
# --------------------------------------------------------------------------- #
#: Indeterminate outcomes by HTTP status: 401 ``token_policy_outdated`` (one
#: refresh fixes it), 503 (a retry: ``claims_policy_mismatch``, a provider
#: down), 501 (an operator: a contract not deployed).
STATUSES = ("401", "503", "501")
_RANK = {"401": 0, "503": 1, "501": 2}


class Status(Fixed):
    """A fixed atom whose indeterminate carries an HTTP status."""

    async def decide(self, ctx: Any) -> AccessDecision:
        self._log.append(self.atom.ident)
        outcome = self.atom.outcome
        if outcome == "T":
            return AccessDecision.allow()
        if outcome == "F":
            return AccessDecision.deny(code="fixed_deny")
        return AccessDecision.error(code=f"fixed_{outcome}", http_status=int(outcome))


def build_status(tree: Tree, log: list[int]) -> Requirement:
    if isinstance(tree, Atom):
        return Status(tree, log)
    parts = [build_status(p, log) for p in tree.parts]
    if tree.kind == "and":
        return And(*parts)
    if tree.kind == "or":
        return Or(*parts)
    return Not(parts[0])


def status_model(tree: Tree) -> str:
    """The documented rule with statuses: ``And`` returns its first
    non-allowing operand (cheapest tier first); ``Or`` allows on a genuine
    allow, else returns its most fixable indeterminate (401, then 503, then
    501; the first of equal rank), else denies; ``Not`` inverts a definite
    answer and returns an indeterminate unchanged, status included."""
    if isinstance(tree, Atom):
        return tree.outcome
    if tree.kind == "not":
        inner = status_model(tree.parts[0])
        return {"T": "F", "F": "T"}.get(inner, inner)
    values = [status_model(p) for p in sorted(_operands(tree), key=_tier)]
    if tree.kind == "and":
        return next((v for v in values if v != "T"), "T")
    if "T" in values:
        return "T"
    unknown = [v for v in values if v in _RANK]
    return min(unknown, key=_RANK.__getitem__) if unknown else "F"


def decide_status(tree: Tree) -> str:
    decision = asyncio.run(build_status(tree, []).decide(None))  # type: ignore[arg-type]
    if decision.allowed:
        return "T"
    if decision.indeterminate:
        assert decision.http_status is not None
        return str(decision.http_status)
    return "F"


@st.composite
def status_atom(draw: st.DrawFn) -> Atom:
    return Atom(
        ident=next(_ids),
        outcome=draw(st.sampled_from(("T", "F", *STATUSES))),
        tier=draw(st.integers(1, 3)),
    )


status_trees: st.SearchStrategy[Tree] = st.recursive(
    status_atom(), _extend, max_leaves=10
)


@LAWS
@given(status_trees)
def test_the_status_equals_the_documented_rule(tree: Tree) -> None:
    """Value and status of every tree: 401 / 503 / 501 kept through ``And``
    and ``Not``, ordered 401 > 503 > 501 by ``Or``."""
    assert decide_status(tree) == status_model(tree)


@LAWS
@given(st.lists(st.sampled_from(("F", *STATUSES)), min_size=1, max_size=6), st.data())
def test_or_answers_the_most_fixable_indeterminate(
    outcomes: list[str], data: st.DataObject
) -> None:
    """With no operand allowing, ``Or`` answers 401 before 503 before 501,
    whatever their order or tiers: a client refreshes before it retries, and
    retries before an operator is needed."""
    parts = tuple(
        Atom(next(_ids), outcome, data.draw(st.integers(1, 3))) for outcome in outcomes
    )
    unknown = [o for o in outcomes if o in _RANK]
    expected = min(unknown, key=_RANK.__getitem__) if unknown else "F"
    assert decide_status(Node("or", parts)) == expected


@LAWS
@given(status_trees)
def test_not_keeps_the_status(tree: Tree) -> None:
    """``Not(U) = U`` with its status: ``~Role("admin")`` on an outdated
    token is a 401, never an allow."""
    value = decide_status(tree)
    negated = decide_status(Node("not", (tree,)))
    assert negated == {"T": "F", "F": "T"}.get(value, value)
    assert decide_status(Node("not", (Node("not", (tree,)),))) == value


@LAWS
@given(status_trees, st.sampled_from(STATUSES), st.integers(1, 3))
def test_an_allow_beside_any_indeterminate_allows_and_and_keeps_it(
    tree: Tree, status: str, tier: int
) -> None:
    """``Role("admin") | AlwaysAllow`` still allows on an outdated token;
    ``A & U`` never allows and answers ``U``'s status when ``A`` allows."""
    allow = Atom(next(_ids), "T", tier)
    broken = Atom(next(_ids), status, tier)
    assert decide_status(Node("or", (tree, broken, allow))) == "T"
    assert decide_status(Node("and", (allow, broken))) == status
    assert decide_status(Node("and", (tree, broken))) != "T"
