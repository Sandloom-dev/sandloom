"""Z-3: the algebra laws of ``tests/laws/test_authz_algebra_laws.py`` with
generated contract atoms in the trees (PoC kb-poc-authz).

Every tier-3 leaf becomes a real ``ContractCheck`` subclass built from a
literal (what ``sandloom generate`` writes), evaluated by the kernel's generic
evaluator against a fake core: ``T``/``F`` come from the provider's boolean
answer, ``U`` from an absent owner (no local provider, no binding: 501).
Tier-1/2 leaves stay the laws' fixed atoms. The oracle is the laws' own model.

Run from the candidate worktree:
    .venv/bin/python -m pytest -q -p no:cacheprovider <this file>
"""

from __future__ import annotations

import asyncio
import itertools
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

sys.path.insert(0, str(Path.cwd() / "tests" / "laws"))
import test_authz_algebra_laws as laws  # noqa: E402

from sandloom.core.di import ServiceNotFound  # noqa: E402
from sandloom.shared.authz import And, Not, Or, PolicyIndeterminate  # noqa: E402
from sandloom.shared.authz.contract_check import ContractCheck  # noqa: E402
from sandloom.shared.authz.context import AuthzContext  # noqa: E402
from sandloom.shared.subject import UserContext  # noqa: E402


class _Provider:
    def __init__(self, outcome: str, ident: int, log: list[int]) -> None:
        self.outcome, self.ident, self.log = outcome, ident, log

    async def check(self, user_id: Any) -> bool:
        self.log.append(self.ident)
        if self.outcome == "R":
            raise RuntimeError("provider bug")
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


def build(tree: laws.Tree, log: list[int], core: _Core, outcome_of=None) -> Any:
    if isinstance(tree, laws.Atom):
        if tree.tier != 3:
            return laws.Fixed(tree, log)
        outcome = (outcome_of or {}).get(tree.ident, tree.outcome)
        name = f"law.c{tree.ident}.v1"
        if outcome == "U":
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
    """A provider that raises (not an outage, a bug) propagates out of the
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


def test_absent_owner_is_indeterminate_501_even_negated() -> None:
    a = laws.Atom(next(laws._ids), "U", 3)
    core = _Core()
    req = Not(build(a, [], core))
    ctx = AuthzContext(UserContext(user_id=uuid4()), core=core)
    decision = asyncio.run(req.decide(ctx))
    assert decision.indeterminate and decision.http_status == 501
    with pytest.raises(PolicyIndeterminate):
        asyncio.run(req.evaluate(ctx))
