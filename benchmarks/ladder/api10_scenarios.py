"""Public API 1.0 performance scenarios (spec §10.2), in-process.

    python ladder/api10_scenarios.py pool     [--n 400] [--conc 50] [--pool 5]
    python ladder/api10_scenarios.py lazyread [--n 200]
    python ladder/api10_scenarios.py orggate  [--n 2000]
    python ladder/api10_scenarios.py projfan  [--events 20000] [--projections 4]

* ``pool`` — eager vs lazy write unit under concurrency: N concurrent writes,
  each with a 50 ms awaited "contract call" inside the handler BEFORE its
  first statement. ``DbSession`` (eager: the request unit's connection is
  held from the dependency on) vs ``db()`` (the lazy unit: no connection until
  the first statement). Pool occupancy (peak checked-out), latency p50/p99
  (the checkout wait shows as latency above the 50 ms floor), throughput.
* ``lazyread`` — connections a read route checks out when it never calls
  ``db()`` (target 0) vs one that does.
* ``orggate`` — requests/s of a ``require(Subscribed(of=Account.org(...)))``
  gate (live: membership + billing facts) vs ``of=Account.user()``
  (claims-only), on the platform stand (identity + tenancy + billing).
* ``projfan`` — projection fan-out apply throughput on the in-memory
  conditional-write store, and the consistency-token wait p99 under lag.

S5 / write-stand env (``pool``, ``lazyread``): the DB must be prepared with
``python -m benchsuite.setup_db``. Platform env (``orggate``): ``python -m
benchsuite.platform seed`` on its own database (``PLATFORM_DATABASE_URL``).
Prints one JSON line per scenario.
"""



import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


async def _asgi(app: Any, method: str, path: str, token: str | None) -> int:
    headers = [(b"host", b"bench"), (b"content-type", b"application/json")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    raw, _, qs = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": raw,
        "raw_path": raw.encode(),
        "query_string": qs.encode(),
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("bench", 80),
        "state": {},
    }
    body = b"{}"
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            await asyncio.sleep(3600)
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    status = 0
    chunks: list[bytes] = []

    async def send(msg: dict[str, Any]) -> None:
        nonlocal status
        if msg["type"] == "http.response.start":
            status = msg["status"]
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body", b""))

    await app(scope, receive, send)
    if status >= 500 and os.environ.get("A10_DEBUG"):
        print(method, path, status, b"".join(chunks)[:500], file=sys.stderr)
    return status


# --------------------------------------------------------------------------- #
# pool / lazyread (S5 env)
# --------------------------------------------------------------------------- #
def _write_routes(app: Any, core: Any) -> None:
    from fastapi import APIRouter, Depends

    from sandloom.core.di import DbSession
    from sandloom.core.http.route_runtime import install_route_runtime
    from sandloom.core.unit_of_work.request import db
    from sandloom.shared.authz import current_context
    from benchsuite.bench.models import BenchWrite

    router = APIRouter(prefix="/a10", dependencies=[Depends(current_context)])

    @router.post("/eager", status_code=201)
    async def eager(session: DbSession) -> dict[str, int]:
        await asyncio.sleep(0.05)  # the contract call, before the first statement
        row = BenchWrite(name="eager", value=1)
        session.add(row)
        await session.flush()
        return {"id": int(row.id)}

    @router.post("/held", status_code=201)
    async def held(session: DbSession) -> dict[str, int]:
        from sqlalchemy import text

        await session.execute(text("SELECT 1"))  # a read first: connection held
        await asyncio.sleep(0.05)  # ... across the contract call
        row = BenchWrite(name="held", value=1)
        session.add(row)
        await session.flush()
        return {"id": int(row.id)}

    @router.post("/lazy", status_code=201)
    async def lazy() -> dict[str, int]:
        await asyncio.sleep(0.05)
        session = db()
        row = BenchWrite(name="lazy", value=1)
        session.add(row)
        await session.flush()
        return {"id": int(row.id)}

    @router.get("/noread")
    async def noread() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/dbread")
    async def dbread() -> dict[str, int]:
        from sqlalchemy import text

        return {"one": int((await db().execute(text("SELECT 1"))).scalar_one())}

    app.include_router(router)
    install_route_runtime(app.routes, core)


async def _s5_app(pool: int) -> tuple[Any, Any, Any, str]:
    os.environ["DB_POOL_SIZE"] = str(pool)
    os.environ["DB_MAX_OVERFLOW"] = "0"
    import ladder

    token = ladder._s5_env("uow")
    os.environ["DB_POOL_SIZE"] = str(pool)
    os.environ["DB_MAX_OVERFLOW"] = "0"
    app, life = await ladder._sandloom_app()
    from sandloom.core.runtime import get_active_core

    core = get_active_core()
    _write_routes(app, core)
    return app, life, core, token


