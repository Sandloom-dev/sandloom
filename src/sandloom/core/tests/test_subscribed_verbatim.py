"""Billing's generated ``Subscribed`` on its verbatim path (KB-33e).

``Subscribed(statuses=..., of=Account.user())`` (a :class:`ClaimFactCheck`
with explicit statuses, ``ClaimFactCheck._via_statuses``) reads the token's
``subscription_status`` verbatim, with zero I/O. Absence of a source is never
a negative fact (owner principle, 2026-11-09; the rows of G-AUTHZ-12 as
amended by KB-33d):

* a status present decides: among the statuses allows, any other denies 402;
* the claim removed by the view for a reason a refresh or a retry may change:
  its 401 ``token_policy_outdated`` / 503 ``claims_policy_mismatch``;
* absent because its authority (billing) failed at the mint: 503
  ``claim_authority_failed``;
* absent otherwise (billing ran and wrote none, the view removed it as
  unassigned / unvouched, a legacy token, an unassigned key): 501
  ``access_unknown``;
* a value that is not a string: 501 ``access_unknown``.

``Not``, ``Or`` and ``And`` never allow on an indeterminate, and no row asks
a contract, a fact over RPC or the database. The default path (no
``statuses=``) is unchanged: with billing failed at the mint it escalates to
billing's live answer (503 without one), never a deny.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI

import sandloom.shared.authz.facts as facts
from sandloom.core.app_factory.claim_authorities import build_claim_authorities
from sandloom.core.auth import claims_view as view
from sandloom.core.cli.generated import render_stub
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api._service_registry import ServiceUnavailableInDeployment
from sandloom.shared.authz import Account, Not, Or, Role
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.subject import UserContext
from sandloom_billing.access import SUBSCRIPTION_ACTIVE_FACT
from sandloom_billing.contracts import EntitlementsContract

pytestmark = pytest.mark.asyncio

KEY = "subscription_status"
MAP = {
    "issuer": "sandloom_identity",
    "roles": "sandloom_identity",
    "permissions": "sandloom_identity",
    KEY: "sandloom_billing",
}
UNASSIGNED_MAP = {
    "issuer": "sandloom_identity",
    "roles": "sandloom_identity",
    "permissions": "sandloom_identity",
}
ALLOW = (True, None)
DENY = (False, None)


def _generated(tmp_path: Path) -> ModuleType:
    """Billing's stub as ``sandloom generate`` writes it for a consumer."""
    path = tmp_path / "billing_entitlements_v1.py"
    path.write_text(render_stub("billing.entitlements.v1", EntitlementsContract))
    spec = importlib.util.spec_from_file_location("_kb33e_billing_stub", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gen(tmp_path: Path) -> Any:
    module = _generated(tmp_path)
    yield module
    sys.modules.pop(module.__name__, None)


class _NoIO:
    """A composition with billing's fact registered locally, whose contracts
    may not be asked: the verbatim path is zero I/O, so any call fails."""

    def __init__(self, mapping: dict[str, str] | None) -> None:
        self.core = CoreFacade(app=FastAPI())
        if mapping is not None:
            self.core._assign_claim_authorities(build_claim_authorities(mapping))
        self.asked: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.core, name)

    def get_access_fact(self, name: str) -> Any:
        # A registry lookup (the owner is loaded), not a call.
        return (
            SUBSCRIPTION_ACTIVE_FACT if name == SUBSCRIPTION_ACTIVE_FACT.name else None
        )

    def remote_fact_owners(self) -> list[str]:
        return []

    def get_service(self, name: str) -> Any:
        self.asked.append(name)
        raise AssertionError(f"the verbatim path asked the contract {name!r}")


class _NoDB:
    """A request session nobody may touch (zero SQL)."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the verbatim path used the database ({name})")


@pytest.fixture
def no_remote(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail on any fact lookup or resolution (describe over RPC, live)."""
    asked: list[str] = []

    async def _refuse(*args: Any, **kwargs: Any) -> Any:
        asked.append("fact")
        raise AssertionError("the verbatim path looked a fact up")

    monkeypatch.setattr(facts, "lookup_access_fact", _refuse)
    monkeypatch.setattr(facts, "resolve_fact", _refuse)
    return asked


