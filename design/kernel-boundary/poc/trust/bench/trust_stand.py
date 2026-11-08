"""kb-poc-trust T-15: request-rung and mint costs, base vs TA-1 candidate.

The kernel is whichever ``sandloom`` the interpreter imports (the base or the
candidate worktree); the same stand and cases run against both.

    python trust_stand.py cg   --case role_and_perm --n 200   # under valgrind
    python trust_stand.py cg   --case mint --n 200            # collect_claims + mint
    python trust_stand.py run  --n 2000 --reps 3 --out x.json # latency (in-process)

Monolith: ``sandloom.main`` with identity, tenancy, billing (three claims
contributors, the real batteries) and the stand. Tokens carry what identity
would mint: roles/permissions, tenant_ids, subscription_status, provenance and,
on the candidate, the process's ``cpv``.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "stand"))

USER = uuid.UUID("00000000-0000-4000-8000-00000000a001")
ORG = uuid.UUID("00000000-0000-4000-8000-00000000b001")
SECRET = "kb-poc-trust-stand-secret-0123456789abcdef0123456789abcdef"
PREFIX = "/api/v1/truststand"
CASES: dict[str, tuple[str, dict[str, str]]] = {
    "control": ("/ts/control", {}),
    "role_and_perm": ("/ts/role_and_perm", {}),
    "tenant": ("/ts/tenant", {"x-tenant-id": str(ORG)}),
}


def env() -> None:
    os.environ.update(
        {
            "ENVIRONMENT": "dev",
            "LOG_LEVEL": "WARNING",
            "RATE_LIMIT_ENABLED": "false",
            "DOCS_ENABLED": "false",
            "BILLING_PROVIDER": "fake",
            "OUTBOX_RELAY_ENABLED": "off",
            "JWT_SECRET": SECRET,
            "SANDLOOM_ACCEPTED_RISKS": "relay_disabled",
            "SANDLOOM_MODULES": json.dumps(
                ["sandloom_identity", "sandloom_tenancy", "sandloom_billing", "truststand"]
            ),
        }
    )


def token(app: Any) -> str:
    import jwt

    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": str(USER),
        "token_type": "access",
        "iat": now,
        "nbf": now,
        "exp": now + 6 * 3600,
        "jti": uuid.uuid4().hex,
        "roles": ["admin"],
        "permissions": ["perm.stand.read"],
        "tenant_ids": [str(ORG)],
        "subscription_status": "active",
        "claims_sources": ["billing", "identity", "tenancy"],
        "claims_sources_failed": [],
    }
    policy = getattr(app.state, "claim_trust", None)
    if policy is not None:  # candidate: minted under this process's policy
        claims["cpv"] = policy.marker
    return jwt.encode(claims, SECRET, algorithm="HS256")


def make_op(app: Any, path: str, tok: str, extra: dict[str, str]) -> Any:
    headers = [
        (b"host", b"127.0.0.1:8000"),
        (b"authorization", f"Bearer {tok}".encode()),
        *((k.encode(), v.encode()) for k, v in extra.items()),
    ]
    base = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    req = {"type": "http.request", "body": b"", "more_body": False}

    async def receive() -> Any:
        return req

    async def op() -> None:
        out: list[Any] = []

        async def send(msg: Any) -> None:
            out.append(msg)

        scope = dict(base)
        scope["state"] = {}
        await app(scope, receive, send)
        if out[0]["status"] != 200:
            raise RuntimeError(f"{path}: {out[0]['status']} {out[1:]}")

    return op


def make_mint(app: Any) -> Any:
    from sandloom.sdk.security import create_access_token

    core = app.state.core
    sealed = "claims" in inspect.signature(create_access_token).parameters

    async def op() -> None:
        claims = await core.collect_claims(USER)
        data = {"sub": str(USER), "token_type": "access"}
        if sealed:
            create_access_token(data=data, claims=claims)
        else:
            create_access_token(data={**claims, **data})

    return op


class Lifespan:
    def __init__(self, app: Any) -> None:
        self.cm = app.router.lifespan_context(app)

    async def __aenter__(self) -> Lifespan:
        await self.cm.__aenter__()
        return self

    async def aclose(self) -> None:
        await self.cm.__aexit__(None, None, None)


async def build() -> tuple[Any, Lifespan]:
    env()
    from sandloom.main import app

    return app, await Lifespan(app).__aenter__()


def ops(app: Any) -> dict[str, Any]:
    tok = token(app)
    out = {c: make_op(app, PREFIX + p, tok, h) for c, (p, h) in CASES.items()}
    out["mint"] = make_mint(app)
    return out


async def cmd_cg(a: argparse.Namespace) -> None:
    app, life = await build()
    op = ops(app)[a.case]
    for _ in range(20):
        await op()
    for _ in range(a.n):
        await op()
    await life.aclose()


def _pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


async def cmd_run(a: argparse.Namespace) -> None:
    app, life = await build()
    table = ops(app)
    import sandloom

    result: dict[str, Any] = {"kernel": os.path.dirname(sandloom.__file__), "cases": {}}
    for rep in range(a.reps):
        order = list(table)[rep % len(table) :] + list(table)[: rep % len(table)]
        for case in order:
            op = table[case]
            n = a.n if case != "mint" else max(200, a.n // 10)
            for _ in range(a.warmup if case != "mint" else 20):
                await op()
            lat: list[float] = []
            for _ in range(n):
                t0 = time.perf_counter()
                await op()
                lat.append((time.perf_counter() - t0) * 1e6)
            result["cases"].setdefault(case, []).append(
                {
                    "n": n,
                    "p50_us": statistics.median(lat),
                    "p95_us": _pct(lat, 0.95),
                    "p99_us": _pct(lat, 0.99),
                }
            )
    await life.aclose()
    Path(a.out).write_text(json.dumps(result, indent=1))
    print(json.dumps({c: v for c, v in result["cases"].items()}, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("cg")
    g.add_argument("--case", required=True)
    g.add_argument("--n", type=int, default=0)
    r = sub.add_parser("run")
    r.add_argument("--n", type=int, default=2000)
    r.add_argument("--warmup", type=int, default=200)
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--out", required=True)
    a = ap.parse_args()
    fn = {"cg": cmd_cg, "run": cmd_run}[a.cmd]
    try:
        import uvloop

        uvloop.run(fn(a))
    except ImportError:
        asyncio.run(fn(a))


if __name__ == "__main__":
    main()
