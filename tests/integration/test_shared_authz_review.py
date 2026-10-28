"""Regression tests for the ``sandloom.shared`` authorization review findings.

Pure (no DB): three-valued logic through ``Not``/``And``/``Or``, live atoms
resolving through ``core.get_service`` (incl. remote SERVICE_BINDINGS proxies),
HTTP guards answering with the decision's status, ``AccessDenied`` as an
``HTTPException``, fact-cache keyed by mode, mandatory claim provenance,
module-agnostic ``UserContext`` claims, pluggable live-contract adapters,
organization tenant resolution, lazy DB for claims-only policies, atomic access
metrics and ``iat`` enforcement.
"""

from __future__ import annotations

import pickle
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI, HTTPException

from sandloom.core.auth.tokens import create_access_token
from sandloom.core.config.settings import settings
from sandloom.core.di import bind_tenant, get_db
from sandloom.core.facade import CoreFacade
from sandloom.core.http.error_handlers import register_error_handlers
from sandloom.core.module_api._service_registry import ServiceUnavailableInDeployment
from sandloom.shared.authz import (
    ENTITLEMENTS,
    AccessDecision,
    AccessDenied,
    AccessFact,
    Account,
    Category,
    ClaimRule,
    Credits,
    Entitled,
    FactUnavailable,
    Feature,
    Not,
    OrgRole,
    Perm,
    PolicyIndeterminate,
    Role,
    Subscribed,
    SubscriptionActive,
    WithinLimit,
    enforce,
    register_live_adapter,
    require,
    resolve_fact,
)
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.live_contracts import reset_live_adapters
from sandloom.shared.authz.metrics import increment
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

USER = uuid4()
ENT = "billing.entitlements.v1"


def _subject(**overrides: object) -> UserContext:
    data: dict[str, object] = {"user_id": USER}
    data.update(overrides)
    return UserContext(**data)


class _StubCore:
    """Mirrors CoreFacade.get_service: local, else 501 ServiceUnavailable."""

    def __init__(self, *, services: dict[str, object] | None = None) -> None:
        self._services = services or {}
        self.metrics: dict[str, int] = {}
        self.facts: dict[str, AccessFact] = {}

    def get_service(self, name: str) -> object:
        if name not in self._services:
            raise ServiceUnavailableInDeployment(name)
        return self._services[name]

    def get_metric(self, name: str) -> object:
        return self.metrics.get(name)

    def register_metric(self, name: str, value: object) -> None:
        self.metrics[name] = int(value)  # type: ignore[arg-type]

    def get_access_fact(self, name: str) -> AccessFact | None:
        return self.facts.get(name)


def _ctx(subject: UserContext, *, core: object = None, **kw: Any) -> AuthzContext:
    return AuthzContext(subject, core=core, **kw)


def _entitlements(*, active: bool = True, balance: int = 0) -> Any:
    calls: list[tuple[str, UUID]] = []

    async def check_subscription_active(tt: str, tid: UUID) -> bool:
        calls.append((tt, tid))
        return active

    async def get_credit_balance(tt: str, tid: UUID) -> int:
        calls.append((tt, tid))
        return balance

    return SimpleNamespace(
        check_subscription_active=check_subscription_active,
        get_credit_balance=get_credit_balance,
        calls=calls,
    )


