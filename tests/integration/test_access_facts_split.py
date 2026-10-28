"""Access facts across processes: ``<owner>.facts.v1`` over real ASGI-RPC.

Two in-process apps wired exactly like a split deployment:

- **owner** ("billing" process) — registers billing's real
  ``subscription.active`` / ``organization.subscription.active`` facts (which
  auto-exposes ``billing.facts.v1``), plus stand-in entitlements / membership
  contracts, and serves the internal RPC routes;
- **consumer** (a tenancy-only process) — registers NO facts and binds only
  ``billing.facts.v1`` via ``SERVICE_BINDINGS``; every RPC goes through the
  real proxy → ``/_sandloom/rpc`` server → contract runtime (ASGI transport).

The same policies must decide identically in the owner ("monolith") and in the
consumer — the review found the consumer answered ``500 fact_unregistered``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from sandloom.core import rpc
from sandloom.core.contract_testing import rpc_settings
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import contract_method
from sandloom.core.rpc import proxy as rpc_proxy
from sandloom.shared.authz import Account, Entitled, Subscribed
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.results import AccessDecision
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

USER = uuid4()
ORG = uuid4()
OTHER_ORG = uuid4()

#: Live state the stand-in contracts answer from (owner side).
STATE: dict[str, Any] = {}


class _Entitlements:
    @classmethod
    @contract_method
    async def check_subscription_active(cls, tenant_type: str, tenant_id: UUID) -> bool:
        STATE.setdefault("live_calls", []).append((tenant_type, tenant_id))
        return bool(STATE["active"].get((tenant_type, tenant_id), False))


class _Membership:
    @classmethod
    @contract_method
    async def get_role(cls, organization_id: UUID, user_id: UUID) -> str | None:
        if organization_id == ORG and user_id == USER:
            return "member"
        return None


class _FakeUoW:
    session = SimpleNamespace(info={})

    async def __aenter__(self) -> _FakeUoW:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeDatabase:
    def uow_factory(self) -> Callable[[], _FakeUoW]:
        return _FakeUoW

    def outbox(self) -> None:
        return None

    def audit(self) -> None:
        return None


def _owner() -> tuple[FastAPI, CoreFacade]:
    from sandloom_billing.access import (
        ORG_SUBSCRIPTION_ACTIVE_FACT,
        SUBSCRIPTION_ACTIVE_FACT,
    )

    app = FastAPI()
    core = CoreFacade(app=app)
    app.state.core = core
    core.database = lambda name="default": _FakeDatabase()  # type: ignore[method-assign]
    core.register_service("billing.entitlements.v1", _Entitlements)
    core.register_service("tenancy.membership.v1", _Membership)
    core.register_access_fact(SUBSCRIPTION_ACTIVE_FACT)
    core.register_access_fact(ORG_SUBSCRIPTION_ACTIVE_FACT)
    rpc.install_service_rpc_routes(app, core)
    return app, core


def _consumer(bindings: dict[str, Any]) -> CoreFacade:
    from sandloom.core.config.settings import settings

    app = FastAPI()
    cfg = settings.model_copy(
        update={
            **rpc_settings("tenancy", peers=("billing",)),
            "service_bindings": bindings,
        }
    )
    core = CoreFacade(app=app, settings_obj=cfg)
    app.state.core = core

    def resolve(
        service_name: str,
        binding: dict[str, object],
        *,
        default_timeout_seconds: float,
        settings: object = None,
    ) -> rpc.RemoteServiceProxy:
        # The consumer process's own keys sign its calls.
        return rpc.RemoteServiceProxy(
            service_name=service_name,
            binding=binding,
            default_timeout_seconds=default_timeout_seconds,
            settings=cfg,
        )

    core.set_service_resolver(resolve)
    return core


@pytest.fixture
def split(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    from sandloom.core.config.settings import settings

    # The owner process is keyed as ``billing``; it admits ``tenancy``'s calls.
    owner_keys = rpc_settings(
        "billing", peers=("tenancy",), allowed_calls={"tenancy": ["billing.facts.v1"]}
    )
    for name, value in owner_keys.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "billing_deny_on_past_due", False)
    STATE.clear()
    STATE["active"] = {}
    owner_app, owner = _owner()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=owner_app), base_url="http://billing"
    )
    monkeypatch.setattr(rpc_proxy, "_rpc_client", lambda: client)
    consumer = _consumer(
        {"billing.facts.v1": {"base_url": "http://billing", "service_id": "billing"}}
    )
    assert owner.service_registry.contains("billing.facts.v1")
    yield {"owner": owner, "consumer": consumer}


def _subject(**claims: Any) -> UserContext:
    return UserContext.model_validate({"sub": str(USER), **claims})


def _request(org_id: UUID | None) -> Any:
    path = {"org_id": str(org_id)} if org_id is not None else {}
    return SimpleNamespace(path_params=path, query_params={}, headers={})


def _key(d: AccessDecision) -> tuple[Any, ...]:
    return (d.allowed, d.http_status, d.category, d.code, d.indeterminate)


async def _both(
    split: dict[str, Any], policy: Any, subject: UserContext, request: Any = None
) -> tuple[AccessDecision, AccessDecision]:
    mono = await policy.decide(
        AuthzContext(subject, core=split["owner"], request=request)
    )
    remote = await policy.decide(
        AuthzContext(subject, core=split["consumer"], request=request)
    )
    return mono, remote


MINTED = {"claims_sources": ["identity", "billing"], "claims_sources_failed": []}
BILLING_FAILED = {"claims_sources": ["identity"], "claims_sources_failed": ["billing"]}


@pytest.mark.parametrize(
    ("claims", "live_active", "allowed"),
    [
        ({**MINTED, "subscription_status": "active"}, False, True),  # claim
        ({**MINTED, "subscription_status": "past_due"}, False, True),  # grace
        ({**MINTED, "subscription_status": "canceled"}, True, False),  # claim
        ({**BILLING_FAILED}, True, True),  # escalates to live
        ({**BILLING_FAILED}, False, False),
    ],
)
async def test_entitled_subscription_same_decision_in_split(
    split: dict[str, Any], claims: dict[str, Any], live_active: bool, allowed: bool
) -> None:
    STATE["active"][("user", USER)] = live_active
    for policy in (Entitled("subscription.active"), Subscribed(of=Account.user())):
        mono, remote = await _both(split, policy, _subject(**claims))
        assert _key(remote) == _key(mono), policy
        assert mono.allowed is allowed
        if not allowed:
            assert mono.http_status == 402


async def test_owner_settings_drive_the_remote_claim_rule(
    split: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "billing_deny_on_past_due", True)
    split["consumer"].remote_access_fact_cache.clear()
    subject = _subject(**MINTED, subscription_status="past_due")
    mono, remote = await _both(split, Entitled("subscription.active"), subject)
    assert _key(remote) == _key(mono)
    assert not mono.allowed and mono.http_status == 402


async def test_claim_mode_needs_no_live_rpc_after_describe(
    split: dict[str, Any],
) -> None:
    subject = _subject(**MINTED, subscription_status="active")
    ctx = AuthzContext(subject, core=split["consumer"])
    assert (await Entitled("subscription.active").decide(ctx)).allowed
    assert STATE.get("live_calls") is None  # answered from the claim


@pytest.mark.parametrize(
    ("org_id", "org_active", "allowed", "status"),
    [
        (ORG, True, True, 200),
        (ORG, False, False, 402),
        (OTHER_ORG, True, False, 403),  # not a member (FactDenied over RPC)
        (None, True, False, 403),  # no organization in context
    ],
)
async def test_entitled_org_subscription_same_decision_in_split(
    split: dict[str, Any],
    org_id: UUID | None,
    org_active: bool,
    allowed: bool,
    status: int,
) -> None:
    if org_id is not None:
        STATE["active"][("organization", org_id)] = org_active
    mono, remote = await _both(
        split,
        Entitled("organization.subscription.active"),
        _subject(**MINTED),
        _request(org_id),
    )
    assert _key(remote) == _key(mono)
    assert mono.allowed is allowed
    assert mono.http_status == status


async def test_unknown_fact_is_501_in_both(split: dict[str, Any]) -> None:
    mono, remote = await _both(split, Entitled("nope.fact"), _subject(**MINTED))
    assert _key(remote) == _key(mono)
    assert mono.indeterminate and mono.http_status == 501
    assert mono.code == "fact_unregistered"


async def test_unbound_fact_is_501_not_500(split: dict[str, Any]) -> None:
    lonely = _consumer({})
    decision = await Entitled("subscription.active").decide(
        AuthzContext(_subject(**MINTED), core=lonely)
    )
    assert decision.indeterminate and not decision.allowed
    assert decision.http_status == 501
    # Negation never turns it into an allow.
    negated = await (~Entitled("subscription.active")).decide(
        AuthzContext(_subject(**MINTED), core=lonely)
    )
    assert not negated.allowed and negated.http_status == 501


async def test_unreachable_owner_is_503(
    split: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("billing down", request=request)

    down = httpx.AsyncClient(transport=httpx.MockTransport(_down))
    monkeypatch.setattr(rpc_proxy, "_rpc_client", lambda: down)
    consumer = _consumer(
        {"billing.facts.v1": {"base_url": "http://billing", "service_id": "billing"}}
    )
    decision = await Entitled("subscription.active").decide(
        AuthzContext(_subject(**MINTED, subscription_status="active"), core=consumer)
    )
    assert decision.indeterminate and decision.http_status == 503
    assert decision.code == "fact_owner_unavailable"


async def test_describe_is_declarative_and_cached(split: dict[str, Any]) -> None:
    described = await split["owner"].get_service("billing.facts.v1").describe()
    by_name = {d["name"]: d for d in described}
    sub = by_name["subscription.active"]
    assert sub["claim_key"] == "subscription_status"
    assert sub["claim_rule"] == {
        "kind": "one_of",
        "values": sorted(["active", "past_due", "trialing"], key=repr),
    }
    assert sub["deny_category"] == "payment_required"
    org = by_name["organization.subscription.active"]
    assert org["claim_key"] is None and org["min_freshness"] == "live"

    consumer = split["consumer"]
    ctx = AuthzContext(_subject(**MINTED, subscription_status="active"), core=consumer)
    await Entitled("subscription.active").decide(ctx)
    assert ("fact", "subscription.active") in consumer.remote_access_fact_cache
    assert ("fact", "organization.subscription.active") in (
        consumer.remote_access_fact_cache
    )
