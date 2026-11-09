"""The authorization stand as a CI count test (ADR-0014 decision 9; KB-30).

The PoC stand (``design/kernel-boundary/poc/authz/stand.py``) promoted: every
policy shape of the stand is decided twice on the real tenancy and billing
providers over the real test database, once with the kernel atoms
(``sandloom.shared.authz``) and once with the atoms ``sandloom generate``
writes beside the consumer's stubs (``tenancy.membership.v1``'s committed
copy, ``billing.entitlements.v1`` rendered here as generate renders it). For
each case, steady state (the second evaluation):

* the decision (allowed, status, category, code, indeterminate) is equal;
* the provider calls and their order are equal (what was ASKED);
* the I/O is equal and pinned: database round trips of the providers'
  units (every statement plus each BEGIN / COMMIT / ROLLBACK), internal-RPC
  requests, and in a split the consumer touches no database at all.

Two topologies:

* **mono**: the standard composition (identity, tenancy, billing) in one
  process, the providers called in-process;
* **split**: a consumer core with no module and no database binds
  ``tenancy.membership.v1``, ``billing.entitlements.v1`` and
  ``billing.facts.v1`` through ``SERVICE_BINDINGS`` to the standard app as
  the provider process, over the real internal RPC (proxy, pair MAC, the
  ``/_sandloom/rpc`` server, the providers' own units; ASGI transport).

Pinned numbers (the plan's budget, KB-30): the decisive shape
``OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant())`` is 3
calls / 9 database round trips mono, 3 RPC / 9 provider round trips / 0
consumer split; ``Credits`` 1 / 4; the claims tier 0 / 0.

ADR-0014 K5 (KB-35, Z-9 / C-F7): with ``tenancy.membership.v1`` bound to a
provider no connection reaches, the generated ``OrgRole`` is indeterminate
503 ``owner_unavailable``; ``OrgRole | SubscriptionActive`` allows only on the
subscription, ``~OrgRole`` never allows, each atom asks its provider as often
as before; the provider's allowlist refusal (``rpc_caller_forbidden``) stays
an error. K6 (KB-36, Z-5 / C-B7): after the boot probe (one ``GET`` per
remote binding) a first request makes no describe ``GET``.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.engine import Engine

import sandloom.shared.authz as kernel
from sandloom.core import rpc
from sandloom.core.cli.generated import render_stub
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import rpc_settings
from sandloom.core.di import bind_tenant
from sandloom.core.facade import CoreFacade
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.core.rpc import readiness as rpc_readiness
from sandloom.core.rpc.errors import RemoteServiceUnavailable
from sandloom.core.testing.fixtures import *  # noqa: F403 - db_engine, real_uow_client
from sandloom.shared.authz import Account, Not, Role
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.subject import UserContext
from sandloom_billing._generated.contracts import tenancy_membership_v1 as gen_tenancy
from sandloom_billing.contracts import EntitlementsContract
from sandloom_billing.models import BillingCustomer, BillingSubscription
from sandloom_billing.models.catalog import BillingPrice, BillingProduct
from sandloom_billing.models.credits import CreditBalance
from sandloom_tenancy.models import Organization, OrganizationMember

pytestmark = pytest.mark.asyncio

USER = UUID("00000000-0000-4000-8000-00000000a001")
MEMBER = UUID("00000000-0000-4000-8000-00000000a002")
OUTSIDER = UUID("00000000-0000-4000-8000-00000000a0ff")
ORG = UUID("00000000-0000-4000-8000-00000000b001")
ORG2 = UUID("00000000-0000-4000-8000-00000000b002")
CONTRACTS = ("tenancy.membership.v1", "billing.entitlements.v1", "billing.facts.v1")
PROVIDER, CONSUMER = "provider", "api"


# --------------------------------------------------------------------------- #
# The two atom sets: the kernel's and the generated ones
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """``billing.entitlements.v1``'s stub as ``sandloom generate`` writes it
    (no module consumes it yet), imported as a consumer imports it."""
    path = tmp_path_factory.mktemp("stub") / "billing_entitlements_v1.py"
    path.write_text(render_stub("billing.entitlements.v1", EntitlementsContract))
    spec = importlib.util.spec_from_file_location("_kb30_billing_stub", path)
    assert spec is not None and spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield SimpleNamespace(
        OrgRole=gen_tenancy.OrgRole,
        SubscriptionActive=module.SubscriptionActive,
        Credits=module.Credits,
        Subscribed=module.Subscribed,
    )
    sys.modules.pop(spec.name, None)


KIT_KERNEL = SimpleNamespace(
    OrgRole=kernel.OrgRole,
    SubscriptionActive=kernel.SubscriptionActive,
    Credits=kernel.Credits,
    Subscribed=kernel.Subscribed,
)


@dataclass(frozen=True)
class Case:
    """One stand case: the policy (built from a kit), the subject, the
    request and the bound tenant."""

    policy: Callable[[Any], Any]
    subject: str = "admin"
    path: dict[str, str] = field(default_factory=dict)
    bound: UUID | None = None
    status: str = "active"


def _DECISIVE(k: Any) -> Any:
    """The decisive shape: bound tenant, organization role and subscription."""
    return k.OrgRole("admin") & k.SubscriptionActive(of=Account.bound_tenant())


CASES: dict[str, Case] = {
    "role.admin": Case(lambda k: Role("admin")),
    "subscribed": Case(lambda k: k.Subscribed(of=Account.user())),
    "subscribed.past_due": Case(
        lambda k: k.Subscribed(of=Account.user()), status="past_due"
    ),
    "subactive": Case(lambda k: k.SubscriptionActive(of=Account.user())),
    "credits": Case(lambda k: k.Credits(10, of=Account.user())),
    "credits.short": Case(lambda k: k.Credits(1000, of=Account.user())),
    "role_or_credits.admin": Case(
        lambda k: Role("admin") | k.Credits(10, of=Account.user())
    ),
    "role_or_credits.user": Case(
        lambda k: Role("admin") | k.Credits(10, of=Account.user()), subject="user"
    ),
    "orgrole": Case(lambda k: k.OrgRole("admin"), path={"org_id": str(ORG)}),
    "orgrole_and_subactive": Case(
        lambda k: k.OrgRole("admin") & k.SubscriptionActive(of=Account.org()),
        path={"org_id": str(ORG)},
    ),
    "orgrole_and_subactive.outsider": Case(
        lambda k: k.OrgRole("admin") & k.SubscriptionActive(of=Account.org()),
        subject="outsider",
        path={"org_id": str(ORG)},
    ),
    "bound.decisive": Case(_DECISIVE, bound=ORG),
    "bound.member": Case(_DECISIVE, subject="member", bound=ORG),
    "bound.admin_inactive": Case(_DECISIVE, bound=ORG2),
    "bound.outsider": Case(_DECISIVE, subject="outsider", bound=ORG),
    "bound.subscribed": Case(
        lambda k: k.Subscribed(of=Account.bound_tenant()), bound=ORG
    ),
    "or_sub_user.admin": Case(
        lambda k: k.OrgRole("admin") | k.SubscriptionActive(of=Account.user()),
        path={"org_id": str(ORG)},
    ),
    "not_member.admin": Case(
        lambda k: Not(k.OrgRole("member")), path={"org_id": str(ORG)}
    ),
    "invalid_org": Case(lambda k: k.OrgRole("admin"), path={"org_id": "nope"}),
}

#: Steady state per request: (provider calls, provider database round trips).
EXPECTED: dict[str, tuple[int, int]] = {
    "role.admin": (0, 0),
    "subscribed": (0, 0),
    "subscribed.past_due": (0, 0),
    "subactive": (1, 3),
    "credits": (1, 4),
    "credits.short": (1, 4),
    "role_or_credits.admin": (0, 0),
    "role_or_credits.user": (1, 4),
    "orgrole": (1, 3),
    "orgrole_and_subactive": (2, 6),
    "orgrole_and_subactive.outsider": (1, 3),
    "bound.decisive": (3, 9),
    "bound.member": (1, 3),
    "bound.admin_inactive": (3, 9),
    "bound.outsider": (1, 3),
    "bound.subscribed": (2, 6),
    "or_sub_user.admin": (1, 3),
    "not_member.admin": (1, 3),
    "invalid_org": (0, 0),
}


def _subject(case: Case) -> UserContext:
    user = {"outsider": OUTSIDER, "member": MEMBER}.get(case.subject, USER)
    return UserContext(
        user_id=user,
        roles=["admin"] if case.subject == "admin" else [],
        tenant_ids=[str(ORG), str(ORG2)],
        claims={"subscription_status": case.status},
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


# --------------------------------------------------------------------------- #
# Counting where the I/O leaves: provider calls, SQL, RPC
# --------------------------------------------------------------------------- #
class _Calls:
    """A core view recording every contract method asked, in order."""

    def __init__(self, core: Any) -> None:
        self._core = core
        self.log: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._core, name)

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
def meter(monkeypatch: pytest.MonkeyPatch) -> Iterator[Meter]:
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
    """The stand's rows: USER admin of ORG (active subscription, 100 credits,
    also on the personal account), MEMBER a member of ORG, USER admin of ORG2
    whose subscription is canceled."""
    now = datetime.now(UTC)
    rows: list[tuple[Any, list[dict[str, Any]]]] = [
        (
            Organization.__table__,
            [
                {"id": ORG, "name": "Stand", "slug": "stand", "owner_id": USER},
                {
                    "id": ORG2,
                    "name": "Inactive",
                    "slug": "inactive",
                    "owner_id": MEMBER,
                },
            ],
        ),
        (
            OrganizationMember.__table__,
            [
                {"organization_id": ORG, "user_id": USER, "role": "owner"},
                {"organization_id": ORG, "user_id": MEMBER, "role": "member"},
                {"organization_id": ORG2, "user_id": USER, "role": "admin"},
            ],
        ),
        (
            BillingProduct.__table__,
            [{"id": 9001, "name": "Stand plan", "type": "subscription"}],
        ),
        (
            BillingPrice.__table__,
            [
                {
                    "id": 9001,
                    "product_id": 9001,
                    "amount": 1000,
                    "currency": "usd",
                    "recurring_interval": "month",
                }
            ],
        ),
    ]
    customers = {"user": (USER, None), "org": (None, ORG), "org2": (None, ORG2)}
    ids = {key: UUID(int=0xC000 + i) for i, key in enumerate(customers)}
    rows.append(
        (
            BillingCustomer.__table__,
            [
                {"id": ids[k], "user_id": u, "organization_id": o}
                for k, (u, o) in customers.items()
            ],
        )
    )
    rows.append(
        (
            BillingSubscription.__table__,
            [
                {
                    "customer_id": ids[k],
                    "price_id": 9001,
                    "status": "canceled" if k == "org2" else "active",
                    "current_period_end": now
                    + (timedelta(days=-1) if k == "org2" else timedelta(days=30)),
                    "cancel_at_period_end": False,
                    "origin": "legacy",
                }
                for k in customers
            ],
        )
    )
    rows.append(
        (
            CreditBalance.__table__,
            [{"customer_id": ids[k], "amount": 100} for k in ("user", "org")],
        )
    )
    async with engine.begin() as conn:
        for table, values in rows:
            for value in values:
                defaults = {
                    c.name: c.default.arg(None)
                    if callable(c.default.arg)
                    else c.default.arg
                    for c in table.columns
                    if c.default is not None
                    and c.name not in value
                    and not getattr(c.default, "is_sequence", False)
                }
                await conn.execute(table.insert().values(**{**defaults, **value}))


Outcome = tuple[tuple[Any, ...], list[str], int, list[str]]

#: The one planned difference (answer #24): ``Credits`` compares in billing
#: now, through the new read with the same query and binding.
_RENAMED = {
    "billing.entitlements.v1.get_credit_balance": (
        "billing.entitlements.v1.has_credits_at_least"
    ),
    "/_sandloom/rpc/billing.entitlements.v1/get_credit_balance": (
        "/_sandloom/rpc/billing.entitlements.v1/has_credits_at_least"
    ),
}


def _as_candidate(outcome: Outcome) -> Outcome:
    key, calls, sql, rpcs = outcome
    renamed = [_RENAMED.get(c, c) for c in calls]
    posts = []
    for r in rpcs:
        method, _, path = r.partition(" ")
        posts.append(f"{method} {_RENAMED.get(path, path)}")
    return key, renamed, sql, posts


async def _decide(core: Any, kit: Any, name: str, meter: Meter) -> Outcome:
    """The case decided twice; the second (steady) evaluation measured."""
    case = CASES[name]
    result: Outcome | None = None
    for _ in range(2):
        calls = _Calls(core)
        policy = case.policy(kit)
        request: Any = _Req(case.path)
        ctx = AuthzContext(_subject(case), request=request, core=calls)
        meter.reset()
        with ExitStack() as stack:
            if case.bound is not None:
                stack.enter_context(bind_tenant(case.bound))
            decision = await policy.decide(ctx)
        key = (
            decision.allowed,
            decision.http_status,
            decision.category,
            decision.code,
            decision.indeterminate,
            decision.failed_requirement,
        )
        result = (key, calls.log, meter.sql, list(meter.rpc))
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


#: The host of a binding whose provider is down (K5): every connection fails.
DOWN_HOST = "down"


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
    async for consumer in _consumer(sandloom_app, meter, monkeypatch):
        yield consumer


async def _consumer(
    sandloom_app: Any,
    meter: Meter,
    monkeypatch: pytest.MonkeyPatch,
    *,
    allowed: tuple[str, ...] = CONTRACTS,
    down: tuple[str, ...] = (),
) -> AsyncIterator[CoreFacade]:
    """The consumer core; ``allowed``: what the provider lets it call;
    ``down``: contracts bound to a provider no connection reaches."""
    for name, value in rpc_settings(
        PROVIDER, peers=(CONSUMER,), allowed_calls={CONSUMER: list(allowed)}
    ).items():
        monkeypatch.setattr(settings, name, value)
    transport = httpx.ASGITransport(app=sandloom_app)
    real = transport.handle_async_request

    async def counted(request: httpx.Request) -> httpx.Response:
        meter.rpc.append(f"{request.method} {request.url.path}")
        if request.url.host == DOWN_HOST:
            raise httpx.ConnectError("connection refused", request=request)
        return await real(request)

    monkeypatch.setattr(transport, "handle_async_request", counted)
    client = httpx.AsyncClient(transport=transport, base_url="http://provider")
    monkeypatch.setattr(rpc_proxy, "_rpc_client", lambda: client)
    monkeypatch.setattr(rpc_readiness, "_rpc_client", lambda: client)
    monkeypatch.setattr(rpc_proxy, "_RPC_RETRY_DELAY_SECONDS", 0.0)

    from fastapi import FastAPI

    app = FastAPI()
    cfg = settings.model_copy(
        update={
            **rpc_settings(CONSUMER, peers=(PROVIDER,)),
            "service_bindings": {
                c: {
                    "base_url": f"http://{DOWN_HOST if c in down else 'provider'}",
                    "service_id": PROVIDER,
                }
                for c in CONTRACTS
            },
        }
    )
    consumer = CoreFacade(app=app, settings_obj=cfg)
    app.state.core = consumer

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
@pytest.mark.parametrize("name", sorted(CASES))
async def test_mono_generated_atoms_equal_the_kernel_atoms(
    mono: Any, generated: Any, meter: Meter, name: str
) -> None:
    base = await _decide(mono, KIT_KERNEL, name, meter)
    candidate = await _decide(mono, generated, name, meter)
    assert candidate == _as_candidate(base)
    calls, sql = EXPECTED[name]
    assert (len(candidate[1]), candidate[2], candidate[3]) == (calls, sql, [])


@pytest.mark.parametrize("name", sorted(CASES))
async def test_split_generated_atoms_equal_the_kernel_atoms(
    split: CoreFacade, generated: Any, meter: Meter, name: str
) -> None:
    base = await _decide(split, KIT_KERNEL, name, meter)
    candidate = await _decide(split, generated, name, meter)
    assert candidate == _as_candidate(base)
    calls, sql = EXPECTED[name]
    # every call one POST to the provider; its database work in its own units
    assert (len(candidate[1]), candidate[2], len(candidate[3])) == (calls, sql, calls)
    assert all(r.startswith("POST /_sandloom/rpc/") for r in candidate[3])


async def test_the_decisive_shape_asks_the_same_three_calls(
    mono: Any, generated: Any, meter: Meter
) -> None:
    _key, calls, sql, _rpc = await _decide(mono, generated, "bound.decisive", meter)
    assert calls == [
        "tenancy.membership.v1.has_role_at_least",
        "tenancy.membership.v1.has_role_at_least",
        "billing.entitlements.v1.check_subscription_active",
    ]
    assert sql == 9


# --------------------------------------------------------------------------- #
# K5 (Z-9, C-F7): tenancy down in a split, inside the algebra
# --------------------------------------------------------------------------- #
OUTAGE_CASES: dict[str, Case] = {
    "orgrole": CASES["orgrole"],
    "or_sub_user.admin": CASES["or_sub_user.admin"],
    "or_sub_user.outsider": Case(
        CASES["or_sub_user.admin"].policy, subject="outsider", path={"org_id": str(ORG)}
    ),
    "not_member.admin": CASES["not_member.admin"],
}

#: (allowed, status, code) with ``tenancy.membership.v1`` unreachable, and the
#: provider calls asked (tenancy's attempted, billing's answered).
OUTAGE_EXPECTED: dict[str, tuple[tuple[bool, int, str], list[str]]] = {
    "orgrole": ((False, 503, "owner_unavailable"), ["membership"]),
    # the subscription reliably allowed: the Or allows on it
    "or_sub_user.admin": ((True, 200, "ok"), ["membership", "entitlements"]),
    # no subscription: the broken branch decides, never an allow
    "or_sub_user.outsider": (
        (False, 503, "owner_unavailable"),
        ["membership", "entitlements"],
    ),
    "not_member.admin": ((False, 503, "owner_unavailable"), ["membership"]),
}


@pytest_asyncio.fixture
async def tenancy_down(
    real_uow_client: Any,
    sandloom_app: Any,
    db_engine: Any,
    meter: Meter,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[CoreFacade]:
    await _seed(db_engine)
    async for consumer in _consumer(
        sandloom_app, meter, monkeypatch, down=("tenancy.membership.v1",)
    ):
        yield consumer


@pytest.mark.parametrize("name", sorted(OUTAGE_CASES))
async def test_split_tenancy_down_is_indeterminate_503(
    tenancy_down: CoreFacade, generated: Any, meter: Meter, name: str
) -> None:
    case = OUTAGE_CASES[name]
    calls = _Calls(tenancy_down)
    request: Any = _Req(case.path)
    ctx = AuthzContext(_subject(case), request=request, core=calls)
    meter.reset()
    decision = await case.policy(generated).decide(ctx)
    (allowed, status, code), asked = OUTAGE_EXPECTED[name]
    assert (decision.allowed, decision.http_status, decision.code) == (
        allowed,
        status,
        code,
    )
    assert decision.indeterminate is (not allowed)
    # M-calls unchanged: each atom asks its provider once, as before K5
    assert [c.split(".")[1] for c in calls.log] == asked
    # the indeterminate branch is reported even where its sibling allowed
    assert tenancy_down.get_metric("access_indeterminate_branch_total") == 1


async def test_split_allowlist_refusal_stays_an_error(
    real_uow_client: Any,
    sandloom_app: Any,
    db_engine: Any,
    generated: Any,
    meter: Meter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``rpc_caller_forbidden`` (the provider's RPC_ALLOWED_CALLS) is a broken
    security invariant, not an outage: an error, the ``Or`` does not allow."""
    await _seed(db_engine)
    billing_only = ("billing.entitlements.v1", "billing.facts.v1")
    async for consumer in _consumer(
        sandloom_app, meter, monkeypatch, allowed=billing_only
    ):
        case = CASES["or_sub_user.admin"]
        calls = _Calls(consumer)
        request: Any = _Req(case.path)
        ctx = AuthzContext(_subject(case), request=request, core=calls)
        with pytest.raises(RemoteServiceUnavailable) as raised:
            await case.policy(generated).decide(ctx)
        assert raised.value.code == "rpc_caller_forbidden"
        assert calls.log == ["tenancy.membership.v1.has_role_at_least"]
        assert consumer.get_metric("access_indeterminate_branch_total") is None


