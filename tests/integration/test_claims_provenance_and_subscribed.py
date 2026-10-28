"""Consumer side of claims provenance (C2b) and ``Subscribed`` semantics (X3)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from sandloom.shared.authz import AccessFact, Account
from sandloom.shared.authz.atoms_claims import Subscribed
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.facts import ClaimRule, FactUnavailable, resolve_fact
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

USER = uuid4()


def _subject(**claims: Any) -> UserContext:
    return UserContext.model_validate({"sub": str(USER), **claims})


class _Core:
    def __init__(self, facts: dict[str, Any] | None = None, services: Any = None):
        self._facts = facts or {}
        self._services = services or {}

    def get_access_fact(self, name: str) -> Any:
        return self._facts.get(name)

    def get_service(self, name: str) -> Any:
        from sandloom.core.di import ServiceUnavailableInDeployment

        if name not in self._services:
            raise ServiceUnavailableInDeployment(name)
        return self._services[name]


def _fact(live_value: bool = False) -> AccessFact:
    async def live(_svc: Any, _ctx: Any) -> bool:
        return live_value

    return AccessFact(
        name="subscription.active",
        owner_module="billing",
        claim_key="subscription_status",
        claim_rule=ClaimRule.one_of({"active"}),
        live_service="billing.entitlements.v1",
        live_resolver=live,
    )


# -- UserContext carries provenance -------------------------------------------


async def test_user_context_carries_claims_sources_failed() -> None:
    s = _subject(claims_sources=["identity"], claims_sources_failed=["billing"])
    assert s.claims_sources == {"identity"}
    assert s.claims_sources_failed == {"billing"}
    assert s.provenance_authoritative is True
    # Not leaked into the namespaced claims mapping.
    assert "claims_sources_failed" not in s.claims


async def test_legacy_token_has_no_authoritative_provenance() -> None:
    s = _subject(claims_sources=["billing"])
    assert s.provenance_authoritative is False
    assert s.claims_sources_failed == set()


# -- _claim_trustworthy --------------------------------------------------------


async def test_authoritative_provenance_trusts_only_listed_owner() -> None:
    core = _Core(services={"billing.entitlements.v1": object()})
    fact = _fact(live_value=False)
    # Billing FAILED at mint: its (absent) claim is not read as "false" —
    # claim mode refuses, auto escalates to live.
    s = _subject(
        subscription_status="active",
        claims_sources=[],
        claims_sources_failed=["billing"],
    )
    with pytest.raises(FactUnavailable):
        await resolve_fact(fact, AuthzContext(s, core=core), mode="claim")
    assert await resolve_fact(fact, AuthzContext(s, core=core), mode="auto") is False

    # Authoritative and listed -> the claim is trusted (no live call needed).
    ok = _subject(
        subscription_status="active",
        claims_sources=["billing"],
        claims_sources_failed=[],
    )
    assert await resolve_fact(fact, AuthzContext(ok, core=_Core()), mode="claim")


async def test_owner_in_both_lists_is_not_trusted() -> None:
    s = _subject(
        subscription_status="active",
        claims_sources=["billing"],
        claims_sources_failed=["billing"],
    )
    with pytest.raises(FactUnavailable):
        await resolve_fact(_fact(), AuthzContext(s, core=_Core()), mode="claim")


async def test_legacy_token_keeps_listed_owner_behaviour() -> None:
    s = _subject(subscription_status="active", claims_sources=["billing"])
    assert await resolve_fact(_fact(), AuthzContext(s, core=_Core()), mode="claim")
    unlisted = _subject(subscription_status="active")
    with pytest.raises(FactUnavailable):
        await resolve_fact(_fact(), AuthzContext(unlisted, core=_Core()), mode="claim")


# -- Subscribed(of=Account.user()) agrees with the live check (X3) ------------------------------


@pytest.mark.parametrize(
    ("status", "deny_past_due", "expected"),
    [
        ("active", False, True),
        ("trialing", False, True),
        ("past_due", False, True),  # grace period
        ("past_due", True, False),  # BILLING_DENY_ON_PAST_DUE
        ("canceled", False, False),
        (None, False, False),
    ],
)
async def test_subscribed_honours_past_due_grace(
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
    deny_past_due: bool,
    expected: bool,
) -> None:
    from sandloom.core.di import settings
    from sandloom_billing.access import SUBSCRIPTION_ACTIVE_FACT

    monkeypatch.setattr(settings, "billing_deny_on_past_due", deny_past_due)
    claims = {"subscription_status": status} if status else {}
    # The grace rule is billing's (the registered fact), not shared's.
    core = _Core(facts={"subscription.active": SUBSCRIPTION_ACTIVE_FACT})
    ctx = AuthzContext(_subject(**claims), core=core)
    assert await Subscribed(of=Account.user()).evaluate(ctx) is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [("active", True), ("trialing", True), ("past_due", False), (None, False)],
)
async def test_subscribed_without_any_subscription_owner_is_strict(
    status: str | None, expected: bool
) -> None:
    claims = {"subscription_status": status} if status else {}
    ctx = AuthzContext(_subject(**claims), core=_Core())
    assert await Subscribed(of=Account.user()).evaluate(ctx) is expected


async def test_subscribed_escalates_to_live_when_billing_failed_at_mint() -> None:
    """Billing in ``claims_sources_failed``: the (absent) claim is not a
    genuine "unsubscribed" — Subscribed asks billing live instead of denying
    402 for the token's whole lifetime."""
    calls: list[tuple[str, UUID]] = []

    async def check_subscription_active(tt: str, tid: UUID) -> bool:
        calls.append((tt, tid))
        return True

    from sandloom_billing.access import SUBSCRIPTION_ACTIVE_FACT

    svc = SimpleNamespace(check_subscription_active=check_subscription_active)
    core = _Core(
        facts={"subscription.active": SUBSCRIPTION_ACTIVE_FACT},
        services={"billing.entitlements.v1": svc},
    )
    subject = _subject(claims_sources=["identity"], claims_sources_failed=["billing"])
    assert (
        await Subscribed(of=Account.user()).evaluate(AuthzContext(subject, core=core))
        is True
    )
    assert calls == [("user", USER)]


