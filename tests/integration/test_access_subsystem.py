"""Unit tests for the unified access subsystem (pure, no DB).

Covers the structured decision model, the ``decide`` propagation through the
algebra, the ``enforce`` chokepoint + deny observability, configurable fact
resolution (claim/projection/live/auto + freshness floor + provenance), the
``Entitled`` / ``OrgRole`` atoms, and mint-time claim provenance stamping.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from sandloom.shared.authz import (
    AccessDecision,
    AccessDenied,
    AccessFact,
    Account,
    Category,
    Entitled,
    FactUnavailable,
    Not,
    OrgRole,
    Perm,
    Role,
    Subscribed,
    enforce,
    resolve_fact,
    system_subject,
)
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.facts import ClaimRule
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

USER = uuid4()


def _subject(**overrides: object) -> UserContext:
    data: dict[str, object] = {"user_id": USER}
    data.update(overrides)
    return UserContext(**data)


class _StubCore:
    """Minimal CoreFacade stand-in for atoms/facts/enforce.

    ``get_service`` mirrors the real facade: local provider, else
    ``ServiceUnavailableInDeployment`` (a 501 HTTPException).
    """

    def __init__(self, *, services: dict[str, object] | None = None) -> None:
        self._services = services or {}
        self.metrics: dict[str, int] = {}
        self.facts: dict[str, AccessFact] = {}

    def get_service(self, name: str) -> object:
        from sandloom.core.module_api._service_registry import (
            ServiceUnavailableInDeployment,
        )

        if name not in self._services:
            raise ServiceUnavailableInDeployment(name)
        return self._services[name]

    def get_metric(self, name: str) -> object:
        return self.metrics.get(name)

    def register_metric(self, name: str, value: object) -> None:
        self.metrics[name] = int(value)  # type: ignore[arg-type]

    def get_access_fact(self, name: str) -> AccessFact | None:
        return self.facts.get(name)


def _ctx(subject: UserContext, *, core: object = None, **kw: object) -> AuthzContext:
    return AuthzContext(subject, core=core, **kw)


# --------------------------------------------------------------------------
# Structured decision + decide() propagation
# --------------------------------------------------------------------------


async def test_decide_allow_and_deny_categories() -> None:
    s = _subject(permissions={"p.write"})
    allow = await Perm("p.write").decide(_ctx(s))
    assert allow.allowed and allow.http_status == 200

    deny = await Perm("p.delete").decide(_ctx(s))
    assert not deny.allowed
    assert deny.category == Category.FORBIDDEN
    assert deny.http_status == 403
    assert deny.failed_requirement == "permission 'p.delete'"


async def test_subscribed_denies_with_payment_required() -> None:
    deny = await Subscribed(of=Account.user()).decide(_ctx(_subject()))
    assert deny.category == Category.PAYMENT_REQUIRED
    assert deny.http_status == 402


async def test_and_returns_first_failing_cheapest_first() -> None:
    s = _subject(roles={"admin"})
    # Subscribed (claim, fails) is cheaper than nothing else; it should decide.
    decision = await (Subscribed(of=Account.user()) & Role("admin")).decide(_ctx(s))
    assert not decision.allowed
    assert decision.category == Category.PAYMENT_REQUIRED


async def test_or_allows_if_any_holds_else_combines() -> None:
    s = _subject(roles={"admin"})
    assert (await (Role("admin") | Perm("x")).decide(_ctx(s))).allowed

    s2 = _subject()
    deny = await (Role("admin") | Perm("x")).decide(_ctx(s2))
    assert not deny.allowed
    assert deny.code == "any_of_failed"


async def test_not_inverts() -> None:
    s = _subject(roles={"admin"})
    assert not (await Not(Role("admin")).decide(_ctx(s))).allowed
    assert (await Not(Role("support")).decide(_ctx(s))).allowed


# --------------------------------------------------------------------------
# enforce() chokepoint
# --------------------------------------------------------------------------


async def test_enforce_allows_and_returns_decision() -> None:
    s = _subject(roles={"admin"})
    decision = await enforce(Role("admin"), subject=s, policy_code="demo")
    assert decision.allowed and decision.policy_code == "demo"


async def test_enforce_denies_and_records_metric() -> None:
    core = _StubCore()
    s = _subject()
    with pytest.raises(AccessDenied) as exc:
        await enforce(Role("admin"), subject=s, core=core, source="task")
    assert exc.value.decision.category == Category.FORBIDDEN
    assert core.metrics.get("access_denied_total") == 1
    assert core.metrics.get("access_denied_forbidden_total") == 1


async def test_system_subject_has_system_role() -> None:
    assert "system" in system_subject().roles


# --------------------------------------------------------------------------
# Configurable fact resolution
# --------------------------------------------------------------------------


def _sub_fact(**over: object) -> AccessFact:
    base: dict[str, object] = {
        "name": "subscription.active",
        "owner_module": "billing",
        "claim_key": "subscription_status",
        "claim_rule": ClaimRule.one_of({"active", "trialing"}),
        "live_service": "billing.entitlements.v1",
    }
    base.update(over)
    return AccessFact(**base)  # type: ignore[arg-type]


async def test_claim_mode_trusts_only_with_provenance() -> None:
    fact = _sub_fact()
    # provenance present + active -> True
    s = _subject(subscription_status="active", claims_sources={"billing"})
    assert await resolve_fact(fact, _ctx(s), mode="claim") is True
    # provenance present + inactive -> legitimate False
    s2 = _subject(subscription_status=None, claims_sources={"billing"})
    assert await resolve_fact(fact, _ctx(s2), mode="claim") is False
    # provenance ABSENT -> claim mode refuses (would silently strip the fact)
    s3 = _subject(subscription_status="active", claims_sources={"identity"})
    with pytest.raises(FactUnavailable):
        await resolve_fact(fact, _ctx(s3), mode="claim")


async def test_live_mode_calls_service() -> None:
    async def _check(tenant_type: str, uid: UUID) -> bool:
        return True

    svc = SimpleNamespace(check_subscription_active=_check)
    fact = _sub_fact(
        live_resolver=lambda s, ctx: s.check_subscription_active(
            "user", ctx.subject.user_id
        )
    )
    core = _StubCore(services={"billing.entitlements.v1": svc})
    s = _subject()
    assert await resolve_fact(fact, _ctx(s, core=core), mode="live") is True


async def test_live_mode_unconfigured_raises() -> None:
    fact = _sub_fact(live_resolver=lambda s, ctx: True)  # type: ignore[arg-type,return-value]
    core = _StubCore(services={})  # service not registered
    with pytest.raises(FactUnavailable):
        await resolve_fact(fact, _ctx(_subject(), core=core), mode="live")


async def test_projection_mode_reads_local() -> None:
    async def reader(ctx: AuthzContext) -> bool:
        return True

    fact = _sub_fact(projection_reader=reader)
    assert await resolve_fact(fact, _ctx(_subject()), mode="projection") is True


async def test_auto_prefers_claim_then_falls_back_to_live() -> None:
    async def _check(tenant_type: str, uid: UUID) -> bool:
        return True

    svc = SimpleNamespace(check_subscription_active=_check)
    fact = _sub_fact(
        live_resolver=lambda s, ctx: s.check_subscription_active(
            "user", ctx.subject.user_id
        )
    )
    core = _StubCore(services={"billing.entitlements.v1": svc})

    # claim present -> resolves from claim, no fallback metric
    s = _subject(subscription_status="active", claims_sources={"billing"})
    assert await resolve_fact(fact, _ctx(s, core=core), mode="auto") is True
    assert core.metrics.get("access_fact_live_fallback_total") is None

    # claim namespace absent -> auto falls back to live (and counts it)
    s2 = _subject(claims_sources={"identity"})
    assert await resolve_fact(fact, _ctx(s2, core=core), mode="auto") is True
    assert core.metrics.get("access_fact_live_fallback_total") == 1


async def test_floor_blocks_downgrade_to_claim() -> None:
    # A money fact pins min_freshness=live; even an explicit claim mode is
    # clamped (auto would skip claim too). Here mode='claim' < floor is refused
    # because the floor forbids it: claim is below 'live'.
    async def _check(tenant_type: str, uid: UUID) -> bool:
        return False

    svc = SimpleNamespace(check_subscription_active=_check)
    fact = _sub_fact(
        min_freshness="live",
        live_resolver=lambda s, ctx: s.check_subscription_active(
            "user", ctx.subject.user_id
        ),
    )
    core = _StubCore(services={"billing.entitlements.v1": svc})
    # auto must NOT read the (present) claim; it must go live and get False.
    s = _subject(subscription_status="active", claims_sources={"billing"})
    assert await resolve_fact(fact, _ctx(s, core=core), mode="auto") is False


async def test_fact_rejects_bad_freshness() -> None:
    with pytest.raises(ValueError):
        AccessFact(name="x", owner_module="m", min_freshness="bogus")


# --------------------------------------------------------------------------
# Entitled atom
# --------------------------------------------------------------------------


async def test_entitled_unregistered_is_misconfigured() -> None:
    core = _StubCore()
    decision = await Entitled("nope").decide(_ctx(_subject(), core=core))
    assert decision.category == Category.POLICY_MISCONFIGURED
    assert decision.code == "fact_unregistered"


async def test_entitled_satisfied_and_denied() -> None:
    core = _StubCore()
    core.facts["subscription.active"] = _sub_fact()
    s_ok = _subject(subscription_status="active", claims_sources={"billing"})
    assert (
        await Entitled(
            "subscription.active", category=Category.PAYMENT_REQUIRED, mode="claim"
        ).decide(_ctx(s_ok, core=core))
    ).allowed

    s_no = _subject(subscription_status=None, claims_sources={"billing"})
    deny = await Entitled(
        "subscription.active", category=Category.PAYMENT_REQUIRED, mode="claim"
    ).decide(_ctx(s_no, core=core))
    assert not deny.allowed and deny.category == Category.PAYMENT_REQUIRED


async def test_entitled_uses_fact_deny_category() -> None:
    """A fact declares its denial category; Entitled(category=) overrides it;
    a resolver's FactDenied picks its own (definite) category."""
    from sandloom.shared.authz import FactDenied

    core = _StubCore()
    core.facts["subscription.active"] = _sub_fact(
        deny_category=Category.PAYMENT_REQUIRED
    )
    s_no = _subject(subscription_status=None, claims_sources={"billing"})
    deny = await Entitled("subscription.active", mode="claim").decide(
        _ctx(s_no, core=core)
    )
    assert deny.category == Category.PAYMENT_REQUIRED and deny.http_status == 402
    override = await Entitled(
        "subscription.active", category=Category.FORBIDDEN, mode="claim"
    ).decide(_ctx(s_no, core=core))
    assert override.http_status == 403

    # No deny_category -> forbidden (403), the historical default.
    core.facts["plain"] = _sub_fact(name="plain")
    plain = await Entitled("plain", mode="claim").decide(_ctx(s_no, core=core))
    assert plain.category == Category.FORBIDDEN and plain.http_status == 403

    async def _refuse(service: object, ctx: object) -> bool:
        raise FactDenied("not a member", code="not_a_member")

    core = _StubCore(services={"billing.entitlements.v1": object()})
    core.facts["org.fact"] = _sub_fact(
        name="org.fact",
        live_resolver=_refuse,
        deny_category=Category.PAYMENT_REQUIRED,
        min_freshness="live",
    )
    refused = await Entitled("org.fact").decide(_ctx(_subject(), core=core))
    assert not refused.allowed and not refused.indeterminate
    assert refused.code == "not_a_member" and refused.http_status == 403