# --------------------------------------------------------------------------- #
# K6 (Z-5, C-B7): no describe GET on a first request after the boot probe
# --------------------------------------------------------------------------- #
FIRST_REQUEST_CASES = ("orgrole_and_subactive", "or_sub_user.admin", "bound.decisive")


async def _first_request(core: Any, kit: Any, name: str, meter: Meter) -> list[str]:
    case = CASES[name]
    request: Any = _Req(case.path)
    ctx = AuthzContext(_subject(case), request=request, core=core)
    meter.reset()
    with ExitStack() as stack:
        if case.bound is not None:
            stack.enter_context(bind_tenant(case.bound))
        assert (await case.policy(kit).decide(ctx)).allowed
    return list(meter.rpc)


@pytest.mark.parametrize("name", FIRST_REQUEST_CASES)
async def test_without_the_boot_probe_a_first_request_describes(
    split: CoreFacade, generated: Any, meter: Meter, name: str
) -> None:
    """The request-path describe K6 removes (what the next test compares to):
    one ``GET`` per contract the first request calls."""
    first = await _first_request(split, generated, name, meter)
    posts = {r.split(" ")[1].rsplit("/", 1)[0] for r in first if r.startswith("POST ")}
    assert sorted(r for r in first if r.startswith("GET ")) == sorted(
        f"GET {p}" for p in posts
    )


@pytest.mark.parametrize("name", FIRST_REQUEST_CASES)
async def test_the_boot_probe_seeds_every_binding_no_describe_on_request(
    split: CoreFacade, generated: Any, meter: Meter, name: str
) -> None:
    meter.reset()
    result = await rpc_readiness.describe_remote_bindings(split)
    assert result.status == "ok"
    # one GET per remote binding at boot, none of them with a known snapshot
    assert sorted(meter.rpc) == sorted(f"GET /_sandloom/rpc/{c}" for c in CONTRACTS)
    first = await _first_request(split, generated, name, meter)
    assert not [r for r in first if r.startswith("GET ")]
    calls, _sql = EXPECTED[name]
    assert len(first) == calls  # only the access checks' POSTs