async def test_subscribed_without_live_path_is_indeterminate_503() -> None:
    from sandloom.shared.authz.results import PolicyIndeterminate
    from sandloom_billing.access import SUBSCRIPTION_ACTIVE_FACT

    core = _Core(facts={"subscription.active": SUBSCRIPTION_ACTIVE_FACT})
    subject = _subject(claims_sources=["identity"], claims_sources_failed=["billing"])
    ctx = AuthzContext(subject, core=core)
    with pytest.raises(PolicyIndeterminate) as exc:
        await Subscribed(of=Account.user()).evaluate(ctx)
    assert exc.value.decision.http_status == 503
    decision = await Subscribed(of=Account.user()).decide(
        AuthzContext(subject, core=core)
    )
    assert not decision.allowed and decision.http_status == 503


async def test_subscribed_uses_registered_fact_interpretation() -> None:
    fact = _fact()  # interprets ONLY "active" as True
    ctx = AuthzContext(
        _subject(subscription_status="trialing"),
        core=_Core(facts={"subscription.active": fact}),
    )
    assert await Subscribed(of=Account.user()).evaluate(ctx) is False


async def test_subscribed_explicit_statuses_are_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sandloom.core.di import settings

    monkeypatch.setattr(settings, "billing_deny_on_past_due", False)
    ctx = AuthzContext(_subject(subscription_status="past_due"), core=_Core())
    assert (
        await Subscribed(frozenset({"active"}), of=Account.user()).evaluate(ctx)
        is False
    )


async def test_subscribed_organization_resolves_live() -> None:
    from sandloom.shared.authz.algebra import TIER_LIVE

    org_id = uuid4()
    calls: list[tuple[str, UUID]] = []

    async def check_subscription_active(tt: str, tid: UUID) -> bool:
        calls.append((tt, tid))
        return True

    svc = SimpleNamespace(check_subscription_active=check_subscription_active)
    core = _Core(services={"billing.entitlements.v1": svc})
    request = SimpleNamespace(path_params={"org_id": str(org_id)}, headers={})
    subject = _subject(tenant_ids=[str(org_id)])
    atom = Subscribed(of=Account.org())
    assert atom.tier == TIER_LIVE
    assert await atom.evaluate(AuthzContext(subject, core=core, request=request))
    assert calls == [("organization", org_id)]