class _PoolWatch:
    def __init__(self, engine: Any) -> None:
        from sqlalchemy import event

        self.out = 0
        self.peak = 0
        self.checkouts = 0
        sync = engine.sync_engine
        event.listen(sync.pool, "checkout", self._checkout)
        event.listen(sync.pool, "checkin", self._checkin)

    def _checkout(self, *_a: Any) -> None:
        self.out += 1
        self.checkouts += 1
        self.peak = max(self.peak, self.out)

    def _checkin(self, *_a: Any) -> None:
        self.out -= 1

    def reset(self) -> None:
        self.peak = self.out
        self.checkouts = 0


async def scenario_pool(n: int, conc: int, pool: int) -> dict[str, Any]:
    app, life, core, token = await _s5_app(pool)
    watch = _PoolWatch(core.database("default").engine)
    out: dict[str, Any] = {"scenario": "pool", "n": n, "conc": conc, "pool": pool}
    try:
        for path in ("/a10/eager", "/a10/lazy", "/a10/held"):
            for _ in range(5):
                status = await _asgi(app, "POST", path, token)
                if status != 201:
                    raise RuntimeError(f"{path}: warm-up answered {status}")
            watch.reset()
            sem = asyncio.Semaphore(conc)
            lat: list[float] = []

            async def one() -> None:
                async with sem:
                    t0 = time.perf_counter()
                    status = await _asgi(app, "POST", path, token)
                    lat.append((time.perf_counter() - t0) * 1000)
                    if status != 201:
                        raise RuntimeError(f"{path}: {status}")

            t0 = time.perf_counter()
            await asyncio.gather(*(one() for _ in range(n)))
            wall = time.perf_counter() - t0
            out[path.rsplit("/", 1)[1]] = {
                "rps": round(n / wall, 1),
                "p50_ms": round(_pct(lat, 0.5), 1),
                "p99_ms": round(_pct(lat, 0.99), 1),
                "wait_p99_ms": round(max(0.0, _pct(lat, 0.99) - 50.0), 1),
                "peak_checked_out": watch.peak,
            }
    finally:
        await life.aclose()
    return out


async def scenario_lazyread(n: int) -> dict[str, Any]:
    app, life, core, token = await _s5_app(10)
    watch = _PoolWatch(core.database("default").engine)
    out: dict[str, Any] = {"scenario": "lazyread", "n": n}
    try:
        for path in ("/a10/noread", "/a10/dbread"):
            for _ in range(5):
                assert await _asgi(app, "GET", path, token) == 200
            watch.reset()
            for _ in range(n):
                assert await _asgi(app, "GET", path, token) == 200
            out[path.rsplit("/", 1)[1]] = {"checkouts_per_request": watch.checkouts / n}
    finally:
        await life.aclose()
    return out


# --------------------------------------------------------------------------- #
# orggate (platform env)
# --------------------------------------------------------------------------- #
_USER_CUSTOMER = uuid.UUID("00000000-0000-4000-8000-0000000000e2")
_ORG_CUSTOMER = uuid.UUID("00000000-0000-4000-8000-0000000000e1")


async def _seed_subscriptions(common: Any, platform: Any) -> None:
    """An active subscription for the platform user (the ``user_claims`` gate
    reads it from the token's real ``subscription_status``) and for its
    organization (the ``org_live`` gate reads it live). Idempotent."""
    import asyncpg

    conn = await asyncpg.connect(common.asyncpg_dsn())
    try:
        await conn.execute(
            "INSERT INTO billing_products (id, name, type, is_active) VALUES"
            " (951, 'Gate Team', 'subscription', true) ON CONFLICT DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO billing_prices (id, product_id, amount, currency,"
            " recurring_interval, stripe_price_id) VALUES"
            " (961, 951, 9900, 'USD', 'month', 'price_gate_team')"
            " ON CONFLICT DO NOTHING"
        )
        for customer, user, org in (
            (_USER_CUSTOMER, platform.USER, None),
            (_ORG_CUSTOMER, None, platform.ORG),
        ):
            await conn.execute(
                "INSERT INTO billing_customers (id, user_id, organization_id)"
                " VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                customer,
                user,
                org,
            )
            if not await conn.fetchval(
                "SELECT 1 FROM billing_subscriptions WHERE customer_id = $1", customer
            ):
                await conn.execute(
                    "INSERT INTO billing_subscriptions (customer_id, price_id,"
                    " status, current_period_end, cancel_at_period_end, origin)"
                    " VALUES ($1, 961, 'active', now() + interval '30 days',"
                    " false, 'intent')",
                    customer,
                )
    finally:
        await conn.close()


