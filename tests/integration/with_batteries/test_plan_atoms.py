"""``Feature`` / ``WithinLimit`` as billing's generated atoms, mono and split (KB-33).

Billing declares both as claim atoms of its access checks
(``billing.entitlements.v1.has_feature`` / ``is_within_limit``); the atoms
``sandloom generate`` writes beside a consumer's stub are decided against the
kernel atoms they replace, on the real billing provider over the real test
database, in-process (mono) and over the real internal RPC (split: a consumer
core with no module and no database binds the contracts to the standard app).

Billing as shipped keeps no features or limits catalogue: its checks raise
``AccessUnknown``, so for an organization account every policy over them,
``Not`` and ``Or`` included, is indeterminate 501 ``access_unknown``, never an
allow, mono and split (KB-33c, C-B5); ``Account.user()`` still reads the
claims with zero I/O.

Otherwise the producer is a test one: the plan reads behind billing's
methods answer from a table here (an explicit off is a deny), and the
kernel atoms' organization facts (``organization.features`` /
``organization.limits``) ask the same billing methods. For each case, steady
state:

* the decision is equal (allowed, status, category, code, indeterminate,
  label) and the provider calls and their order are equal;
* the parameters reach the provider unchanged, over RPC included (the plan
  read records what billing was ASKED);
* the claims tier is 0 calls / 0 database round trips / 0 RPC; an
  organization account is one call to billing (two with a bound tenant: the
  tenant's kind first), the same count as the kernel atom.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, insert
from sqlalchemy.engine import Engine

import sandloom.shared.authz as kernel
from sandloom.core import rpc
from sandloom.core.claims import ClaimAuthorities
from sandloom.core.cli.generated import render_stub
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import rpc_settings
from sandloom.core.di import bind_tenant
from sandloom.core.facade import CoreFacade
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.testing.fixtures import *  # noqa: F403 - db_engine, real_uow_client
from sandloom.shared.authz import AccessFact, Account, Category, Not, Or
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.subject import UserContext
from sandloom_billing import entitlements as billing_entitlements
from sandloom_billing.contracts import EntitlementsContract
from sandloom_tenancy.models import Organization, OrganizationMember

pytestmark = pytest.mark.asyncio

USER = UUID("00000000-0000-4000-8000-00000000a101")
ORG = UUID("00000000-0000-4000-8000-00000000b101")
ENTITLEMENTS = "billing.entitlements.v1"
CONTRACTS = ("tenancy.membership.v1", ENTITLEMENTS)
PROVIDER, CONSUMER = "provider", "api"

#: The test producer's plans, by account.
PLANS: dict[UUID, dict[str, dict[str, Any]]] = {
    ORG: {"features": {"export": True}, "limits": {"seats": 3}},
}
#: The personal account's plan, as the token carries it.
CLAIMS: dict[str, Any] = {"features": {"export": True}, "limits": {"seats": 3}}


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """``billing.entitlements.v1``'s stub as ``sandloom generate`` writes it."""
    path = tmp_path_factory.mktemp("stub") / "billing_entitlements_v1.py"
    path.write_text(render_stub(ENTITLEMENTS, EntitlementsContract))
    spec = importlib.util.spec_from_file_location("_kb33_billing_stub", path)
    assert spec is not None and spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield SimpleNamespace(Feature=module.Feature, WithinLimit=module.WithinLimit)
    sys.modules.pop(spec.name, None)


KIT_KERNEL = SimpleNamespace(Feature=kernel.Feature, WithinLimit=kernel.WithinLimit)


@dataclass(frozen=True)
class Case:
    policy: Callable[[Any], Any]
    path: dict[str, str] = field(default_factory=dict)
    bound: UUID | None = None