def _subject(
    *,
    failed: set[str] | None = None,
    untrusted: str | None = None,
    status: Any = None,
) -> UserContext:
    """A caller with roles {member}; ``failed`` the sources that failed at the
    mint (``None``: a legacy token without provenance), ``status`` its
    ``subscription_status`` (``None``: absent)."""
    data: dict[str, Any] = {"user_id": uuid4(), "roles": {"member"}}
    if status is not None:
        data[KEY] = status
    if failed is not None:
        data["claims_sources"] = {"identity", "billing"} - failed
        data["claims_sources_failed"] = set(failed)
    if untrusted is not None:
        data[view.UNTRUSTED_KEY] = {KEY: untrusted}
    return UserContext(**data)


#: row -> (subject, claim authorities map, outcome over Subscribed(statuses=
#: {"active", "trialing"})). An outcome is ALLOW / DENY or (status, code).
TABLE: dict[str, tuple[Any, dict[str, str] | None, Any]] = {
    "present.allowed": (lambda: _subject(failed=set(), status="active"), MAP, ALLOW),
    "present.other_allowed": (
        lambda: _subject(failed=set(), status="trialing"),
        MAP,
        ALLOW,
    ),
    "present.denied": (lambda: _subject(failed=set(), status="canceled"), MAP, DENY),
    "present.declared_not_chosen": (
        lambda: _subject(failed=set(), status="past_due"),
        MAP,
        DENY,
    ),
    "present.none": (lambda: _subject(failed=set(), status="none"), MAP, DENY),
    "present.legacy": (lambda: _subject(status="active"), MAP, ALLOW),
    "authority_failed": (
        lambda: _subject(failed={"billing"}),
        MAP,
        (503, "claim_authority_failed"),
    ),
    "authority_failed.no_map": (
        lambda: _subject(failed={"billing"}),
        None,
        (503, "claim_authority_failed"),
    ),
    "authority_failed.view_unassigned": (
        lambda: _subject(failed={"billing"}, untrusted=view.UNASSIGNED),
        MAP,
        (503, "claim_authority_failed"),
    ),
    "claim_absent.authority_ran": (
        lambda: _subject(failed=set()),
        MAP,
        (501, "access_unknown"),
    ),
    "claim_absent.other_source_failed": (
        lambda: _subject(failed={"rogue"}),
        MAP,
        (501, "access_unknown"),
    ),
    "claim_absent.unassigned_key": (
        lambda: _subject(failed={"billing"}),
        UNASSIGNED_MAP,
        (501, "access_unknown"),
    ),
    "claim_absent.legacy": (lambda: _subject(), MAP, (501, "access_unknown")),
    "untrusted.outdated": (
        lambda: _subject(failed=set(), untrusted=view.OUTDATED),
        MAP,
        (401, view.OUTDATED),
    ),
    "untrusted.mismatch": (
        lambda: _subject(failed=set(), untrusted=view.MISMATCH),
        MAP,
        (503, view.MISMATCH),
    ),
    "untrusted.outdated.authority_failed": (
        lambda: _subject(failed={"billing"}, untrusted=view.OUTDATED),
        MAP,
        (401, view.OUTDATED),
    ),
    "untrusted.unassigned": (
        lambda: _subject(failed=set(), untrusted=view.UNASSIGNED),
        MAP,
        (501, "access_unknown"),
    ),
    "untrusted.source_absent": (
        lambda: _subject(failed=set(), untrusted=view.SOURCE_ABSENT),
        MAP,
        (501, "access_unknown"),
    ),
    "malformed.int": (
        lambda: _subject(failed=set(), status=1),
        MAP,
        (501, "access_unknown"),
    ),
    "malformed.list": (
        lambda: _subject(failed=set(), status=["active"]),
        MAP,
        (501, "access_unknown"),
    ),
    "malformed.bool": (
        lambda: _subject(failed=set(), status=True),
        MAP,
        (501, "access_unknown"),
    ),
}

_SWAP = {ALLOW: DENY, DENY: ALLOW}
#: How a wrap maps the atom's outcome: ``Not`` swaps a definite answer and
#: keeps an indeterminate; an allowing sibling of ``Or`` allows on every row.
WRAPS: dict[str, tuple[Any, Any]] = {
    "bare": (lambda a: a, lambda o: o),
    "not": (Not, lambda o: _SWAP.get(o, o)),
    "not_not": (lambda a: Not(Not(a)), lambda o: o),
    "or_denied": (lambda a: Or(Role("ops"), a), lambda o: o),
    "or_not_denied": (lambda a: Or(Role("ops"), Not(a)), lambda o: _SWAP.get(o, o)),
    "and_allowed": (lambda a: Role("member") & a, lambda o: o),
    "and_not": (lambda a: Role("member") & Not(a), lambda o: _SWAP.get(o, o)),
    "not_and": (lambda a: Not(Role("member") & a), lambda o: _SWAP.get(o, o)),
    "or_allowed": (lambda a: Or(Role("member"), a), lambda _o: ALLOW),
}