async def test_entitled_unavailable_is_501() -> None:
    core = _StubCore(services={})
    core.facts["subscription.active"] = _sub_fact(
        live_resolver=lambda s, ctx: True  # type: ignore[arg-type,return-value]
    )
    # claim namespace absent + service unconfigured -> auto exhausts -> 501
    s = _subject(claims_sources={"identity"})
    deny = await Entitled("subscription.active").decide(_ctx(s, core=core))
    assert deny.http_status == 501
    assert deny.code == "fact_unavailable"


# --------------------------------------------------------------------------
# OrgRole (relationship / ReBAC) atom
# --------------------------------------------------------------------------


async def test_org_role_resolves_membership_service() -> None:
    org_id = uuid4()

    async def has_role_at_least(oid: UUID, uid: UUID, minimum: str) -> bool:
        return oid == org_id and minimum == "admin"

    svc = SimpleNamespace(has_role_at_least=has_role_at_least)
    core = _StubCore(services={"tenancy.membership.v1": svc})
    request = SimpleNamespace(path_params={"org_id": str(org_id)}, app=None)
    s = _subject()

    assert (await OrgRole("admin").decide(_ctx(s, core=core, request=request))).allowed
    assert not (
        await OrgRole("owner").decide(_ctx(s, core=core, request=request))
    ).allowed


async def test_org_role_missing_path_param_denies() -> None:
    core = _StubCore(services={"tenancy.membership.v1": SimpleNamespace()})
    request = SimpleNamespace(path_params={}, app=None)
    assert not (
        await OrgRole("member").decide(_ctx(_subject(), core=core, request=request))
    ).allowed


# --------------------------------------------------------------------------
# UserContext provenance coercion
# --------------------------------------------------------------------------


async def test_user_context_coerces_claims_sources_list_to_set() -> None:
    ctx = UserContext(user_id=USER, claims_sources=["billing", "identity"])
    assert ctx.claims_sources == {"billing", "identity"}


async def test_access_decision_payment_required_status() -> None:
    d = AccessDecision.deny(code="x", category=Category.PAYMENT_REQUIRED)
    assert d.http_status == 402
