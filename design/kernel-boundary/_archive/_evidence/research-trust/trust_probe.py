"""Trust probe (tm kb-research-trust), run at arch/kernel-boundary HEAD in the repository env.

    uv run python design/kernel-boundary/_evidence/research-trust/trust_probe.py

No database, no Redis, no network: the real CoreFacade registration and merge,
create_access_token/decode_token (HS256), UserContext, the platform gate and the
Role/Perm atoms. Each section prints what the kernel does today.
"""

import asyncio
import os
import timeit
import uuid

os.environ.update({
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "CACHE_URL": "redis://localhost:6379/9", "BROKER_URL": "redis://localhost:6379/9",
    "JWT_SECRET": "kb-trust-probe-not-a-real-secret-0123456789abcdef",
    "EVENT_BUS_BACKEND": "memory", "SANDLOOM_MODULES": "[]",
    "SERVICE_BINDINGS": '{"identity.claims.v1": {"base_url": "http://p2:8000", "service_id": "p2"},'
                        ' "inventory.claims.v1": {"base_url": "http://p2:8000", "service_id": "p2", "critical": false}}',
})

from fastapi import FastAPI, HTTPException  # noqa: E402

from sandloom.core.auth.tokens import create_access_token, decode_token  # noqa: E402
from sandloom.core.facade import CoreFacade  # noqa: E402
from sandloom.core.facade._claims import ClaimsUnavailable  # noqa: E402
from sandloom.core.module_api import contract_method  # noqa: E402
from sandloom.shared.authz import Perm, Role  # noqa: E402
from sandloom.shared.authz.context import AuthzContext  # noqa: E402
from sandloom.shared.guards.platform import require_platform_admin  # noqa: E402
from sandloom.shared.subject import UserContext  # noqa: E402

UID = uuid.uuid4()


class NoCore:
    def get_access_fact(self, name):
        return None

    def remote_fact_owners(self):
        return []


async def gate(subject):
    try:
        await require_platform_admin(subject)
        return "allowed"
    except HTTPException as exc:
        return str(exc.status_code)


async def decide(req, subject):
    d = await req.decide(AuthzContext(subject=subject, core=NoCore()))
    return "allow" if d.allowed else "deny"


async def main():
    facade = CoreFacade(app=FastAPI())

    print("== R1 remote owner name comes from the binding key text")
    print("   SERVICE_BINDINGS keys: identity.claims.v1 -> service_id p2; inventory.claims.v1 -> p2 (critical false)")
    print("   remote_claims_contributors():", facade.remote_claims_contributors())
    print("   (p2 is whatever service the operator pinned; the provenance name is 'identity' for anything it answers)")

    print("\n== R2 a module registers a contract named after another module's claims contract")

    class Fake:
        @contract_method
        async def contribute(self, user_id: str) -> dict:
            return {"roles": ["admin"]}

    facade._registering_module = "shop.modules.forged"
    try:
        facade.register_service("identity.claims.v1", Fake())
        print("   register_service('identity.claims.v1') from shop.modules.forged: accepted;",
              "local owner of the contract:", facade.contract_owner("identity.claims.v1")
              if hasattr(facade, "contract_owner") else "?")
    except Exception as exc:  # noqa: BLE001
        print("   refused:", type(exc).__name__, exc)

    print("\n== R3 owner= on the facade is stamped as given")

    async def noop(user_id):
        return {}

    facade2 = CoreFacade(app=FastAPI())
    facade2._registering_module = "shop.modules.inventory"
    facade2.add_claims_contributor(noop, owner="identity")
    print("   contributor registered by shop.modules.inventory with owner='identity' ->",
          [o for _f, _c, o in facade2._claims_contributors])

    print("\n== R4 one non-critical contributor writing a foreign or reserved key blocks every mint")

    async def ok_identity():
        return {"roles": ["member"], "permissions": []}

    plan = [("identity", True, None, None), ("analytics", False, None, None)]
    for label, value in (("reserved key 'jti'", {"jti": "x"}),
                         ("identity's key 'roles'", {"roles": []}),
                         ("its own key only", {"analytics_cohort": "b"})):
        outcomes = {0: (True, {"roles": ["member"], "permissions": []}), 1: (True, value)}
        try:
            merged = facade2._merge_claim_outcomes(plan, outcomes)
            print(f"   analytics (non-critical) writes {label}: minted, sources={merged['claims_sources']}")
        except ClaimsUnavailable as exc:
            print(f"   analytics (non-critical) writes {label}: {exc.status_code} {exc.detail!r}")

    print("\n== R5 legacy tokens (no claims_sources_failed) and tokens from code that skips collect_claims")
    cases = {
        "legacy: roles admin, claims_sources ['identity'], no failed key":
            {"sub": str(UID), "token_type": "access", "roles": ["admin"], "claims_sources": ["identity"]},
        "no provenance at all: roles admin":
            {"sub": str(UID), "token_type": "access", "roles": ["admin"], "permissions": ["perm.x"]},
        "minted by any code holding the key: roles admin, claims_sources ['identity'], failed []":
            {"sub": str(UID), "token_type": "access", "roles": ["admin"], "claims_sources": ["identity"],
             "claims_sources_failed": []},
    }
    for label, data in cases.items():
        claims = decode_token(create_access_token(data=data))
        subject = UserContext(**claims)
        print(f"   {label}: provenance_authoritative={subject.provenance_authoritative}",
              f"| platform gate {await gate(subject)} | Role('admin') {await decide(Role('admin'), subject)}",
              f"| Perm('perm.x') {await decide(Perm('perm.x'), subject)}")

    print("\n== R6 hot-path cost of a provenance check in Role (pure Python, no I/O)")
    subject = UserContext(**decode_token(create_access_token(data={
        "sub": str(UID), "token_type": "access", "roles": ["admin"],
        "claims_sources": ["identity", "tenancy", "billing"], "claims_sources_failed": []})))
    authority = "identity"

    def today():
        return "admin" in subject.roles

    def with_provenance():
        return ("admin" in subject.roles and authority in subject.claims_sources
                and authority not in subject.claims_sources_failed)

    trusted = frozenset(subject.roles) if (authority in subject.claims_sources
                                            and authority not in subject.claims_sources_failed) else frozenset()

    def precomputed():
        return "admin" in trusted

    n = 2_000_000
    for name, fn in (("today: role in roles", today),
                     ("+ authority in sources and not in failed", with_provenance),
                     ("trusted set computed once per token", precomputed)):
        best = min(timeit.repeat(fn, number=n, repeat=5))
        print(f"   {name}: {best / n * 1e9:.1f} ns per check")
    ctx = AuthzContext(subject=subject, core=NoCore())
    role = Role("admin")
    import time
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        for _ in range(20_000):
            await role.decide(ctx)
        best = min(best, time.perf_counter() - start)
    print(f"   for scale, await Role('admin').decide(ctx): {best / 20_000 * 1e9:.0f} ns")


asyncio.run(main())
