"""The algebra laws hold with generated access checks in the trees (Z-3,
Z-10; ADR-0014 decisions 9 and 12).

The trees of ``test_authz_algebra_laws`` again, with every tier-3 leaf a real
:class:`~sandloom.shared.authz.contract_check.ContractCheck` subclass built
from a literal (the shape ``sandloom generate`` writes), evaluated by the
kernel's one generic evaluator against a fake core. ``T`` / ``F`` are the
provider's boolean answer; ``U`` is one of the three indeterminates an
access check has: the provider absent (no local provider, no binding), a
provider whose signature no longer matches the literal (``contract_mismatch``,
the arguments refused) and a provider answering a non-boolean
(``contract_mismatch``). Each is 501 inside the algebra: ``Or`` still allows
on a genuine ``T`` beside it and ``Not`` never allows (the PoC's Z-10 failure,
a 422 raised out of the ``Or``, fixed). The oracle is the laws' own model.
"""

from __future__ import annotations

import asyncio
import itertools
from typing import Any
from uuid import uuid4

import pytest
import test_authz_algebra_laws as laws
from hypothesis import given
from hypothesis import strategies as st

from sandloom.core.di import ServiceNotFound
from sandloom.core.module_api._errors import ContractArgumentError
from sandloom.shared.authz import And, Not, Or, PolicyIndeterminate
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.contract_check import CONTRACT_MISMATCH, ContractCheck
from sandloom.shared.subject import UserContext

#: How an indeterminate leaf is produced, chosen by its id.
_U_KINDS = ("absent", "mismatch", "not_bool")


class _Provider:
    def __init__(self, outcome: str, ident: int, log: list[int]) -> None:
        self.outcome, self.ident, self.log = outcome, ident, log

    async def check(self, user_id: Any) -> Any:
        self.log.append(self.ident)
        if self.outcome == "R":
            raise RuntimeError("provider bug")
        if self.outcome == "mismatch":
            raise ContractArgumentError(
                status_code=422, detail="missing a required argument: 'scope'"
            )
        if self.outcome == "not_bool":
            return {"allowed": True}
        return self.outcome == "T"


class _Core:
    def __init__(self) -> None:
        self.providers: dict[str, Any] = {}
        self.absent: dict[str, tuple[int, list[int]]] = {}

    def get_service(self, name: str) -> Any:
        if name in self.absent:
            ident, log = self.absent[name]
            log.append(ident)  # the atom was reached: it is in the order
            raise ServiceNotFound(name)
        return self.providers[name]


def _atom_class(ident: int) -> type[ContractCheck]:
    literal = {
        "contract": f"law.c{ident}.v1",
        "method": "check",
        "args": ["user_id"],
        "binds": {"user_id": ["subject"]},
        "tier": 3,
        "category": "forbidden",
    }
    return type(f"Gen{ident}", (ContractCheck,), {"__sandloom_atom__": literal})


def build(
    tree: laws.Tree,
    log: list[int],
    core: _Core,
    outcome_of: dict[int, str] | None = None,
) -> Any:
    if isinstance(tree, laws.Atom):
        if tree.tier != 3:
            return laws.Fixed(tree, log)
        outcome = (outcome_of or {}).get(tree.ident, tree.outcome)
        name = f"law.c{tree.ident}.v1"
        if outcome == "U":
            outcome = _U_KINDS[tree.ident % len(_U_KINDS)]
        if outcome == "absent":
            core.absent[name] = (tree.ident, log)
        else:
            core.providers[name] = _Provider(outcome, tree.ident, log)
        return _atom_class(tree.ident)({})
    parts = [build(p, log, core, outcome_of) for p in tree.parts]
    if tree.kind == "and":
        return And(*parts)
    if tree.kind == "or":
        return Or(*parts)
    return Not(parts[0])


def decide(tree: laws.Tree) -> tuple[str, list[int]]:
    log: list[int] = []
    core = _Core()
    req = build(tree, log, core)
    ctx = AuthzContext(UserContext(user_id=uuid4()), core=core)
    return laws._value(asyncio.run(req.decide(ctx))), log


@laws.LAWS
@given(laws.trees)
def test_generated_atoms_keep_value_and_order(tree: laws.Tree) -> None:
    value, evaluated = decide(tree)
    assert (value, evaluated) == laws.model(tree)
    assert len(evaluated) == len(set(evaluated))


@laws.LAWS
@given(laws.trees)
def test_generated_atoms_not_keeps_an_indeterminate(tree: laws.Tree) -> None:
    value, _ = decide(tree)
    negated, _ = decide(laws.Node("not", (tree,)))
    assert negated == {"T": "F", "F": "T", "U": "U"}[value]


@laws.LAWS
@given(laws.trees, st.data())
def test_generated_atoms_fail_closed(tree: laws.Tree, data: st.DataObject) -> None:
    value, _ = decide(tree)
    unknown = sorted(a.ident for a in laws.atoms(tree) if a.outcome == "U")
    if value != "T" or not unknown:
        return
    for combo in itertools.islice(itertools.product("TF", repeat=len(unknown)), 64):
        assert laws.model(tree, dict(zip(unknown, combo, strict=True)))[0] == "T"


@laws.LAWS
@given(laws.trees)
def test_a_raising_generated_atom_never_allows(tree: laws.Tree) -> None:
    """A provider that raises (a bug, not a mismatch) propagates out of the
    policy when it is reached: never an allow, never folded into a deny."""
    raising = laws.Atom(next(laws._ids), "T", 3)
    policy_tree = laws.Node("and", (tree, raising))
    log: list[int] = []
    core = _Core()
    req = build(policy_tree, log, core, {raising.ident: "R"})
    ctx = AuthzContext(UserContext(user_id=uuid4()), core=core)
    value, evaluated = laws.model(policy_tree)
    if raising.ident in evaluated:
        with pytest.raises(RuntimeError):
            asyncio.run(req.decide(ctx))
    else:
        assert laws._value(asyncio.run(req.decide(ctx))) == value


@pytest.mark.parametrize("kind", _U_KINDS)
def test_each_indeterminate_is_501_even_negated(kind: str) -> None:
    atom = laws.Atom(next(laws._ids), "T", 3)
    core = _Core()
    req = Not(build(atom, [], core, {atom.ident: kind}))
    ctx = AuthzContext(UserContext(user_id=uuid4()), core=core)
    decision = asyncio.run(req.decide(ctx))
    assert decision.indeterminate and decision.http_status == 501
    expected = "service_unavailable" if kind == "absent" else CONTRACT_MISMATCH
    assert decision.code == expected
    with pytest.raises(PolicyIndeterminate):
        asyncio.run(req.evaluate(ctx))


@pytest.mark.parametrize("kind", ["mismatch", "not_bool"])
def test_or_allows_on_a_genuine_allow_beside_a_mismatch(kind: str) -> None:
    """Z-10: a mismatching branch never escapes the ``Or`` as an error."""
    broken = laws.Atom(next(laws._ids), "T", 3)
    allows = laws.Atom(next(laws._ids), "T", 3)
    log: list[int] = []
    core = _Core()
    req = Or(
        build(broken, log, core, {broken.ident: kind}),
        build(allows, log, core),
    )
    ctx = AuthzContext(UserContext(user_id=uuid4()), core=core)
    assert asyncio.run(req.decide(ctx)).allowed
    assert log == [broken.ident, allows.ident]