_ORG = {"org_id": str(ORG)}
CASES: dict[str, Case] = {
    "user.feature.on": Case(lambda k: k.Feature("export", of=Account.user())),
    "user.feature.off": Case(lambda k: k.Feature("beta", of=Account.user())),
    "user.limit.below": Case(lambda k: k.WithinLimit("seats", 2, of=Account.user())),
    "user.limit.equal": Case(lambda k: k.WithinLimit("seats", 3, of=Account.user())),
    "org.feature.on": Case(lambda k: k.Feature("export", of=Account.org()), _ORG),
    "org.feature.off": Case(lambda k: k.Feature("beta", of=Account.org()), _ORG),
    "org.not_feature.off": Case(
        lambda k: Not(k.Feature("beta", of=Account.org())), _ORG
    ),
    "org.limit.below": Case(
        lambda k: k.WithinLimit("seats", 2, of=Account.org()), _ORG
    ),
    "org.limit.equal": Case(
        lambda k: k.WithinLimit("seats", 3, of=Account.org()), _ORG
    ),
    "bound.feature.on": Case(
        lambda k: k.Feature("export", of=Account.bound_tenant()), bound=ORG
    ),
    "bound.limit.equal": Case(
        lambda k: k.WithinLimit("seats", 3, of=Account.bound_tenant()), bound=ORG
    ),
}

#: Steady state per request: provider calls (a claims-tier case: none).
EXPECTED_CALLS: dict[str, int] = {
    name: 0 if name.startswith("user.") else 2 if name.startswith("bound.") else 1
    for name in CASES
}
#: Steady state per request: the providers' database round trips (billing's
#: plan reads touch none; the bound tenant's kind is tenancy's 3).
EXPECTED_SQL: dict[str, int] = {
    name: 3 if name.startswith("bound.") else 0 for name in CASES
}
#: What billing was asked (the parameters as received), per case.
ASKED: dict[str, list[tuple[Any, ...]]] = {
    "org.feature.on": [("plan_feature", "organization", ORG, "export")],
    "org.feature.off": [("plan_feature", "organization", ORG, "beta")],
    "org.not_feature.off": [("plan_feature", "organization", ORG, "beta")],
    "org.limit.below": [("plan_limit_allows", "organization", ORG, "seats", 2)],
    "org.limit.equal": [("plan_limit_allows", "organization", ORG, "seats", 3)],
    "bound.feature.on": [("plan_feature", "organization", ORG, "export")],
    "bound.limit.equal": [("plan_limit_allows", "organization", ORG, "seats", 3)],
}
ALLOWED = {
    "user.feature.on",
    "user.limit.below",
    "org.feature.on",
    "org.not_feature.off",
    "org.limit.below",
    "bound.feature.on",
}