async def scenario_orggate(n: int) -> dict[str, Any]:
    from benchsuite import common, compat, platform

    env = platform.server_env()
    env["DATABASE_URL"] = os.environ.get("PLATFORM_DATABASE_URL", env["DATABASE_URL"])
    os.environ.update(env)
    await _seed_subscriptions(common, platform)
    from fastapi import APIRouter, Depends

    from sandloom.core.http.route_runtime import install_route_runtime
    from sandloom_standard.main import app
    from sandloom.shared.authz import current_context, require
    from sandloom.shared.authz.account import Account

    Subscribed = compat.subscribed_atom()

    router = APIRouter(prefix="/a10gate", dependencies=[Depends(current_context)])

    @router.get("/user", dependencies=[Depends(require(Subscribed(of=Account.user())))])
    async def gate_user() -> dict[str, bool]:
        return {"ok": True}

    @router.get(
        "/org/{org_id}",
        dependencies=[Depends(require(Subscribed(of=Account.org(param="org_id"))))],
    )
    async def gate_org(org_id: uuid.UUID) -> dict[str, bool]:
        return {"ok": True}

    import ladder

    life = await ladder._Lifespan(app).__aenter__()
    app.include_router(router)
    install_route_runtime(app.routes, app.state.core)
    token = platform.mint()  # the real mint: claims from the seeded database
    out: dict[str, Any] = {"scenario": "orggate", "n": n}
    try:
        for name, path in (
            ("user_claims", "/a10gate/user"),
            ("org_live", f"/a10gate/org/{platform.ORG}"),
        ):
            statuses: dict[int, int] = {}
            for _ in range(20):
                await _asgi(app, "GET", path, token)
            t0 = time.perf_counter()
            for _ in range(n):
                s = await _asgi(app, "GET", path, token)
                statuses[s] = statuses.get(s, 0) + 1
            wall = time.perf_counter() - t0
            out[name] = {"rps": round(n / wall, 1), "statuses": statuses}
    finally:
        await life.aclose()
    return out


# --------------------------------------------------------------------------- #
# projfan (pure kernel, in-memory store)
# --------------------------------------------------------------------------- #
async def scenario_projfan(events: int, projections: int) -> dict[str, Any]:
    from sandloom.core.projections.stores import MemoryDocumentStore
    from sandloom.core.projections.tokens import (
        entries_satisfied,
        issue_token,
        key_digest,
        read_token,
    )

    tenant = uuid.uuid4()
    stores = [MemoryDocumentStore() for _ in range(projections)]
    t0 = time.perf_counter()
    for i in range(events):
        key = {"tenant_id": str(tenant), "id": str(i % 1000)}
        for store in stores:
            await store.put_if_newer(
                tenant_id=tenant,
                key=key,
                version_column="v",
                version=i,
                fields={"title": f"t{i}"},
            )
    wall = time.perf_counter() - t0
    applies = events * projections
    keys = {"k1": "bench-secret"}
    per_lag: dict[int, dict[str, Any]] = {}
    for lag_ms in (0, 50, 100, 200, 400):
        waits: list[float] = []
        stale = 0
        for i in range(40):
            store = MemoryDocumentStore()
            key = {"tenant_id": str(tenant), "id": f"w{i}"}
            token = read_token(
                issue_token([("p", key, 1)], tenant_id=tenant, keys=keys),
                tenant_id=tenant,
                keys=keys,
            )
            digest = key_digest(key)

            async def apply_later(store: Any = store, key: Any = key) -> None:
                await asyncio.sleep(lag_ms / 1000)
                await store.put_if_newer(
                    tenant_id=tenant, key=key, version_column="v", version=1,
                    fields={"title": "x"},
                )

            task = asyncio.create_task(apply_later())
            started = time.perf_counter()
            versions: dict[str, int | None] = {}
            deadline = started + 0.3  # PROJECTION_RYW_WAIT_MS default
            while True:
                versions[digest] = await store.version_of(
                    tenant_id=tenant, key=key, version_column="v"
                )
                if entries_satisfied(token, "p", versions.get):
                    break
                if time.perf_counter() >= deadline:
                    stale += 1
                    break
                await asyncio.sleep(0.005)
            waits.append((time.perf_counter() - started) * 1000)
            await task
        per_lag[lag_ms] = {
            "wait_p50_ms": round(statistics.median(waits), 1),
            "wait_p99_ms": round(_pct(waits, 0.99), 1),
            "served_stale": round(stale / len(waits), 2),
        }
    return {
        "scenario": "projfan",
        "projections": projections,
        "events": events,
        "applies_per_s": round(applies / wall),
        "token_wait_by_lag_ms": per_lag,
        "wait_budget_ms": 300,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("scenario", choices=["pool", "lazyread", "orggate", "projfan"])
    p.add_argument("--n", type=int, default=400)
    p.add_argument("--conc", type=int, default=50)
    p.add_argument("--pool", type=int, default=5)
    p.add_argument("--events", type=int, default=20000)
    p.add_argument("--projections", type=int, default=4)
    a = p.parse_args()
    if a.scenario == "pool":
        res = asyncio.run(scenario_pool(a.n, a.conc, a.pool))
    elif a.scenario == "lazyread":
        res = asyncio.run(scenario_lazyread(a.n))
    elif a.scenario == "orggate":
        res = asyncio.run(scenario_orggate(a.n))
    else:
        res = asyncio.run(scenario_projfan(a.events, a.projections))
    res["load_avg"] = os.getloadavg()
    print(json.dumps(res))


if __name__ == "__main__":
    main()