def _sub_fact(**over: object) -> AccessFact:
    base: dict[str, object] = {
        "name": "subscription.active",
        "owner_module": "billing",
        "claim_key": "subscription_status",
        "claim_rule": ClaimRule.one_of({"active", "trialing"}),
        "live_service": ENT,
        "live_resolver": lambda s, ctx: s.check_subscription_active(
            "user", ctx.subject.user_id
        ),
    }
    base.update(over)
    return AccessFact(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 1. Three-valued logic: Not / Or / And never turn an error into an allow
# --------------------------------------------------------------------------


async def test_not_over_unregistered_fact_denies_misconfigured() -> None:
    core = _StubCore()  # "account.suspended" NOT registered
    decision = await (~Entitled("account.suspended")).decide(
        _ctx(_subject(), core=core)
    )
    assert not decision.allowed
    assert decision.indeterminate
    assert decision.category == Category.POLICY_MISCONFIGURED
    # Not implemented in THIS deployment (no local fact, no bound owner).
    assert decision.http_status == 501
    assert decision.code == "fact_unregistered"


async def test_not_over_unavailable_fact_denies_501() -> None:
    core = _StubCore(services={})  # live service absent
    core.facts["account.suspended"] = _sub_fact(name="account.suspended")
    s = _subject()  # no provenance -> auto must go live -> unavailable
    decision = await Not(Entitled("account.suspended")).decide(_ctx(s, core=core))
    assert not decision.allowed and decision.indeterminate
    assert decision.http_status == 501


async def test_not_evaluate_raises_instead_of_inverting() -> None:
    core = _StubCore()
    with pytest.raises(PolicyIndeterminate):
        await Not(Entitled("nope")).evaluate(_ctx(_subject(), core=core))
    with pytest.raises(PolicyIndeterminate):
        await Entitled("nope").evaluate(_ctx(_subject(), core=core))


async def test_or_with_erroring_branch_does_not_allow() -> None:
    core = _StubCore()
    policy = Perm("x") | ~Entitled("account.suspended")
    decision = await policy.decide(_ctx(_subject(), core=core))
    assert not decision.allowed
    assert decision.indeterminate and decision.http_status == 501


async def test_or_genuine_branch_still_allows() -> None:
    core = _StubCore()
    policy = Role("admin") | Entitled("broken")
    decision = await policy.decide(_ctx(_subject(roles={"admin"}), core=core))
    assert decision.allowed


async def test_or_genuine_allow_wins_over_indeterminate_sibling_any_order() -> None:
    """Pinned semantics: ``Or`` allows when ANOTHER operand genuinely allows,
    even if an indeterminate sibling is evaluated first. The allow comes from
    the satisfied operand — the broken branch is never inverted or assumed
    true — so this is not a fail-open; with no genuine allow the ``Or`` stays
    indeterminate (see test_or_with_erroring_branch_does_not_allow)."""
    from sandloom.shared.authz.algebra import TIER_CLAIM, TIER_LIVE, Requirement
    from sandloom.shared.authz.results import AccessDecision

    class _Broken(Requirement):
        tier = TIER_CLAIM  # evaluated FIRST

        async def evaluate(self, ctx: AuthzContext) -> bool:
            return await self._evaluate_via_decide(ctx)

        async def decide(self, ctx: AuthzContext) -> AccessDecision:
            return AccessDecision.error(code="broken", http_status=500)

        def describe(self) -> str:
            return "broken"

    class _Grant(Requirement):
        tier = TIER_LIVE  # evaluated LAST

        def __init__(self, allow: bool) -> None:
            self.allow = allow

        async def evaluate(self, ctx: AuthzContext) -> bool:
            return await self._evaluate_via_decide(ctx)

        async def decide(self, ctx: AuthzContext) -> AccessDecision:
            if self.allow:
                return AccessDecision.allow()
            return self._deny()

        def describe(self) -> str:
            return "grant"

    ctx = _ctx(_subject(), core=_StubCore())
    for policy in (_Broken() | _Grant(True), _Grant(True) | _Broken()):
        decision = await policy.decide(ctx)
        assert decision.allowed and not decision.indeterminate
    denied = await (_Broken() | _Grant(False)).decide(ctx)
    assert not denied.allowed and denied.indeterminate
    assert denied.code == "broken" and denied.http_status == 500


async def test_and_propagates_indeterminate() -> None:
    core = _StubCore()
    policy = Role("admin") & ~Entitled("broken")
    decision = await policy.decide(_ctx(_subject(roles={"admin"}), core=core))
    assert decision.indeterminate and decision.category == Category.POLICY_MISCONFIGURED
    # And.evaluate must not silently answer False either
    with pytest.raises(PolicyIndeterminate):
        await policy.evaluate(_ctx(_subject(roles={"admin"}), core=core))


async def test_double_negation_of_error_still_denies() -> None:
    core = _StubCore()
    decision = await Not(Not(Entitled("broken"))).decide(_ctx(_subject(), core=core))
    assert not decision.allowed and decision.indeterminate


async def test_enforce_maps_indeterminate_to_access_denied_501() -> None:
    core = _StubCore()
    with pytest.raises(AccessDenied) as exc:
        await enforce(~Entitled("broken"), subject=_subject(), core=core, source="task")
    assert exc.value.status_code == 501
    assert exc.value.decision.indeterminate
    assert core.metrics.get("access_indeterminate_total") == 1


# --------------------------------------------------------------------------
# 2 + 5. Live atoms resolve via core.get_service (remote bindings), never raise
# --------------------------------------------------------------------------


def _facade_with_remote(service: object) -> tuple[CoreFacade, list[str]]:
    resolved: list[str] = []
    cfg = settings.model_copy(
        update={"service_bindings": {ENT: {"url": "http://billing.internal"}}}
    )
    core = CoreFacade(FastAPI(), settings_obj=cfg)

    def _resolver(name: str, binding: object, **_kw: object) -> object:
        resolved.append(name)
        return service

    core.set_service_resolver(_resolver)
    return core, resolved


async def test_credits_uses_remote_binding_proxy() -> None:
    svc = _entitlements(balance=100)
    core, resolved = _facade_with_remote(svc)
    decision = await Credits(10, of=Account.user()).decide(_ctx(_subject(), core=core))
    assert decision.allowed
    assert resolved == [ENT]


async def test_subscription_active_uses_remote_binding_proxy() -> None:
    svc = _entitlements(active=True)
    core, _ = _facade_with_remote(svc)
    assert (
        await SubscriptionActive(of=Account.user()).decide(_ctx(_subject(), core=core))
    ).allowed


async def test_live_fact_uses_remote_binding_proxy() -> None:
    svc = _entitlements(active=True)
    core, resolved = _facade_with_remote(svc)
    fact = _sub_fact()
    assert await resolve_fact(fact, _ctx(_subject(), core=core), mode="live") is True
    assert resolved == [ENT]


async def test_live_atom_without_provider_is_501_decision_not_http_exception() -> None:
    core = CoreFacade(FastAPI())  # no local provider, no binding
    for atom in (Credits(1, of=Account.user()), SubscriptionActive(of=Account.user())):
        decision = await atom.decide(_ctx(_subject(), core=core, source="task"))
        assert not decision.allowed and decision.indeterminate
        assert decision.http_status == 501
        assert decision.code == "service_unavailable"
    # No core bound at all (worker without core=) -> same typed decision.
    decision = await Credits(1, of=Account.user()).decide(
        _ctx(_subject(), source="task")
    )
    assert decision.http_status == 501
    # The primitive evaluate raises a typed error, never HTTPException.
    with pytest.raises(PolicyIndeterminate) as exc:
        await Credits(1, of=Account.user()).evaluate(_ctx(_subject(), source="task"))
    assert not isinstance(exc.value, HTTPException)


async def test_not_over_missing_live_service_denies() -> None:
    decision = await (~Credits(1, of=Account.user())).decide(
        _ctx(_subject(), core=_StubCore())
    )
    assert not decision.allowed and decision.http_status == 501


# --------------------------------------------------------------------------
# 3 + 4. HTTP guards use the decision status; AccessDenied is an HTTPException
# --------------------------------------------------------------------------


def _token(**claims: object) -> str:
    data: dict[str, object] = {"sub": str(USER), "token_type": "access"}
    data.update(claims)
    return create_access_token(data=data)


def _app(core: object) -> tuple[FastAPI, list[str]]:
    app = FastAPI()
    app.state.core = core
    register_error_handlers(app)
    db_opened: list[str] = []

    async def _fake_db() -> Any:
        db_opened.append("db")
        yield None

    app.dependency_overrides[get_db] = _fake_db

    @app.get("/sub", dependencies=[Depends(require(Subscribed(of=Account.user())))])
    async def _sub() -> dict[str, str]:
        return {"ok": "sub"}

    @app.get("/fact", dependencies=[Depends(require(Entitled("nope")))])
    async def _fact() -> dict[str, str]:
        return {"ok": "fact"}

    @app.get(
        "/credits", dependencies=[Depends(require(Credits(50, of=Account.user())))]
    )
    async def _credits() -> dict[str, str]:
        return {"ok": "credits"}

    @app.get(
        "/credits-override",
        dependencies=[
            Depends(require(Credits(50, of=Account.user()), status_code=409))
        ],
    )
    async def _credits_override() -> dict[str, str]:
        return {"ok": "credits"}

    @app.get("/role", dependencies=[Depends(require(Role("admin")))])
    async def _role() -> dict[str, str]:
        return {"ok": "role"}

    @app.get("/enforce")
    async def _enforce() -> dict[str, str]:
        await enforce(Subscribed(of=Account.user()), subject=_subject(), core=core)
        return {"ok": "enforce"}

    return app, db_opened


async def _get(app: FastAPI, path: str, token: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path, headers={"Authorization": f"Bearer {token}"})


async def test_http_subscribed_denial_is_402() -> None:
    app, _ = _app(_StubCore())
    resp = await _get(app, "/sub", _token())
    assert resp.status_code == 402
    assert resp.json()["code"] == "Subscribed"
    assert "active subscription" in resp.json()["detail"]


async def test_http_unregistered_fact_is_501_not_403() -> None:
    app, _ = _app(_StubCore())
    resp = await _get(app, "/fact", _token())
    assert resp.status_code == 501


async def test_http_missing_contract_is_501() -> None:
    app, _ = _app(_StubCore(services={}))
    resp = await _get(app, "/credits", _token())
    assert resp.status_code == 501
    assert resp.json()["code"] == "service_unavailable"


async def test_http_credits_denial_is_402_with_reason_detail() -> None:
    app, db_opened = _app(_StubCore(services={ENT: _entitlements(balance=0)}))
    resp = await _get(app, "/credits", _token())
    assert resp.status_code == 402
    assert resp.json()["detail"] == "requires at least 50 credits"
    assert db_opened == ["db"]  # live policy gets the request session


async def test_http_status_override_only_for_definite_denials() -> None:
    app, _ = _app(_StubCore(services={ENT: _entitlements(balance=0)}))
    assert (await _get(app, "/credits-override", _token())).status_code == 409
    app2, _ = _app(_StubCore(services={}))
    # An indeterminate 501 is never relabelled by the override.
    assert (await _get(app2, "/credits-override", _token())).status_code == 501


async def test_http_claims_only_policy_opens_no_db_session() -> None:
    app, db_opened = _app(_StubCore())
    resp = await _get(app, "/role", _token(roles=["admin"]))
    assert resp.status_code == 200
    resp = await _get(app, "/sub", _token())
    assert resp.status_code == 402
    assert db_opened == []


async def test_http_enforce_in_endpoint_maps_status() -> None:
    app, _ = _app(_StubCore())
    resp = await _get(app, "/enforce", _token())
    assert resp.status_code == 402
    assert resp.json()["code"] == "Subscribed"


async def test_access_denied_is_http_exception_and_picklable() -> None:
    decision = AccessDecision.deny(code="x", category=Category.PAYMENT_REQUIRED)
    exc = AccessDenied(decision)
    assert isinstance(exc, HTTPException)
    assert exc.status_code == exc.http_status == 402
    assert exc.code == "x" and exc.detail
    clone = pickle.loads(pickle.dumps(exc))  # noqa: S301 - round-trips our own exception
    assert isinstance(clone, AccessDenied)
    assert clone.decision == decision and clone.status_code == 402


async def test_token_without_iat_is_rejected() -> None:
    app, _ = _app(_StubCore())
    exp = datetime.now(UTC) + timedelta(minutes=5)
    no_iat = jwt.encode(
        {"sub": str(USER), "token_type": "access", "exp": exp, "roles": ["admin"]},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    resp = await _get(app, "/role", no_iat)
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# 6. Fact cache keyed by forced mode
# --------------------------------------------------------------------------


async def test_fact_cache_does_not_serve_claim_answer_to_forced_live() -> None:
    svc = _entitlements(active=False)
    core = _StubCore(services={ENT: svc})
    fact = _sub_fact()
    ctx = _ctx(
        _subject(subscription_status="active", claims_sources={"billing"}), core=core
    )
    assert await resolve_fact(fact, ctx, mode="claim") is True
    assert await resolve_fact(fact, ctx, mode="live") is False
    assert len(svc.calls) == 1


# --------------------------------------------------------------------------
# 7. Empty / missing provenance is untrusted
# --------------------------------------------------------------------------


async def test_empty_provenance_is_untrusted_in_claim_mode() -> None:
    fact = _sub_fact()
    s = _subject(subscription_status="active")  # no claims_sources at all
    with pytest.raises(FactUnavailable):
        await resolve_fact(fact, _ctx(s), mode="claim")


async def test_empty_provenance_auto_falls_back_to_live() -> None:
    svc = _entitlements(active=False)
    core = _StubCore(services={ENT: svc})
    s = _subject(subscription_status="active", claims_sources=[])
    assert await resolve_fact(_sub_fact(), _ctx(s, core=core), mode="auto") is False
    assert svc.calls  # went live instead of trusting the unvouched claim


async def test_mapping_provenance_trusts_only_successful_sources() -> None:
    s = UserContext(
        user_id=USER,
        subscription_status="active",
        claims_sources={"billing": "failed", "identity": "ok"},
    )
    assert s.claims_sources == {"identity"}
    with pytest.raises(FactUnavailable):
        await resolve_fact(_sub_fact(), _ctx(s), mode="claim")
    ok = UserContext(user_id=USER, claims_sources={"billing": True})
    assert ok.claims_sources == {"billing"}


# --------------------------------------------------------------------------
# 8. Module-agnostic UserContext + pluggable adapters + org tenant id
# --------------------------------------------------------------------------


async def test_user_context_namespaces_module_claims() -> None:
    payload = {
        "sub": str(USER),
        "roles": ["admin"],
        "plan_code": "pro",
        "subscription_status": "active",
        "features": {"export": True},
        "limits": {"seats": 3},
        "tenant_ids": ["a"],
    }
    s = UserContext(**payload)
    assert s.claim("plan_code") == "pro"
    assert s.claims["tenant_ids"] == ["a"]
    # module claims passed as top-level kwargs land in the claims namespace
    assert s.claim("subscription_status") == "active"
    assert s.claim("features") == {"export": True}
    assert s.claim("limits") == {"seats": 3}
    for removed in ("plan_code", "subscription_status", "features", "limits"):
        assert not hasattr(s, removed)
    assert await Feature("export", of=Account.user()).evaluate(_ctx(s))
    assert await WithinLimit("seats", 2, of=Account.user()).evaluate(_ctx(s))
    assert not await WithinLimit("seats", 3, of=Account.user()).evaluate(_ctx(s))


async def test_claim_atoms_accept_custom_claim_key() -> None:
    s = _subject(shop_status="active")
    assert await Subscribed(of=Account.user(), claim_key="shop_status").evaluate(
        _ctx(s)
    )


async def test_registered_adapter_overrides_contract() -> None:
    seen: list[str] = []

    class _Adapter:
        service = "shop.wallet.v1"

        async def subscription_active(
            self, service: Any, *, tenant_type: str, tenant_id: UUID, ctx: Any
        ) -> bool:
            return True

        async def credit_balance(
            self, service: Any, *, tenant_type: str, tenant_id: UUID, ctx: Any
        ) -> int:
            seen.append(service)
            return 7

    core = _StubCore(services={"shop.wallet.v1": "wallet-svc"})
    try:
        register_live_adapter(ENTITLEMENTS, _Adapter())
        assert (
            await Credits(5, of=Account.user()).decide(_ctx(_subject(), core=core))
        ).allowed
        assert seen == ["wallet-svc"]
    finally:
        reset_live_adapters()
    with pytest.raises(TypeError):
        register_live_adapter(ENTITLEMENTS, object())


async def test_provider_side_live_adapter_registration_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Adapters are consumer-side: a provider module registering one for its
    own contract only affects ITS process — in a split the consumer silently
    keeps the default. That must not pass unnoticed."""
    from sandloom.shared.authz.live_contracts import DefaultEntitlementsAdapter

    adapter = DefaultEntitlementsAdapter()  # service = billing.entitlements.v1
    provider_globals = {
        "__name__": "myapp.modules.billing",
        "register_live_adapter": register_live_adapter,
        "ENTITLEMENTS": ENTITLEMENTS,
        "adapter": adapter,
    }
    consumer_globals = {**provider_globals, "__name__": "myapp.modules.shop"}
    try:
        with caplog.at_level("WARNING", logger="sandloom.access.live_contracts"):
            exec("register_live_adapter(ENTITLEMENTS, adapter)", consumer_globals)  # noqa: S102 - literal code run under a chosen module name
            assert not caplog.records
            exec("register_live_adapter(ENTITLEMENTS, adapter)", provider_globals)  # noqa: S102 - literal code run under a chosen module name
        assert any("consumer-side" in r.getMessage() for r in caplog.records)
    finally:
        reset_live_adapters()


async def test_org_credits_use_org_from_path_never_user_id() -> None:
    org = uuid4()
    svc = _entitlements(balance=100)
    core = _StubCore(services={ENT: svc})
    request = SimpleNamespace(path_params={"org_id": str(org)}, headers={}, app=None)
    s = _subject(tenant_ids=[str(org)])
    atom = Credits(10, of=Account.org())
    assert (await atom.decide(_ctx(s, core=core, request=request))).allowed
    assert svc.calls == [("organization", org)]
    assert USER not in {tid for _, tid in svc.calls}


async def test_org_subscription_from_tenant_header_validated() -> None:
    org = uuid4()
    svc = _entitlements(active=True)
    core = _StubCore(services={ENT: svc})
    request = SimpleNamespace(
        path_params={}, headers={"X-Tenant-Id": str(org)}, app=None
    )
    atom = SubscriptionActive(of=Account.org())
    ok = await atom.decide(
        _ctx(_subject(tenant_ids=[str(org)]), core=core, request=request)
    )
    assert ok.allowed and svc.calls == [("organization", org)]
    # Org not granted by the token -> indeterminate 403, service never called.
    svc.calls.clear()
    denied = await atom.decide(_ctx(_subject(), core=core, request=request))
    assert not denied.allowed and denied.indeterminate
    assert denied.http_status == 403 and svc.calls == []


async def test_org_credits_use_bound_tenant() -> None:
    org = uuid4()
    svc = _entitlements(balance=100)
    core = _StubCore(services={ENT: svc})
    with bind_tenant(org):
        decision = await Credits(10, of=Account.org()).decide(
            _ctx(_subject(), core=core, source="task")
        )
    assert decision.allowed and svc.calls == [("organization", org)]


async def test_org_credits_without_org_context_is_indeterminate() -> None:
    svc = _entitlements(balance=100)
    core = _StubCore(services={ENT: svc})
    atom = Credits(10, of=Account.org())
    decision = await atom.decide(_ctx(_subject(), core=core))
    assert decision.indeterminate and decision.http_status == 400
    # and the negation cannot turn it into an allow
    assert not (await (~atom).decide(_ctx(_subject(), core=core))).allowed
    assert svc.calls == []


async def test_org_role_missing_org_cannot_be_negated_into_allow() -> None:
    core = _StubCore(services={"tenancy.membership.v1": SimpleNamespace()})
    request = SimpleNamespace(path_params={}, headers={}, app=None)
    decision = await (~OrgRole("admin")).decide(
        _ctx(_subject(), core=core, request=request)
    )
    assert not decision.allowed and decision.indeterminate


# --------------------------------------------------------------------------
# 9. Perm / Role keep ``code`` as the atom's denial code
# --------------------------------------------------------------------------


async def test_perm_role_denial_code_is_class_code() -> None:
    perm, role = Perm("p.write"), Role("ops")
    assert perm.perm == "p.write" and role.role == "ops"
    assert (await perm.decide(_ctx(_subject()))).code == "Perm"
    assert (await role.decide(_ctx(_subject()))).code == "Role"


# --------------------------------------------------------------------------
# 11. Atomic metric increments
# --------------------------------------------------------------------------


async def test_metric_increment_prefers_counter_api() -> None:
    calls: list[tuple[str, int]] = []
    core = SimpleNamespace(increment_metric=lambda n, a: calls.append((n, a)))
    increment(core, "access_denied_total")
    assert calls == [("access_denied_total", 1)]


async def test_metric_increment_is_thread_safe() -> None:
    class _SlowCore(_StubCore):
        def get_metric(self, name: str) -> object:
            value = super().get_metric(name)
            threading.Event().wait(0.0005)  # widen the read-modify-write window
            return value

    core = _SlowCore()
    threads = [
        threading.Thread(target=lambda: [increment(core, "m") for _ in range(20)])
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert core.metrics["m"] == 160