@pytest.mark.parametrize("wrap", sorted(WRAPS))
@pytest.mark.parametrize("row", sorted(TABLE))
async def test_the_verbatim_path_is_three_valued_from_the_token(
    gen: Any, no_remote: list[str], row: str, wrap: str
) -> None:
    """Every row, bare and under Not / Or / And: no indeterminate is ever an
    allow, and nothing is asked (no contract, no fact lookup, no SQL)."""
    subject, mapping, outcome = TABLE[row]
    build, expect = WRAPS[wrap]
    core = _NoIO(mapping)
    ctx = AuthzContext(subject(), core=core, db=_NoDB())  # type: ignore[arg-type]
    atom = gen.Subscribed(frozenset({"active", "trialing"}), of=Account.user())
    decision = await build(atom).decide(ctx)
    expected = expect(outcome)
    if expected == ALLOW:
        assert decision.allowed and not decision.indeterminate, decision
    elif expected == DENY:
        assert not decision.allowed and not decision.indeterminate, decision
        if wrap == "bare":
            assert (decision.http_status, decision.code) == (402, "Subscribed")
    else:
        status, code = expected
        assert not decision.allowed and decision.indeterminate, decision
        assert (decision.http_status, decision.code) == (status, code), decision
    assert core.asked == [] and no_remote == []


async def test_billing_failed_at_the_mint_never_lets_not_allow(
    gen: Any, no_remote: list[str]
) -> None:
    """The reported hole: ``~Subscribed(statuses=..., of=Account.user())``
    with billing failed at the mint is 503, not an allow."""
    ctx = AuthzContext(_subject(failed={"billing"}), core=_NoIO(MAP))
    policy = Not(gen.Subscribed(frozenset({"active"}), of=Account.user()))
    decision = await policy.decide(ctx)
    assert not decision.allowed
    assert (decision.http_status, decision.code) == (503, "claim_authority_failed")
    assert "failed when the token was minted" in decision.reason


async def test_an_unknown_names_the_claim(gen: Any, no_remote: list[str]) -> None:
    atom = gen.Subscribed(frozenset({"active"}), of=Account.user())
    absent = await atom.decide(AuthzContext(_subject(failed=set()), core=_NoIO(MAP)))
    assert "'subscription_status' is not in the token" in absent.reason
    assert absent.failed_requirement == "active subscription"
    assert absent.metadata == {"claim": KEY}
    malformed = await atom.decide(
        AuthzContext(_subject(failed=set(), status=3), core=_NoIO(MAP))
    )
    assert "is not a string" in malformed.reason


async def test_or_of_two_unknown_branches_never_allows(
    gen: Any, no_remote: list[str]
) -> None:
    ctx = AuthzContext(_subject(failed=set()), core=_NoIO(MAP))
    strict = gen.Subscribed(frozenset({"active"}), of=Account.user())
    trial = gen.Subscribed(frozenset({"trialing"}), of=Account.user())
    for policy in (Or(strict, trial), Or(Not(strict), Not(trial)), Not(strict & trial)):
        decision = await policy.decide(ctx)
        assert not decision.allowed and decision.indeterminate, policy.describe()
        assert (decision.http_status, decision.code) == (501, "access_unknown")


async def test_the_default_path_escalates_when_billing_failed(gen: Any) -> None:
    """Unchanged: without ``statuses=`` a failed billing is not trusted, so
    the atom asks billing live; with no provider that is 503, and ``Not``
    never allows."""

    class _NoProvider(_NoIO):
        def get_service(self, name: str) -> Any:
            self.asked.append(name)
            raise ServiceUnavailableInDeployment(name)

    core = _NoProvider(MAP)
    ctx = AuthzContext(_subject(failed={"billing"}), core=core)
    for policy in (
        gen.Subscribed(of=Account.user()),
        Not(gen.Subscribed(of=Account.user())),
    ):
        decision = await policy.decide(ctx)
        assert not decision.allowed and decision.indeterminate, decision
        assert decision.http_status == 503, decision
    assert "billing.entitlements.v1" in core.asked  # it went live, as before
