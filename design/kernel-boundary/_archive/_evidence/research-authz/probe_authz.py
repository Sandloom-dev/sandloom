"""Probes for research-authz-portability.md (no DB, no network).

Run from the repository root:  uv run python design/kernel-boundary/_evidence/research-authz/probe_authz.py
P1  a bound-but-unreachable provider inside Or: exception or indeterminate decision?
P2  which require() policies depend on the request DB session (get_db)?
P3  the evaluation order of And/Or with equal tiers, and with a generated-style atom
    whose tier differs from today's (order is semantic: ~(F & U) vs ~(U & F)).
P4  how many contract calls OrgRole("admin") & SubscriptionActive(of=bound_tenant) makes,
    and with which arguments (the memo key), with a counting fake core.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid

from sandloom.core.rpc.errors import RemoteServiceUnavailable
from sandloom.shared.authz import (
    TIER_CLAIM,
    TIER_LIVE,
    Account,
    AuthzContext,
    Credits,
    Entitled,
    OrgRole,
    Requirement,
    Role,
    Subscribed,
    SubscriptionActive,
    require,
)
from sandloom.shared.authz.results import AccessDecision, PolicyIndeterminate
from sandloom.shared.subject import UserContext

USER = uuid.uuid4()
ORG = uuid.uuid4()


class _Req:
    def __init__(self, path_params=None, headers=None):
        self.path_params = path_params or {}
        self.query_params = {}
        self.headers = headers or {}
        self.scope = {"state": {}}


class Down:
    """A bound provider whose every call fails like the RPC proxy does."""

    def __getattr__(self, name):
        async def call(*a, **k):
            raise RemoteServiceUnavailable("tenancy.membership.v1", reason="ConnectError")

        return call


class Counting:
    def __init__(self, log, answers):
        self.log, self.answers = log, answers

    def __getattr__(self, name):
        async def call(*a, **k):
            self.log.append((name, a))
            return self.answers[name](*a)

        return call


class Core:
    def __init__(self, services):
        self.services = services

    def get_service(self, name):
        from sandloom.core.di import ServiceNotFound  # noqa: PLC0415

        if name not in self.services:
            raise ServiceNotFound(name)
        return self.services[name]


def ctx(core, request=None, roles=()):
    subject = UserContext(user_id=USER, roles=set(roles), tenant_ids=[str(ORG)])
    return AuthzContext(subject, request=request or _Req({"org_id": str(ORG)}), core=core)


class Fixed(Requirement):
    def __init__(self, name, outcome, tier, log):
        self.name, self.outcome, self.tier, self.log = name, outcome, tier, log

    async def decide(self, c):
        self.log.append(self.name)
        if self.outcome == "U":
            return AccessDecision.error(code="u", category="policy_misconfigured", http_status=501, reason="u", failed_requirement=self.name)
        return AccessDecision.allow() if self.outcome == "T" else self._deny()

    async def evaluate(self, c):
        return await self._evaluate_via_decide(c)

    def describe(self):
        return self.name


async def p1():
    print("P1  provider bound but down, inside Or")
    allow_branch = SubscriptionActive(of=Account.user())
    core = Core({
        "tenancy.membership.v1": Down(),
        "billing.entitlements.v1": Counting([], {"check_subscription_active": lambda *a: True}),
    })
    policy = OrgRole("admin") | allow_branch
    try:
        d = await policy.decide(ctx(core))
        print("    decision:", d.allowed, d.indeterminate, d.code)
    except Exception as exc:  # noqa: BLE001
        print("    raised:", type(exc).__name__, getattr(exc, "status_code", None),
              "-> the allowing sibling never ran; no indeterminate decision, no deny log")
    unbound = OrgRole("admin") | allow_branch
    d = await unbound.decide(ctx(Core({"billing.entitlements.v1": core.services["billing.entitlements.v1"]})))
    print("    same policy, tenancy NOT bound (absent):", "allow" if d.allowed else d.code)


def p2():
    print("P2  require(policy) depends on get_db?  (tier > TIER_CLAIM, deps.py:121-123)")
    for label, pol in [
        ('Role("admin")', Role("admin")),
        ('Subscribed(of=Account.user())', Subscribed(of=Account.user())),
        ('Role("admin") | Credits(10, of=Account.user())', Role("admin") | Credits(10, of=Account.user())),
        ('Entitled("subscription.active")', Entitled("subscription.active")),
        ('OrgRole("admin")', OrgRole("admin")),
    ]:
        dep = require(pol)
        params = list(inspect.signature(dep).parameters)
        print(f"    {label:52} tier={pol.tier} db_param={'db' in params}")


async def p3():
    print("P3  order: And/Or sort by tier, stable for equal tiers (algebra.py:165, :209)")
    for desc, parts in [("~(F & U) both live", [("F", "F", TIER_LIVE), ("U", "U", TIER_LIVE)]),
                        ("~(U & F) both live", [("U", "U", TIER_LIVE), ("F", "F", TIER_LIVE)]),
                        ("~(U & F) F re-tiered to claim", [("U", "U", TIER_LIVE), ("F", "F", TIER_CLAIM)])]:
        log: list[str] = []
        a, b = (Fixed(n, o, t, log) for n, o, t in parts)
        d = await (~(a & b)).decide(ctx(Core({})))
        verdict = "allow" if d.allowed else ("indeterminate" if d.indeterminate else "deny")
        print(f"    {desc:32} evaluated={log} -> {verdict}")


async def p4():
    print("P4  OrgRole('admin') & SubscriptionActive(of=Account.bound_tenant()) - contract calls")
    from sandloom.core import tenant_context  # noqa: PLC0415

    log: list = []
    core = Core({
        "tenancy.membership.v1": Counting(log, {"has_role_at_least": lambda *a: True}),
        "billing.entitlements.v1": Counting(log, {"check_subscription_active": lambda *a: True}),
    })
    policy = OrgRole("admin") & SubscriptionActive(of=Account.bound_tenant())
    req = _Req(headers={})
    with tenant_context.bind_tenant(ORG):
        d = await policy.decide(ctx(core, request=req))
    print("    decision allowed:", d.allowed)
    for name, args in log:
        print("    call:", name, tuple(str(a) if isinstance(a, uuid.UUID) else a for a in args))
    print("    total contract calls:", len(log))


async def main():
    await p1()
    p2()
    await p3()
    await p4()


asyncio.run(main())
