# ruff: noqa: F811 - the shared ``pg_proxy`` fixture is imported and requested by name
"""RPC cost-decomposition stand (DIAGNOSTIC ONLY): every rung x layer works.

The ladder rungs ``R<m>[t].<layer>`` (``ladder/stands.py``) call
``benchsuite.rpcstand.build_layer`` — the SAME builder these tests exercise,
so a rung that would measure a broken path fails here first:

* every method x layer x (tenant | none) returns the provider's answer
  (``srv`` returns the raw reply: decoded and checked too);
* the provider side does the same database / Redis work locally and over
  RPC, and the client-only layers (``enc`` / ``cli``) do none at all;
* the protocol-level round trips of every method are pinned (they are the
  denominators of the per-call CPU numbers);
* the command rung dedupes (reserve + terminal record) and the replay rung
  only reads the record; every call is pair-MAC authenticated and the tenant
  rungs really carry the tenant in the authenticated context.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from test_s5_secure_read import _dsn, _url, pg_proxy  # noqa: F401 - fixture
from tests.perf_budgets.pgcount import PgCountingProxy

from sandloom.core.contract_testing import rpc_settings
from sandloom.core.config.settings import settings
from sandloom.core.testing.infra import override_instance
from benchsuite import rpcstand
from benchsuite.bench.rpc_diag import RpcDiagContract

pytestmark = pytest.mark.asyncio

METHODS = rpcstand.METHODS
LAYERS = list(rpcstand.LAYERS)
#: PostgreSQL round trips of ONE call, provider side (identical for every
#: layer that reaches the provider: loc / srv / rpc / rpcn).
EXPECTED_PG = {
    "R0": [],  # planned no-op: no Unit of Work at all
    "R2": [],  # a read UoW that sends nothing opens no connection (lazy)
    "R2q": ["BEGIN", "SELECT", "ROLLBACK"],
    "R3": ["BEGIN", "SELECT", "ROLLBACK"],  # planner general: BEGIN READ ONLY
    # READ ONLY is mandatory even without tenant_rls: a SELECT can call a
    # volatile writing function. Historical autocommit numbers no longer apply.
    "R3o": ["BEGIN", "SELECT", "COMMIT"],
    "R3u": ["BEGIN", "SELECT", "ROLLBACK"],
    # a command with an empty body still writes its durable dedupe record:
    # claim (INSERT … ON CONFLICT) + result (UPDATE), committed together
    "R4": ["BEGIN", "INSERT", "UPDATE", "COMMIT"],
    # a repeat: the claim conflicts, the stored record is read and replayed
    "R4r": ["BEGIN", "INSERT", "SELECT", "ROLLBACK"],
}
#: Redis commands of ONE call, local (none: command dedupe lives in the
#: database).
EXPECTED_REDIS = {k: [] for k in METHODS}
#: ... and over RPC: a COMMAND call's nonce is also claimed in the shared
#: replay guard (``SET NX`` in the security cache, ``RPC_REPLAY_GUARD``
#: default ``commands``); reads use the per-process table only.
EXPECTED_REDIS_RPC = {k: ["SET"] if m.command else [] for k, m in METHODS.items()}
#: Layers that reach the provider (the client-only ones never do).
PROVIDER_LAYERS = ("loc", "srv", "rpc", "rpcn")


class Stand:
    def __init__(self, app: Any, core: Any, proxy: PgCountingProxy) -> None:
        self.app, self.core, self.proxy = app, core, proxy
        self.redis: list[str] = []

    def pg(self) -> list[str]:
        out = []
        for rt in self.proxy.reset():
            up = " ".join(rt.sql).upper()
            out.append(
                next(
                    (
                        k
                        for k in (
                            "BEGIN",
                            "COMMIT",
                            "ROLLBACK",
                            "INSERT",
                            "UPDATE",
                            "SELECT",
                        )
                        if k in up
                    ),
                    up[:40],
                )
            )
        return out


@pytest_asyncio.fixture
async def stand_env(
    sandloom_app: Any,
    sandloom_core: Any,
    db_engine: Any,
    pg_proxy: PgCountingProxy,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Stand]:
    """RPC keys, the diag contract, the seeded membership, a counting DB proxy."""
    # The stand's service keyed to call ITSELF (as ``rpcstand.server_env``).
    for name, value in rpc_settings(
        rpcstand.STAND_SERVICE,
        allowed_calls={rpcstand.STAND_SERVICE: [rpcstand.DIAG, rpcstand.MEMBERSHIP]},
    ).items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "rpc_auth_max_skew_seconds", 300)
    # a small srv pool (3 ops per test; the ladder's default is 40000)
    monkeypatch.setenv("RPC_STAND_COMMAND_POOL", "8")
    if sandloom_core.local_contract(rpcstand.DIAG) is None:
        sandloom_core.register_service(rpcstand.DIAG, RpcDiagContract)
    conn = await asyncpg.connect(_dsn(make_url(_url())))
    try:  # committed: the provider reads on its OWN contract pool
        await conn.execute(
            """INSERT INTO tenancy_organizations
                   (id, name, slug, owner_id, is_active, created_at, updated_at)
               VALUES ($1, 'Stand Org', 'stand-org', $2, true, now(), now())
               ON CONFLICT (id) DO NOTHING""",
            rpcstand.ORG,
            rpcstand.USER,
        )
        await conn.execute(
            """INSERT INTO tenancy_members (id, organization_id, user_id, role, created_at)
               VALUES (gen_random_uuid(), $1, $2, 'owner', now())
               ON CONFLICT (organization_id, user_id) DO NOTHING""",
            rpcstand.ORG,
            rpcstand.USER,
        )
    finally:
        await conn.close()
    import redis.asyncio.client as rc

    stand = Stand(sandloom_app, sandloom_core, pg_proxy)
    orig_exec = rc.Redis.execute_command

    async def counting(self: Any, *args: Any, **kw: Any) -> Any:
        stand.redis.append(str(args[0]).upper())
        return await orig_exec(self, *args, **kw)

    monkeypatch.setattr(rc.Redis, "execute_command", counting)
    yield stand


@asynccontextmanager
async def _backends(
    stand: Stand, monkeypatch: pytest.MonkeyPatch, *, planner_opt: bool
) -> AsyncIterator[None]:
    monkeypatch.setattr(settings, "planner_optimized_executor", planner_opt)
    url = make_url(_url()).set(host="127.0.0.1", port=stand.proxy.port)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            override_instance(
                stand.core,
                "database",
                driver="sqlalchemy",
                url=url.render_as_string(hide_password=False),
            )
        )
        await stack.enter_async_context(
            override_instance(
                stand.core, "cache", driver="redis", url=os.environ["CACHE_URL"]
            )
        )
        yield


async def _call(op: Any, layer: str, i: int) -> Any:
    got = await op(i)
    return rpcstand.decode_reply(got) if layer == "srv" else got


# --------------------------------------------------------------------------- #
# 1. Every rung x layer answers (with and without a tenant in the context)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("layer", LAYERS)
@pytest.mark.parametrize("rung", list(METHODS))
async def test_every_rung_answers(
    stand_env: Stand, monkeypatch: pytest.MonkeyPatch, rung: str, layer: str
) -> None:
    m = METHODS[rung]
    async with _backends(stand_env, monkeypatch, planner_opt=m.planner_opt):
        for tenant in (False, True):
            op, aclose = await rpcstand.build_layer(
                stand_env.app, stand_env.core, m, layer, tenant=tenant
            )
            try:
                for i in range(3):
                    assert await _call(op, layer, i) == "owner"
            finally:
                await aclose()


# --------------------------------------------------------------------------- #
# 2. Round trips per call: pinned, and identical local vs over RPC
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rung", list(METHODS))
async def test_round_trips_per_call(
    stand_env: Stand, monkeypatch: pytest.MonkeyPatch, rung: str
) -> None:
    m = METHODS[rung]
    async with _backends(stand_env, monkeypatch, planner_opt=m.planner_opt):
        for layer in LAYERS:
            op, aclose = await rpcstand.build_layer(
                stand_env.app, stand_env.core, m, layer
            )
            try:
                await _call(op, layer, 0)  # warm (describe, prepared statements)
                stand_env.proxy.reset()
                stand_env.redis.clear()
                await _call(op, layer, 1)
                pg, redis = stand_env.pg(), list(stand_env.redis)
            finally:
                await aclose()
            if layer in PROVIDER_LAYERS:
                assert pg == EXPECTED_PG[rung], (layer, pg)
                expected = EXPECTED_REDIS if layer == "loc" else EXPECTED_REDIS_RPC
                assert redis == expected[rung], (layer, redis)
            else:  # client only: nothing reaches a backend
                assert (pg, redis) == ([], []), (layer, pg, redis)


async def test_command_rung_dedupes_and_replay_rung_only_reads(
    stand_env: Stand, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = METHODS["R4"]
    async with _backends(stand_env, monkeypatch, planner_opt=False):
        op, aclose = await rpcstand.build_layer(
            stand_env.app,
            stand_env.core,
            m,
            "rpc",
            nonce=uuid.uuid4().hex,  # a fresh key space per run
        )
        try:
            stand_env.proxy.reset()
            stand_env.redis.clear()
            await op(7)
            assert stand_env.pg() == ["BEGIN", "INSERT", "UPDATE", "COMMIT"]
            await op(7)  # the same operation_key again: replayed, body not run
            assert stand_env.pg() == ["BEGIN", "INSERT", "SELECT", "ROLLBACK"]
            # the dedupe record is in the database; Redis holds only each
            # call's nonce (the shared replay guard of commands)
            assert stand_env.redis == ["SET", "SET"]
        finally:
            await aclose()


# --------------------------------------------------------------------------- #
# 3. Every call is authenticated; only the tenant rungs carry a tenant
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tenant", [False, True])
async def test_tenant_in_context_only_on_tenant_rungs(
    stand_env: Stand, monkeypatch: pytest.MonkeyPatch, tenant: bool
) -> None:
    import httpx

    from sandloom.core import rpc, rpc_binding, rpc_pair_mac
    from sandloom.core.tenant import bind_tenant

    seen: list[httpx.Request] = []

    async def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return await httpx.ASGITransport(app=stand_env.app).handle_async_request(
            request
        )

    async with (
        _backends(stand_env, monkeypatch, planner_opt=False),
        httpx.AsyncClient(
            transport=httpx.MockTransport(record), base_url=rpcstand.INPROC_URL
        ) as http,
    ):
        proxy = rpc.RemoteServiceProxy(
            service_name=rpcstand.DIAG,
            binding=rpcstand.binding(),
            default_timeout_seconds=5,
            client=http,
        )
        if tenant:
            with bind_tenant(rpcstand.ORG):
                assert await proxy.noop(rpcstand.ORG, rpcstand.USER) == "owner"
        else:
            assert await proxy.noop(rpcstand.ORG, rpcstand.USER) == "owner"
    post = [r for r in seen if r.method == "POST"][-1]
    auth = rpc_pair_mac.parse_header(post.headers[rpc_pair_mac.AUTH_HEADER])
    assert (auth["caller"], auth["aud"]) == (rpcstand.STAND_SERVICE,) * 2
    assert rpc_binding.NONCE_HEADER in post.headers
    context = rpc_pair_mac.decode_context(post.headers["x-sandloom-rpc-context"])
    assert context["tenant_id"] == (str(rpcstand.ORG) if tenant else None)
