"""Authorization subjects and billing accounts (spec §7; ledger G-AUTHZ-*,
C-AUTH-06, G-BILLING personal spend).

Independent oracles (spec §10.1): the account a live atom asks billing about
is read off a recording fake entitlements contract (what was ASKED, not what
the kernel thinks it resolved); the membership authority is a fake contract
that confirms exactly the organizations it was told about.
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from sandloom.core.config.settings import settings
from sandloom.core.di import bind_tenant
from sandloom.core.module_api._service_registry import ServiceUnavailableInDeployment
from sandloom.shared.authz import (
    Account,
    Credits,
    Feature,
    PolicyIndeterminate,
    Subscribed,
    SubscriptionActive,
    WithinLimit,
)
from sandloom.shared.authz.account import (
    AuthzSubjectMissingError,
    resolve_account,
    tenant_kind,
)
from sandloom.shared.authz.context import AuthzContext
from sandloom.shared.subject import UserContext

pytestmark = pytest.mark.asyncio

ENT = "billing.entitlements.v1"
MEM = "tenancy.membership.v1"


class _Core:
    def __init__(self, members: set[UUID]) -> None:
        self.asked: list[tuple[str, UUID]] = []
        self.members = members
        core = self

        async def check_subscription_active(tt: str, tid: UUID) -> bool:
            core.asked.append((tt, tid))
            return True

        async def get_credit_balance(tt: str, tid: UUID) -> int:
            core.asked.append((tt, tid))
            return 100

        async def has_role_at_least(org: UUID, user: UUID, minimum: str) -> bool:
            return org in core.members

        self.services: dict[str, Any] = {
            ENT: SimpleNamespace(
                check_subscription_active=check_subscription_active,
                get_credit_balance=get_credit_balance,
            ),
            MEM: SimpleNamespace(has_role_at_least=has_role_at_least),
        }

    def get_service(self, name: str) -> Any:
        if name not in self.services:
            raise ServiceUnavailableInDeployment(name)
        return self.services[name]

    def get_metric(self, name: str) -> Any:
        return None


class _Req:
    def __init__(
        self,
        path: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.path_params = path or {}
        self.query_params = query or {}
        self.headers = headers or {}
        self.scope: dict[str, Any] = {"state": {}}


def _subject(user: UUID, tenants: list[UUID]) -> UserContext:
    return UserContext(
        user_id=user,
        tenant_ids=[str(t) for t in tenants],
        claims={"subscription_status": "active"},
    )


# --------------------------------------------------------------------------- #
# of= is required (SANDLOOM-B017)
# --------------------------------------------------------------------------- #
async def test_every_billing_atom_requires_of() -> None:
    makers = {
        "Subscribed": lambda **kw: Subscribed(**kw),
        "SubscriptionActive": lambda **kw: SubscriptionActive(**kw),
        "Credits": lambda **kw: Credits(1, **kw),
        "Feature": lambda **kw: Feature("export", **kw),
        "WithinLimit": lambda **kw: WithinLimit("seats", 1, **kw),
    }
    for name, make in makers.items():
        with pytest.raises(AuthzSubjectMissingError) as exc:
            make()
        assert isinstance(exc.value, TypeError)
        assert exc.value.sandloom_code == "SANDLOOM-B017"
        message = str(exc.value)
        assert name in message and "Account.org(param=" in message, message
        assert "Account.user()" in message and "Account.bound_tenant()" in message
        with pytest.raises(AuthzSubjectMissingError):
            make(of="user")  # not an Account
        assert make(of=Account.user()) is not None


async def test_the_personal_claims_check_stays_zero_io() -> None:
    from sandloom.shared.authz.algebra import TIER_CLAIM

    atom = Subscribed(of=Account.user())
    assert atom.tier == TIER_CLAIM and atom.org_params() == frozenset()
    user = uuid4()
    # no core, no request: answered from the claim alone
    assert await atom.evaluate(AuthzContext(_subject(user, []))) is True
    assert Subscribed(of=Account.org()).tier > TIER_CLAIM


# --------------------------------------------------------------------------- #
# route shapes x tenant kinds
# --------------------------------------------------------------------------- #
async def test_route_shapes_resolve_the_stated_account_only() -> None:
    user, org, other = uuid4(), uuid4(), uuid4()
    core = _Core(members={org})
    subject = _subject(user, [org])

    async def asked(account: Account, request: _Req, bound: UUID | None = None) -> Any:
        core.asked.clear()
        ctx = AuthzContext(subject, request=request, core=core)  # type: ignore[arg-type]
        try:
            if bound is not None:
                with bind_tenant(bound):
                    await SubscriptionActive(of=account).evaluate(ctx)
            else:
                await SubscriptionActive(of=account).evaluate(ctx)
        except PolicyIndeterminate as exc:
            return exc.decision.http_status
        return core.asked[-1]

    # Account.user(): the caller's own account, whatever the route carries
    assert await asked(Account.user(), _Req(path={"org_id": str(org)})) == (
        "user",
        user,
    )
    # Account.org(param=): the path parameter, else the query parameter
    assert await asked(
        Account.org(param="org_id"), _Req(path={"org_id": str(org)})
    ) == (
        "organization",
        org,
    )
    assert await asked(
        Account.org(param="organization_id"), _Req(query={"organization_id": str(org)})
    ) == ("organization", org)
    # ... else the X-Tenant-Id header, else the bound tenant
    assert await asked(
        Account.org(param=None), _Req(headers={"X-Tenant-Id": str(org)})
    ) == ("organization", org)
    assert await asked(Account.org(param=None), _Req(), bound=org) == (
        "organization",
        org,
    )
    # no organization: indeterminate, NEVER the caller's own id
    assert await asked(Account.org(), _Req()) == 400
    # an organization the token does not grant: indeterminate 403
    assert await asked(Account.org(), _Req(path={"org_id": str(other)})) == 403


async def test_bound_tenant_resolves_by_tenant_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, org, stranger_org = uuid4(), uuid4(), uuid4()
    core = _Core(members={org})
    subject = _subject(user, [org, stranger_org])

    async def account_for(bound: UUID) -> Any:
        ctx = AuthzContext(subject, request=_Req(), core=core)  # type: ignore[arg-type]
        try:
            with bind_tenant(bound):
                return await resolve_account(
                    ctx, Account.bound_tenant(), requirement="t"
                )
        except PolicyIndeterminate as exc:
            return exc.decision.http_status

    assert await account_for(user) == ("user", user)  # personal
    assert await account_for(org) == ("organization", org)  # membership confirmed
    assert await account_for(stranger_org) == 403  # unknown
    monkeypatch.setattr(settings, "tenancy_mode", "single")
    assert await account_for(org) == 403  # canonical
    ctx = AuthzContext(subject, request=_Req(), core=core)  # type: ignore[arg-type]
    with pytest.raises(PolicyIndeterminate):
        await resolve_account(ctx, Account.bound_tenant(), requirement="t")  # unbound


async def test_tenant_kind_is_authoritative_and_memoised() -> None:
    user, org = uuid4(), uuid4()
    core = _Core(members={org})
    calls: list[UUID] = []
    has_role = core.services[MEM].has_role_at_least

    async def counting(o: UUID, u: UUID, m: str) -> bool:
        calls.append(o)
        return await has_role(o, u, m)

    core.services[MEM].has_role_at_least = counting
    request = _Req()
    ctx = AuthzContext(_subject(user, [org]), request=request, core=core)  # type: ignore[arg-type]
    assert await tenant_kind(user, ctx=ctx) == "personal"
    assert await tenant_kind(org, ctx=ctx) == "organization"
    assert await tenant_kind(org, ctx=ctx) == "organization"
    assert await tenant_kind(uuid4(), ctx=ctx) == "unknown"
    assert calls.count(org) == 1  # memoised per request
    no_tenancy = _Core(members=set())
    del no_tenancy.services[MEM]
    ctx2 = AuthzContext(_subject(user, [org]), request=_Req(), core=no_tenancy)  # type: ignore[arg-type]
    assert await tenant_kind(org, ctx=ctx2) == "unknown"


async def test_no_resolution_returns_a_foreign_account() -> None:
    """Property (spec §7.5): over random route shapes, bindings and kinds, an
    atom only ever asks about the caller's personal account or an
    organization the membership authority confirmed / the token grants."""
    rng = random.Random(1729)
    for _ in range(300):
        user = uuid4()
        member_orgs = {uuid4() for _ in range(rng.randint(0, 2))}
        strangers = {uuid4() for _ in range(rng.randint(0, 2))}
        granted = list(
            member_orgs | set(rng.sample(sorted(strangers), k=len(strangers) // 2))
        )
        core = _Core(members=member_orgs)
        candidates = [user, *member_orgs, *strangers, uuid4()]
        path = {"org_id": str(rng.choice(candidates))} if rng.random() < 0.5 else {}
        headers = (
            {"X-Tenant-Id": str(rng.choice(candidates))} if rng.random() < 0.3 else {}
        )
        bound = rng.choice([None, *candidates])
        account = rng.choice(
            [
                Account.user(),
                Account.org(),
                Account.org(param=None),
                Account.bound_tenant(),
            ]
        )
        ctx = AuthzContext(
            _subject(user, granted), request=_Req(path=path, headers=headers), core=core
        )  # type: ignore[arg-type]
        try:
            if bound is not None:
                with bind_tenant(bound):
                    await Credits(1, of=account).evaluate(ctx)
            else:
                await Credits(1, of=account).evaluate(ctx)
        except PolicyIndeterminate:
            continue
        kind, asked = core.asked[-1]
        if kind == "user":
            assert asked == user
        else:
            assert asked != user
            assert asked in member_orgs or asked in granted or asked == bound


# --------------------------------------------------------------------------- #
# Feature / WithinLimit for an organization: the owner's facts, live
# --------------------------------------------------------------------------- #
async def test_org_feature_and_limit_need_the_owners_fact() -> None:
    user, org = uuid4(), uuid4()
    core = _Core(members={org})
    core.facts = {}  # type: ignore[attr-defined]
    subject = UserContext(
        user_id=user,
        tenant_ids=[str(org)],
        claims={"features": {"export": True}, "limits": {"seats": 3}},
    )
    personal = AuthzContext(subject)
    assert await Feature("export", of=Account.user()).evaluate(personal) is True
    assert await WithinLimit("seats", 2, of=Account.user()).evaluate(personal) is True
    org_ctx = AuthzContext(subject, request=_Req(path={"org_id": str(org)}), core=core)  # type: ignore[arg-type]
    for atom in (
        Feature("export", of=Account.org()),
        WithinLimit("seats", 2, of=Account.org()),
    ):
        decision = await atom.decide(org_ctx)
        assert not decision.allowed and decision.http_status == 501  # no fact


# --------------------------------------------------------------------------- #
# billing's personal-spend check (spec §7.4)
# --------------------------------------------------------------------------- #
async def test_a_personal_spend_must_be_the_callers_own() -> None:
    import sandloom.core.contract_context as cc
    from sandloom.core.module_api._context import caller_user_id, stamp_caller
    from sandloom.core.unit_of_work.request import RequestScope, bind_request_scope
    from sandloom_billing.credits import CALLER_USER_INFO_KEY, ensure_tenant_matches

    alice, bob = uuid4(), uuid4()

    class _S:
        info: dict[str, Any] = {}

    session = _S()
    session.info = {}
    # a request of alice: the contract unit is stamped with alice
    scope = RequestScope(core=None, policy=None)
    scope.subject, scope.claims = str(alice), {"sub": str(alice)}
    with bind_request_scope(scope):
        assert caller_user_id() == alice
        stamp_caller(session)
    assert session.info[CALLER_USER_INFO_KEY] == alice
    bound = cc._contract_session_var.set(session)
    try:
        ensure_tenant_matches("user", alice)
        with pytest.raises(Exception) as exc:
            ensure_tenant_matches("user", bob)
        assert type(exc.value).__name__ == "BillingTenantMismatchError"
    finally:
        cc._contract_session_var.reset(bound)
    # over RPC: the signed caller facts, bound as the call's request scope
    from sandloom.core.rpc.context import _caller_scope

    with bind_request_scope(_caller_scope(None, {"sub": str(bob)})):
        assert caller_user_id() == bob
    # a system / service caller: no end user, any personal account
    session.info = {}
    stamp_caller(session)
    assert session.info[CALLER_USER_INFO_KEY] is None
    bound = cc._contract_session_var.set(session)
    try:
        ensure_tenant_matches("user", bob)
    finally:
        cc._contract_session_var.reset(bound)
