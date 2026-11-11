"""kb-poc-trust T-15 micro rung: the per-request work TA-1 changes, alone.

One verified-token cache hit (``decode_token``) + ``UserContext`` construction
+ ``Role("admin") & Perm(...)`` decided, N times, no app, no I/O. Under
cachegrind with N=0 and N=K: Ir/op = (Ir(K) - Ir(0)) / K. The kernel is the
``sandloom`` on PYTHONPATH (base or candidate).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

os.environ.setdefault("JWT_SECRET", "kb-poc-trust-micro-secret-0123456789abcdef0123456789")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@127.0.0.1:1/x")
os.environ.setdefault("CACHE_URL", "redis://127.0.0.1:1/0")
os.environ.setdefault("BROKER_URL", "redis://127.0.0.1:1/1")

import jwt  # noqa: E402

from sandloom.core.auth import tokens  # noqa: E402
from sandloom.shared.authz import Perm, Role  # noqa: E402
from sandloom.shared.authz.context import AuthzContext  # noqa: E402
from sandloom.shared.subject import UserContext  # noqa: E402

try:
    import sandloom.core.auth.claim_trust as ct

    POLICY = ct.ClaimTrustPolicy(
        authorities={
            # last segments = the short names stamped in claims_sources (the
            # cachegrind image has no entry points to map sandloom_identity)
            "roles": "app.identity",
            "permissions": "app.identity",
            "tenant_ids": "app.tenancy",
            "subscription_status": "app.billing",
            "plan_code": "app.billing",
        }
    )
    ct.activate(POLICY)
except ImportError:  # base kernel
    POLICY = None


def main(n: int) -> None:
    now = int(time.time())
    claims = {
        "sub": str(uuid.uuid4()),
        "token_type": "access",
        "iat": now,
        "nbf": now,
        "exp": now + 6 * 3600,
        "jti": uuid.uuid4().hex,
        "roles": ["admin"],
        "permissions": ["perm.stand.read"],
        "tenant_ids": [str(uuid.uuid4())],
        "subscription_status": "active",
        "claims_sources": ["billing", "identity", "tenancy"],
        "claims_sources_failed": [],
    }
    if POLICY is not None:
        claims["cpv"] = POLICY.marker
    tok = jwt.encode(claims, os.environ["JWT_SECRET"], algorithm="HS256")
    policy = Role("admin") & Perm("perm.stand.read")

    async def op() -> None:
        payload = tokens.decode_token(tok)
        assert payload is not None
        subject = UserContext(**payload)
        decision = await policy.decide(AuthzContext(subject=subject))
        if not decision.allowed:
            raise SystemExit(f"denied: {decision}")

    async def run() -> None:
        for _ in range(200):
            await op()
        for _ in range(n):
            await op()

    asyncio.run(run())


if __name__ == "__main__":
    main(int(sys.argv[1]))
