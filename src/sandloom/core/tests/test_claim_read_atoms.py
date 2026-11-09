"""Claim atoms over a mapping claim (``claim_atom``; ADR-0014 decision 15; KB-33).

``Feature`` and ``WithinLimit`` move out of the kernel as generated atoms of
their owner's access checks. A third-party provider (``planco.plans.v1``, no
battery involved) declares both exactly as billing does; the atoms
``sandloom generate``'s renderer writes beside the stub are compared with the
kernel atoms they replace. Pinned here:

* generation: the declaration is in the describe document, the atoms are
  rendered in their final layout with the kernel atoms' signatures
  (``Feature(key, *, of, claim_key)``, ``WithinLimit(key, used, *, of,
  claim_key)``), a malformed declaration is refused;
* ``Account.user()``: decisions equal to the kernel atoms' on every trust case
  (present, absent, an authority that failed at the mint 503, the view's 401 /
  503, unassigned, legacy token), under ``Not`` and ``Or`` too, with zero
  calls and no contract named;
* an organization account: the provider's live check asked once with the
  atom's parameters, decisions equal to the kernel atoms on a test producer
  (the owner's organization fact asking the same provider), the same calls;
  no provider is 501 and ``Not`` never allows;
* the compatibility rule: changing a claim atom's read is incompatible.
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

import sandloom.shared.authz as kernel
from sandloom.core.app_factory.claim_authorities import build_claim_authorities
from sandloom.core.auth import claims_view as view
from sandloom.core.cli import dto_copies as _dto_copies
from sandloom.core.cli.generated import render_stub
from sandloom.core.contracts.compatibility import compare_contract
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_schema
from sandloom.core.module_api._service_registry import ServiceUnavailableInDeployment
from sandloom.core.rpc.errors import RemoteServiceUnavailable
from sandloom.sdk import contract
from sandloom.shared.authz import (
    AccessFact,
    Account,
    Category,
    From,
    Not,
    Or,
    Role,
    access_check,
    claim_atom,
)
from sandloom.shared.authz.algebra import TIER_CLAIM, TIER_LIVE
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

PLANS_CONTRACT = "planco.plans.v1"
IDP = "acme.idp"
PLANS = "acme.plans"
MAP = {
    "issuer": IDP,
    "roles": IDP,
    "permissions": IDP,
    "features": PLANS,
    "limits": PLANS,
}
UNASSIGNED_MAP = {"issuer": IDP, "roles": IDP, "permissions": IDP}


@contract(PLANS_CONTRACT)
class PlansContract:
    """Plan entitlements of an account (a third-party provider)."""

    @claim_atom("Feature", claim_key="features", entry="key")
    @access_check(
        "LiveFeature",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        describe="feature '{key}'",
    )
    async def has_feature(self, tenant_type: str, tenant_id: UUID, key: str) -> bool:
        """True when the plan enables ``key``."""
        return False

    @claim_atom("WithinLimit", claim_key="limits", entry="key", below="used")
    @access_check(
        "LiveWithinLimit",
        bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        category=Category.QUOTA_EXCEEDED,
        describe="within limit '{key}'",
    )
    async def is_within_limit(
        self, tenant_type: str, tenant_id: UUID, key: str, used: int
    ) -> bool:
        """True when ``used`` is below the plan limit ``key``."""
        return False


def _generated(tmp_path: Path) -> ModuleType:
    path = tmp_path / "planco_plans_v1.py"
    path.write_text(render_stub(PLANS_CONTRACT, PlansContract))
    spec = importlib.util.spec_from_file_location("_kb33_planco_plans_v1", path)
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


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
async def test_the_declaration_is_part_of_the_describe_document() -> None:
    methods = contract_schema(PlansContract).describe()["methods"]
    feature = methods["has_feature"]["access_check"]["claim"]
    assert feature == {
        "name": "Feature",
        "read": "flag",
        "claim_key": "features",
        "claim_keys": ["features"],
        "entry": "key",
        "category": "forbidden",
        "describe": "feature '{key}'",
    }
    limit = methods["is_within_limit"]["access_check"]["claim"]
    assert limit["read"] == "limit" and limit["below"] == "used"
    assert limit["category"] == Category.QUOTA_EXCEEDED


async def test_the_atoms_are_generated_in_their_final_layout(tmp_path: Path) -> None:
    first = render_stub(PLANS_CONTRACT, PlansContract)
    assert first == render_stub(PLANS_CONTRACT, PlansContract)
    assert "class Feature(ClaimReadCheck):" in first
    assert "class WithinLimit(ClaimReadCheck):" in first
    path = tmp_path / "planco_plans_v1.py"
    assert _dto_copies.ruff_formatted(first, path, cwd=Path.cwd()) == first


async def test_the_signatures_are_the_kernel_atoms(gen: Any) -> None:
    for name in ("Feature", "WithinLimit"):
        mine = inspect.signature(getattr(gen, name).__init__).parameters
        theirs = inspect.signature(getattr(kernel, name).__init__).parameters
        assert list(mine) == list(theirs)
        for pname, param in mine.items():
            assert param.kind is theirs[pname].kind, (name, pname)


async def test_a_malformed_declaration_is_refused() -> None:
    with pytest.raises(ValueError, match="above @access_check"):

        @claim_atom("Feature", claim_key="features", entry="key")
        async def bare(key: str) -> bool:  # pragma: no cover - refused
            return False

    with pytest.raises(ValueError, match="class name"):
        claim_atom("feature", claim_key="features", entry="key")

    with pytest.raises(ValueError, match="must bind"):

        @claim_atom("Feature", claim_key="features", entry="key")
        @access_check("LiveFeature", bind={"user_id": From.subject_id()})
        async def by_subject(user_id: UUID, key: str) -> bool:  # pragma: no cover
            return False

    @contract("planco.broken.v1")
    class Broken:
        @claim_atom("Feature", claim_key="features", entry="name")
        @access_check(
            "LiveFeature",
            bind={"tenant_type": From.account_type(), "tenant_id": From.account_id()},
        )
        async def has_feature(
            self, tenant_type: str, tenant_id: UUID, key: str
        ) -> bool:
            return False

    with pytest.raises(Exception, match="claim atom entry names 'name'"):
        contract_schema(Broken)


# --------------------------------------------------------------------------- #
# Account.user(): the claims, zero I/O, the kernel atoms' decisions
# --------------------------------------------------------------------------- #
class _Provider:
    """A recording provider of planco.plans.v1 (the test producer): what
    was ASKED, in order, answered from a plan table."""

    def __init__(self, plans: dict[Any, dict[str, Any]] | None = None) -> None:
        self.plans = plans or {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def has_feature(self, tenant_type: str, tenant_id: UUID, key: str) -> bool:
        self.calls.append(("has_feature", (tenant_type, tenant_id, key)))
        plan = self.plans.get(tenant_id, {})
        return plan.get("features", {}).get(key) is True

    async def is_within_limit(
        self, tenant_type: str, tenant_id: UUID, key: str, used: int
    ) -> bool:
        self.calls.append(("is_within_limit", (tenant_type, tenant_id, key, used)))
        limit = self.plans.get(tenant_id, {}).get("limits", {}).get(key)
        return isinstance(limit, int) and used < limit


def _core(mapping: dict[str, str] | None = MAP) -> CoreFacade:
    core = CoreFacade(app=FastAPI())
    if mapping is not None:
        core._assign_claim_authorities(build_claim_authorities(mapping))
    return core


CLAIMS: dict[str, Any] = {
    "features": {"export": True, "beta": False, "truthy": 1},
    "limits": {"seats": 3, "text": "3", "flag": True, "zero": 0},
}


def _subject(
    *, failed: frozenset[str] | set[str] | None = frozenset(), **claims: Any
) -> UserContext:
    data: dict[str, Any] = {"user_id": uuid4(), "roles": {"member"}, **claims}
    if failed is not None:
        data["claims_sources"] = {"idp", "plans"} - set(failed)
        data["claims_sources_failed"] = set(failed)
    return UserContext(**data)


def _viewed(key: str, reason: str) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        claims_sources={"idp", "plans"},
        claims_sources_failed=set(),
        **{view.UNTRUSTED_KEY: {key: reason}},
    )


SUBJECTS: dict[str, tuple[Any, dict[str, str] | None]] = {
    "present": (lambda: _subject(**CLAIMS), MAP),
    "present.rogue_failed": (lambda: _subject(failed={"rogue"}, **CLAIMS), MAP),
    "present.authority_failed": (lambda: _subject(failed={"plans"}, **CLAIMS), MAP),
    "empty": (lambda: _subject(features={}, limits={}), MAP),
    "absent.ran": (lambda: _subject(), MAP),
    "absent.failed": (lambda: _subject(failed={"plans"}), MAP),
    "absent.failed.unassigned": (lambda: _subject(failed={"plans"}), UNASSIGNED_MAP),
    "absent.failed.no_map": (lambda: _subject(failed={"plans"}), None),
    "absent.legacy": (lambda: _subject(failed=None), MAP),
    "present.legacy": (lambda: _subject(failed=None, **CLAIMS), MAP),
    "malformed": (lambda: _subject(features=["export"], limits=3), MAP),
    **{
        f"view.{key}.{reason}": (lambda k=key, r=reason: _viewed(k, r), MAP)
        for key in ("features", "limits")
        for reason in (
            view.OUTDATED,
            view.MISMATCH,
            view.UNASSIGNED,
            view.SOURCE_ABSENT,
        )
    },
}

USER_ATOMS: dict[str, Any] = {
    "feature.on": lambda k: k.Feature("export", of=Account.user()),
    "feature.off": lambda k: k.Feature("beta", of=Account.user()),
    "feature.truthy": lambda k: k.Feature("truthy", of=Account.user()),
    "feature.missing": lambda k: k.Feature("nope", of=Account.user()),
    "limit.below": lambda k: k.WithinLimit("seats", 2, of=Account.user()),
    "limit.equal": lambda k: k.WithinLimit("seats", 3, of=Account.user()),
    "limit.text": lambda k: k.WithinLimit("text", 0, of=Account.user()),
    "limit.bool": lambda k: k.WithinLimit("flag", 0, of=Account.user()),
    "limit.zero": lambda k: k.WithinLimit("zero", -1, of=Account.user()),
    "limit.missing": lambda k: k.WithinLimit("nope", 0, of=Account.user()),
}
WRAPS: dict[str, Any] = {
    "bare": lambda a: a,
    "not": Not,
    "or_denied": lambda a: Or(Role("ops"), a),
    "or_allowed": lambda a: Or(Role("member"), a),
}


def _key(decision: Any) -> tuple[Any, ...]:
    return (
        decision.allowed,
        decision.http_status,
        decision.category,
        decision.code,
        decision.indeterminate,
        decision.failed_requirement,
    )


@pytest.mark.parametrize("wrap", sorted(WRAPS))
@pytest.mark.parametrize("atom", sorted(USER_ATOMS))
async def test_the_user_account_decides_as_the_kernel_atom(
    gen: Any, atom: str, wrap: str
) -> None:
    provider = _Provider()
    for name, (subject, mapping) in SUBJECTS.items():
        core = _core(mapping)
        core.get_service = lambda _n: provider  # type: ignore[method-assign,assignment]
        ctx = AuthzContext(subject(), core=core)
        base = await WRAPS[wrap](USER_ATOMS[atom](kernel)).decide(ctx)
        candidate = await WRAPS[wrap](USER_ATOMS[atom](gen)).decide(ctx)
        assert _key(candidate) == _key(base), name
    assert provider.calls == []


async def test_the_trust_cases_keep_their_outcomes(gen: Any) -> None:
    """The equality above is not vacuous: the claim atom allows, denies and
    answers each indeterminate."""
    seen = {}
    for name in ("present", "absent.ran", "absent.failed", "absent.failed.no_map"):
        subject, mapping = SUBJECTS[name]
        ctx = AuthzContext(subject(), core=_core(mapping))
        decision = await gen.Feature("export", of=Account.user()).decide(ctx)
        seen[name] = (decision.allowed, decision.http_status, decision.code)
    assert seen == {
        "present": (True, 200, "ok"),
        "absent.ran": (False, 403, "Feature"),
        "absent.failed": (False, 503, "claim_authority_failed"),
        "absent.failed.no_map": (False, 503, "claim_authority_failed"),
    }
    outdated = await gen.WithinLimit("seats", 1, of=Account.user()).decide(
        AuthzContext(_viewed("limits", view.OUTDATED), core=_core())
    )
    assert outdated.indeterminate and outdated.http_status == 401


async def test_the_user_account_is_claim_tier_and_names_no_contract(gen: Any) -> None:
    for atom in (
        gen.Feature("export", of=Account.user()),
        gen.WithinLimit("seats", 1, of=Account.user()),
    ):
        assert atom.tier == TIER_CLAIM
        assert atom.contracts() == frozenset()
    org = gen.Feature("export", of=Account.org())
    assert org.tier == TIER_LIVE
    assert org.contracts() == frozenset({PLANS_CONTRACT})
    assert org.org_params() == frozenset({"org_id"})
    assert gen.WithinLimit("seats", 1, of=Account.org()).category == (
        Category.QUOTA_EXCEEDED
    )
    with pytest.raises(ValueError, match="claim_key must be one of"):
        gen.Feature("export", of=Account.user(), claim_key="roles")


# --------------------------------------------------------------------------- #
# An organization account: the live check with the parameters
# --------------------------------------------------------------------------- #
class _Core:
    """A core serving the provider and, for the kernel atoms, the owner's
    organization facts (the test producer asking the same provider)."""

    def __init__(self, provider: Any | None, *, facts: bool = True) -> None:
        self.provider = provider
        self.facts: dict[str, Any] = {}
        if facts and provider is not None:
            self.facts = {
                "organization.features": AccessFact(
                    name="organization.features",
                    owner_module="organization",
                    live_service=PLANS_CONTRACT,
                    live_resolver=_kernel_feature,
                    min_freshness="live",
                    default_mode="live",
                ),
                "organization.limits": AccessFact(
                    name="organization.limits",
                    owner_module="organization",
                    live_service=PLANS_CONTRACT,
                    live_resolver=_kernel_limit,
                    min_freshness="live",
                    default_mode="live",
                ),
            }

    def get_service(self, name: str) -> Any:
        if name != PLANS_CONTRACT or self.provider is None:
            raise ServiceUnavailableInDeployment(name)
        return self.provider

    def get_access_fact(self, name: str) -> Any:
        return self.facts.get(name)

    def remote_fact_owners(self) -> list[str]:
        return []

    def get_metric(self, name: str) -> Any:
        return None


async def _kernel_feature(service: Any, ctx: AuthzContext) -> bool:
    res = await ctx.get_resource()
    return bool(
        await service.has_feature("organization", res["organization_id"], res["key"])
    )


async def _kernel_limit(service: Any, ctx: AuthzContext) -> bool:
    res = await ctx.get_resource()
    return bool(
        await service.is_within_limit(
            "organization", res["organization_id"], res["key"], res["used"]
        )
    )


class _Req:
    def __init__(self, org: UUID) -> None:
        self.path_params = {"org_id": str(org)}
        self.query_params: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.scope: dict[str, Any] = {"state": {}}
        self.state = SimpleNamespace()


def _req(org: UUID) -> Any:
    """A request carrying ``org_id`` (duck-typed, as the atoms read it)."""
    return _Req(org)


ORG_ATOMS: dict[str, Any] = {
    "feature.on": lambda k: k.Feature("export", of=Account.org()),
    "feature.off": lambda k: k.Feature("beta", of=Account.org()),
    "limit.below": lambda k: k.WithinLimit("seats", 2, of=Account.org()),
    "limit.equal": lambda k: k.WithinLimit("seats", 3, of=Account.org()),
    "not.feature.off": lambda k: Not(k.Feature("beta", of=Account.org())),
}


@pytest.mark.parametrize("atom", sorted(ORG_ATOMS))
async def test_an_organization_asks_the_live_check_with_its_parameters(
    gen: Any, atom: str
) -> None:
    org = uuid4()
    plans = {org: {"features": {"export": True}, "limits": {"seats": 3}}}
    outcomes = []
    for kit in (kernel, gen):
        provider = _Provider(plans)
        subject = UserContext(user_id=uuid4(), tenant_ids=[str(org)])
        ctx = AuthzContext(subject, request=_req(org), core=_Core(provider))
        decision = await ORG_ATOMS[atom](kit).decide(ctx)
        outcomes.append((_key(decision), provider.calls))
    assert outcomes[1] == outcomes[0]
    _decision, calls = outcomes[1]
    assert len(calls) == 1 and calls[0][1][:2] == ("organization", org)
    assert calls[0][1][2] in {"export", "beta", "seats"}


async def test_the_parameters_reach_the_provider_unchanged(gen: Any) -> None:
    org = uuid4()
    provider = _Provider({org: {"limits": {"seats": 10}}})
    subject = UserContext(user_id=uuid4(), tenant_ids=[str(org)])
    ctx = AuthzContext(subject, request=_req(org), core=_Core(provider))
    assert (await gen.WithinLimit("seats", 9, of=Account.org()).decide(ctx)).allowed
    denied = await gen.WithinLimit("seats", 10, of=Account.org()).decide(ctx)
    assert denied.category == Category.QUOTA_EXCEEDED and denied.http_status == 402
    assert denied.code == "WithinLimit"
    assert provider.calls == [
        ("is_within_limit", ("organization", org, "seats", 9)),
        ("is_within_limit", ("organization", org, "seats", 10)),
    ]


async def test_a_personal_bound_tenant_reads_the_claim(gen: Any) -> None:
    from sandloom.core.di import bind_tenant

    provider = _Provider()
    subject = _subject(**CLAIMS)
    ctx = AuthzContext(subject, request=_req(uuid4()), core=_Core(provider))
    with bind_tenant(subject.user_id):
        atom = gen.Feature("export", of=Account.bound_tenant())
        assert (await atom.decide(ctx)).allowed
    assert provider.calls == []


async def test_without_a_provider_the_organization_path_is_501(gen: Any) -> None:
    org = uuid4()
    subject = UserContext(user_id=uuid4(), tenant_ids=[str(org)])
    ctx = AuthzContext(subject, request=_req(org), core=_Core(None))
    for atom in (
        gen.Feature("export", of=Account.org()),
        gen.WithinLimit("seats", 0, of=Account.org()),
    ):
        for policy in (atom, Not(atom)):
            decision = await policy.decide(ctx)
            assert not decision.allowed
            assert decision.indeterminate and decision.http_status == 501
            assert decision.code == "service_unavailable"


class _DownProvider:
    """A bound provider that is down (K5): every call fails over RPC."""

    def __init__(self) -> None:
        self.calls = 0

    async def has_feature(self, *_args: Any) -> bool:
        self.calls += 1
        raise RemoteServiceUnavailable(
            PLANS_CONTRACT, reason="unreachable", code="remote_service_unavailable"
        )


async def test_a_provider_down_is_503_and_never_allows(gen: Any) -> None:
    """The organization path inherits K5: indeterminate 503, ``Not`` never
    allows, ``Or`` still allows on a genuine allow; the claims path never
    asks the provider at all."""
    org = uuid4()
    down = _DownProvider()
    subject = _subject(tenant_ids=[str(org)], **CLAIMS)
    ctx = AuthzContext(subject, request=_req(org), core=_Core(down, facts=False))
    atom = gen.Feature("export", of=Account.org())
    for policy in (atom, Not(atom)):
        decision = await policy.decide(ctx)
        assert not decision.allowed
        assert decision.indeterminate and decision.http_status == 503
    assert (
        await Or(atom, gen.Feature("export", of=Account.user())).decide(ctx)
    ).allowed
    assert down.calls == 2  # the Or decided on its cheaper, claims branch


async def test_no_request_session_is_opened(gen: Any) -> None:
    """K6: the claim is in the token and the live check runs in the
    provider's unit, so ``require()`` opens no session for either."""
    for atom in (
        gen.Feature("export", of=Account.user()),
        gen.WithinLimit("seats", 1, of=Account.org()),
        gen.Feature("export", of=Account.bound_tenant()),
    ):
        for policy in (atom, Not(atom), atom & Role("admin")):
            assert not policy.needs_request_session(), policy.describe()


# --------------------------------------------------------------------------- #
# Compatibility (SANDLOOM-T004)
# --------------------------------------------------------------------------- #
async def test_changing_a_claim_read_is_incompatible() -> None:
    before = contract_schema(PlansContract).describe()
    for field, value in (("read", "flag"), ("entry", "used"), ("below", None)):
        after = copy.deepcopy(before)
        after.pop("hash", None)  # the provider's new document
        claim = after["methods"]["is_within_limit"]["access_check"]["claim"]
        claim[field] = value
        report = compare_contract(PLANS_CONTRACT, before, after)
        assert any(f"claim atom {field} changed" in e for e in report.errors), field
    assert not compare_contract(PLANS_CONTRACT, before, before).errors