# --------------------------------------------------------------------------- #
# The test producer: billing's plan reads from a table; the kernel's facts
# --------------------------------------------------------------------------- #
@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Billing's plan reads answered from :data:`PLANS`, recording what the
    provider was asked (in a split: what arrived over RPC)."""
    log: list[tuple[Any, ...]] = []

    async def plan_feature(
        cls: Any, tenant_type: str, tenant_id: UUID, key: str
    ) -> bool:
        log.append(("plan_feature", tenant_type, tenant_id, key))
        return PLANS.get(tenant_id, {}).get("features", {}).get(key) is True

    async def plan_limit_allows(
        cls: Any, tenant_type: str, tenant_id: UUID, key: str, used: int
    ) -> bool:
        log.append(("plan_limit_allows", tenant_type, tenant_id, key, used))
        limit = PLANS.get(tenant_id, {}).get("limits", {}).get(key)
        return isinstance(limit, int) and used < limit

    service = billing_entitlements.EntitlementsService
    monkeypatch.setattr(service, "plan_feature", classmethod(plan_feature))
    monkeypatch.setattr(service, "plan_limit_allows", classmethod(plan_limit_allows))
    return log


async def _org_features(service: Any, ctx: AuthzContext) -> bool:
    res = await ctx.get_resource()
    return bool(
        await service.has_feature("organization", res["organization_id"], res["key"])
    )


async def _org_limits(service: Any, ctx: AuthzContext) -> bool:
    res = await ctx.get_resource()
    return bool(
        await service.is_within_limit(
            "organization", res["organization_id"], res["key"], res["used"]
        )
    )


#: The kernel atoms' organization facts on the test producer (local to the
#: deciding process, asking billing's methods like the generated atoms).
KERNEL_FACTS = {
    fact.name: fact
    for fact in (
        AccessFact(
            name="organization.features",
            owner_module="organization",
            live_service=ENTITLEMENTS,
            live_resolver=_org_features,
            min_freshness="live",
            default_mode="live",
        ),
        AccessFact(
            name="organization.limits",
            owner_module="organization",
            live_service=ENTITLEMENTS,
            live_resolver=_org_limits,
            min_freshness="live",
            default_mode="live",
        ),
    )
}


class _Calls:
    """A core view recording every contract method asked, in order, and
    serving the kernel atoms' organization facts."""

    def __init__(self, core: Any) -> None:
        self._core = core
        self.log: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._core, name)

    def get_access_fact(self, name: str) -> Any:
        return KERNEL_FACTS.get(name) or self._core.get_access_fact(name)

    def get_service(self, name: str) -> Any:
        service = self._core.get_service(name)
        log = self.log

        class _Recording:
            def __getattr__(self, method: str) -> Any:
                target = getattr(service, method)

                async def call(*args: Any, **kwargs: Any) -> Any:
                    log.append(f"{name}.{method}")
                    return await target(*args, **kwargs)

                return call

        return _Recording()


@dataclass
class Meter:
    sql: int = 0
    rpc: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.sql = 0
        self.rpc = []


@pytest.fixture
def meter() -> Iterator[Meter]:
    found = Meter()

    def on_statement(*_args: Any, **_kwargs: Any) -> None:
        found.sql += 1

    hooks = ("before_cursor_execute", "begin", "commit", "rollback")
    for name in hooks:
        event.listen(Engine, name, on_statement)
    try:
        yield found
    finally:
        for name in hooks:
            event.remove(Engine, name, on_statement)


async def _seed(engine: Any) -> None:
    """USER a member of ORG (the bound tenant's kind is ``organization``)."""
    async with engine.begin() as conn:
        await conn.execute(
            insert(Organization).values(
                id=ORG, name="Plans", slug="plans", owner_id=USER
            )
        )
        await conn.execute(
            insert(OrganizationMember).values(
                organization_id=ORG, user_id=USER, role="owner"
            )
        )


def _subject() -> UserContext:
    return UserContext(
        user_id=USER,
        tenant_ids=[str(ORG)],
        claims=dict(CLAIMS),
        claims_sources=["identity", "tenancy", "billing"],
        claims_sources_failed=[],
    )


class _Req:
    def __init__(self, path: dict[str, str]) -> None:
        self.path_params = dict(path)
        self.query_params: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self.scope: dict[str, Any] = {"state": {}}
        self.state = SimpleNamespace()


Outcome = tuple[tuple[Any, ...], list[str], int, list[str], list[tuple[Any, ...]]]


async def _decide(
    core: Any, kit: Any, name: str, meter: Meter, asked: list[tuple[Any, ...]]
) -> Outcome:
    """The case decided twice; the second (steady) evaluation measured."""
    case = CASES[name]
    result: Outcome | None = None
    for _ in range(2):
        calls = _Calls(core)
        request: Any = _Req(case.path)
        ctx = AuthzContext(_subject(), request=request, core=calls)
        meter.reset()
        asked.clear()
        with ExitStack() as stack:
            if case.bound is not None:
                stack.enter_context(bind_tenant(case.bound))
            decision = await case.policy(kit).decide(ctx)
        key = (
            decision.allowed,
            decision.http_status,
            decision.category,
            decision.code,
            decision.indeterminate,
            decision.failed_requirement,
        )
        result = (key, calls.log, meter.sql, list(meter.rpc), list(asked))
    assert result is not None
    return result


