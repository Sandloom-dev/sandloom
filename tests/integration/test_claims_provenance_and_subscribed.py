"""Consumer side of claims provenance (C2b) and ``Subscribed`` semantics (X3).

``Subscribed`` is billing's generated atom (``generated_atoms``, KB-34): it
reads the claim under billing's registered ``subscription.active`` fact, and
with billing neither loaded nor bound it is indeterminate 501.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from sandloom.sdk.testing import override_settings
from sandloom.shared.authz import AccessFact, Account
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.authz.facts import ClaimRule, FactUnavailable, resolve_fact
from sandloom.shared.subject import UserContext
from sandloom_billing.config import BillingSettings

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


@pytest.fixture
def billing_config() -> Iterator[Callable[..., None]]:
    """``billing_config(billing_deny_on_past_due=True)``: billing's declared
    settings (ADR-0015) for the rest of the test; calls nest."""
    with ExitStack() as stack:

        def _set(**values: Any) -> None:
            stack.enter_context(override_settings(BillingSettings, **values))

        yield _set


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
    billing_config: Callable[..., None],
    generated_atoms: SimpleNamespace,
    status: str | None,
    deny_past_due: bool,
    expected: bool,
) -> None:
    from sandloom_billing.access import SUBSCRIPTION_ACTIVE_FACT

    billing_config(billing_deny_on_past_due=deny_past_due)
    claims = {"subscription_status": status} if status else {}
    # The grace rule is billing's (the registered fact), not shared's.
    core = _Core(facts={"subscription.active": SUBSCRIPTION_ACTIVE_FACT})
    ctx = AuthzContext(_subject(**claims), core=core)
    Subscribed = generated_atoms.Subscribed
    assert await Subscribed(of=Account.user()).evaluate(ctx) is expected


@pytest.mark.parametrize("status", ["active", "trialing", "past_due", None])
async def test_subscribed_without_its_owner_is_indeterminate_501(
    generated_atoms: SimpleNamespace, status: str | None
) -> None:
    """Billing neither loaded nor bound: nobody vouches for the claim, so
    even an ``active`` one is unknown (501), never an allow or a deny."""
    claims = {"subscription_status": status} if status else {}
    ctx = AuthzContext(_subject(**claims), core=_Core())
    decision = await generated_atoms.Subscribed(of=Account.user()).decide(ctx)
    assert not decision.allowed and decision.indeterminate
    assert (decision.http_status, decision.code) == (501, "fact_unregistered")


async def test_subscribed_escalates_to_live_when_billing_failed_at_mint(
    generated_atoms: SimpleNamespace,
) -> None:
    """Billing in ``claims_sources_failed``: the (absent) claim is not a
    genuine "unsubscribed" — Subscribed asks billing live instead of denying
    402 for the token's whole lifetime."""
    Subscribed = generated_atoms.Subscribed
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


async def test_subscribed_without_live_path_is_indeterminate_503(
    generated_atoms: SimpleNamespace,
) -> None:
    from sandloom.shared.authz.results import PolicyIndeterminate
    from sandloom_billing.access import SUBSCRIPTION_ACTIVE_FACT

    Subscribed = generated_atoms.Subscribed
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


async def test_subscribed_uses_registered_fact_interpretation(
    generated_atoms: SimpleNamespace,
) -> None:
    fact = _fact()  # interprets ONLY "active" as True
    ctx = AuthzContext(
        _subject(subscription_status="trialing"),
        core=_Core(facts={"subscription.active": fact}),
    )
    assert await generated_atoms.Subscribed(of=Account.user()).evaluate(ctx) is False


async def test_subscribed_explicit_statuses_are_verbatim(
    billing_config: Callable[..., None], generated_atoms: SimpleNamespace
) -> None:
    from sandloom_billing.access import SUBSCRIPTION_ACTIVE_FACT

    billing_config(billing_deny_on_past_due=False)
    # billing present (its fact registered), whose own rule would allow the
    # grace period: explicit statuses are read verbatim instead
    core = _Core(facts={"subscription.active": SUBSCRIPTION_ACTIVE_FACT})
    ctx = AuthzContext(_subject(subscription_status="past_due"), core=core)
    Subscribed = generated_atoms.Subscribed
    assert await Subscribed(of=Account.user()).evaluate(ctx) is True
    assert (
        await Subscribed(frozenset({"active"}), of=Account.user()).evaluate(ctx)
        is False
    )


async def test_subscribed_organization_resolves_live(
    generated_atoms: SimpleNamespace,
) -> None:
    from sandloom.shared.authz.algebra import TIER_LIVE

    Subscribed = generated_atoms.Subscribed
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