# --------------------------------------------------------------------------- #
# Topologies
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def mono(
    real_uow_client: Any, sandloom_core: Any, db_engine: Any
) -> AsyncIterator[Any]:
    await _seed(db_engine)
    yield sandloom_core


@pytest_asyncio.fixture
async def split(
    real_uow_client: Any,
    sandloom_app: Any,
    db_engine: Any,
    meter: Meter,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[CoreFacade]:
    """A consumer core bound to the standard app (the provider process)."""
    await _seed(db_engine)
    for name, value in rpc_settings(
        PROVIDER, peers=(CONSUMER,), allowed_calls={CONSUMER: list(CONTRACTS)}
    ).items():
        monkeypatch.setattr(settings, name, value)
    transport = httpx.ASGITransport(app=sandloom_app)
    real = transport.handle_async_request

    async def counted(request: httpx.Request) -> httpx.Response:
        meter.rpc.append(f"{request.method} {request.url.path}")
        return await real(request)

    monkeypatch.setattr(transport, "handle_async_request", counted)
    client = httpx.AsyncClient(transport=transport, base_url="http://provider")
    monkeypatch.setattr(rpc_proxy, "_rpc_client", lambda: client)

    from fastapi import FastAPI

    app = FastAPI()
    cfg = settings.model_copy(
        update={
            **rpc_settings(CONSUMER, peers=(PROVIDER,)),
            "service_bindings": {
                c: {"base_url": "http://provider", "service_id": PROVIDER}
                for c in CONTRACTS
            },
        }
    )
    consumer = CoreFacade(app=app, settings_obj=cfg)
    app.state.core = consumer
    # the root's map, as create_app assigns it in the consumer process: the
    # bound tenant's kind is asked of the tenant authority's witness (KB-31)
    consumer._assign_claim_authorities(
        ClaimAuthorities(
            authorities={"tenant_ids": "sandloom_tenancy"},
            owners={"tenant_ids": "tenancy"},
            assigned=True,
        )
    )
    assert consumer.tenant_witness is not None

    def no_database(name: str = "default") -> Any:
        raise AssertionError(f"the consumer asked for its database {name!r}")

    consumer.database = no_database  # type: ignore[method-assign]

    def resolve(
        service_name: str,
        binding: dict[str, object],
        *,
        default_timeout_seconds: float,
        settings: object = None,
    ) -> rpc.RemoteServiceProxy:
        return rpc.RemoteServiceProxy(
            service_name=service_name,
            binding=binding,
            default_timeout_seconds=default_timeout_seconds,
            settings=cfg,
        )

    consumer.set_service_resolver(resolve)
    try:
        yield consumer
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #
def _check(name: str, base: Outcome, candidate: Outcome) -> None:
    assert candidate == base
    key, calls, _sql, _rpc, asked = candidate
    assert key[0] is (name in ALLOWED), key
    assert len(calls) == EXPECTED_CALLS[name]
    assert asked == ASKED.get(name, [])


@pytest.mark.parametrize("name", sorted(CASES))
async def test_mono_billing_atoms_equal_the_kernel_atoms(
    mono: Any, generated: Any, meter: Meter, asked: Any, name: str
) -> None:
    base = await _decide(mono, KIT_KERNEL, name, meter, asked)
    candidate = await _decide(mono, generated, name, meter, asked)
    _check(name, base, candidate)
    assert candidate[3] == []
    assert candidate[2] == EXPECTED_SQL[name], candidate[2]


#: Standard billing as shipped (no test producer): it keeps no features or
#: limits catalogue, so an organization's check is unknown. None may allow.
NO_CATALOGUE: dict[str, Case] = {
    "feature": Case(lambda k: k.Feature("export", of=Account.org()), _ORG),
    "limit": Case(lambda k: k.WithinLimit("seats", 3, of=Account.org()), _ORG),
    "not.feature": Case(lambda k: Not(k.Feature("export", of=Account.org())), _ORG),
    "not.limit": Case(lambda k: Not(k.WithinLimit("seats", 3, of=Account.org())), _ORG),
    "or": Case(
        lambda k: Or(
            k.Feature("export", of=Account.org()),
            k.WithinLimit("seats", 3, of=Account.org()),
        ),
        _ORG,
    ),
    "or.not": Case(
        lambda k: Or(
            Not(k.Feature("export", of=Account.org())),
            Not(k.WithinLimit("seats", 3, of=Account.org())),
        ),
        _ORG,
    ),
    "bound.not.feature": Case(
        lambda k: Not(k.Feature("export", of=Account.bound_tenant())), bound=ORG
    ),
}


async def _no_catalogue(core: Any, generated: Any, name: str) -> tuple[Any, ...]:
    case = NO_CATALOGUE[name]
    request: Any = _Req(case.path)
    ctx = AuthzContext(_subject(), request=request, core=core)
    with ExitStack() as stack:
        if case.bound is not None:
            stack.enter_context(bind_tenant(case.bound))
        decision = await case.policy(generated).decide(ctx)
    return (
        decision.allowed,
        decision.http_status,
        decision.category,
        decision.code,
        decision.indeterminate,
        ENTITLEMENTS in decision.reason and "catalogue" in decision.reason,
    )


#: The decision of every no-catalogue case: an indeterminate 501 whose
#: reason names billing's contract and what it lacks (C-B5, G-AUTHZ-12).
UNKNOWN = (False, 501, Category.POLICY_MISCONFIGURED, "access_unknown", True, True)


@pytest.mark.parametrize("name", sorted(NO_CATALOGUE))
async def test_mono_billing_without_a_catalogue_is_501_never_an_allow(
    mono: Any, generated: Any, name: str
) -> None:
    assert await _no_catalogue(mono, generated, name) == UNKNOWN


@pytest.mark.parametrize("name", sorted(NO_CATALOGUE))
async def test_split_billing_without_a_catalogue_is_501_never_an_allow(
    split: CoreFacade, generated: Any, meter: Meter, name: str
) -> None:
    meter.reset()
    assert await _no_catalogue(split, generated, name) == UNKNOWN
    # billing was asked over the RPC and its 501 reached the consumer
    asked_billing = f"POST /_sandloom/rpc/{ENTITLEMENTS}/"
    assert any(r.startswith(asked_billing) for r in meter.rpc), meter.rpc


async def test_without_a_catalogue_the_user_claims_path_is_unchanged(
    mono: Any, generated: Any, meter: Meter
) -> None:
    """``Account.user()`` still reads the token's claims with zero I/O."""
    request: Any = _Req({})
    ctx = AuthzContext(_subject(), request=request, core=_Calls(mono))
    meter.reset()
    assert (await generated.Feature("export", of=Account.user()).decide(ctx)).allowed
    denied = await generated.WithinLimit("seats", 3, of=Account.user()).decide(ctx)
    assert not denied.allowed and denied.http_status == 402
    assert meter.sql == 0 and meter.rpc == []


@pytest.mark.parametrize("name", sorted(CASES))
async def test_split_billing_atoms_carry_their_parameters_over_rpc(
    split: CoreFacade, generated: Any, meter: Meter, asked: Any, name: str
) -> None:
    base = await _decide(split, KIT_KERNEL, name, meter, asked)
    candidate = await _decide(split, generated, name, meter, asked)
    _check(name, base, candidate)
    # every call one POST to the provider, its database work in its own
    # units; the claims tier none at all
    assert len(candidate[3]) == EXPECTED_CALLS[name]
    assert candidate[2] == EXPECTED_SQL[name]
    assert all(r.startswith("POST /_sandloom/rpc/") for r in candidate[3])
