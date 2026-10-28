# ruff: noqa: F811 - the shared ``s5`` fixture is imported and requested by name
"""Write-path stand (W1-W6, DIAGNOSTIC ONLY): correctness, atomicity, budgets.

Runs on the S5 environment (FORCEd RLS, NOBYPASSRLS app role, request pool of
ONE, wire-protocol counting proxy) with the stand's routes
(``benchsuite.bench.api.diag_write``) mounted:

* every rung writes the right rows for the bound tenant only;
* atomicity: outbox / audit rows and the on_commit callback commit iff the
  change commits — a failure after every write leaves NOTHING behind;
* W4 updates by PK in the caller's tenant only (cross-tenant -> 404);
* W6 (command contract) is deduplicated on its operation key;
* the protocol-level round trips (PostgreSQL + Redis) of every rung are
  pinned: they are the denominators of the per-op CPU numbers.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from test_s5_secure_read import (  # noqa: F401 - fixtures
    ROLE,
    S5,
    A,
    B,
    admin,
    headers,
    merged_tx,
    pg_proxy,
    s5,
)

from sandloom.core.http.route_runtime import install_route_runtime
from sandloom.core.unit_of_work.dispatch import drain_post_commit
from benchsuite import common
from benchsuite.bench import write_diag
from benchsuite.bench.api.diag_write import diag_write_router
from benchsuite.setup_db import (
    WRITE_COUNTERS_PER_TENANT,
    WRITE_DIAG_TABLES,
    write_statements,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.parametrize("s5", ["uow"], indirect=True),
]

A_COUNTER = 7  # owned by tenant A
B_COUNTER = WRITE_COUNTERS_PER_TENANT + 7  # owned by tenant B
COUNTED = (*WRITE_DIAG_TABLES, "outbox_messages", "audit_log")
#: rung -> (path, body, status, table that gains a row, outbox+1, audit+1,
#: on_commit+1)
INSERTS = {
    "W1": ("/dw/1", {"name": "w", "value": 1}, "bench_tenant_writes", 0, 0, 0),
    "W2": ("/dw/2", {"name": "w", "value": 2}, "bench_tenant_writes", 1, 0, 0),
    "W3": ("/dw/3", {"name": "w", "value": 3}, "bench_tenant_writes", 1, 1, 0),
    "W5": ("/dw/5", {"name": "w", "value": 5}, "bench_tenant_orders", 1, 1, 1),
    "W6": ("/dw/6", {"name": "w", "value": 6}, "bench_tenant_orders", 1, 1, 1),
}


class W:
    def __init__(self, s5: S5) -> None:
        self.s5 = s5

    async def post(self, path: str, body: dict[str, Any] | None = None, **kw: Any):
        body = dict(body or {})
        if path.startswith("/dw/6"):
            body.setdefault("key", f"op-{uuid.uuid4().hex}")
        resp = await self.s5.http.post(path, json=body, **kw)
        await drain_post_commit()  # the deferred outbox dispatch belongs to it
        return resp

    async def counts(self) -> dict[str, int]:
        return {
            t: int(await self.s5.admin.fetchval(f"SELECT count(*) FROM {t}"))
            for t in COUNTED
        }

    async def counter(self, counter_id: int) -> int:
        return int(
            await self.s5.admin.fetchval(
                "SELECT value FROM bench_tenant_counters WHERE id = $1", counter_id
            )
        )


@pytest_asyncio.fixture
async def w(s5: S5, sandloom_app: Any, sandloom_core: Any) -> AsyncIterator[W]:
    # The harness may have created the stand's tables from the ORM metadata
    # WITHOUT RLS: rebuild them exactly as benchsuite.setup_db does.
    await s5.admin.execute(
        "DROP TABLE IF EXISTS " + ", ".join(WRITE_DIAG_TABLES) + " CASCADE"
    )
    for sql in write_statements(ROLE):
        await s5.admin.execute(sql)
    await s5.admin.execute("TRUNCATE outbox_messages, audit_log")
    if sandloom_core.local_contract(write_diag.SERVICE) is None:
        sandloom_core.register_service(write_diag.SERVICE, write_diag.WriteDiagContract)
    sandloom_app.include_router(diag_write_router)  # the s5 fixture restores routes
    # routes added after create_app: the route runtime (model E) as in the app
    install_route_runtime(sandloom_app.routes, sandloom_core)
    write_diag.ON_COMMIT_RUNS["count"] = 0
    yield W(s5)


# --------------------------------------------------------------------------- #
# 1. Every rung writes the right rows, for the bound tenant only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rung", list(INSERTS))
async def test_insert_rungs_write_for_their_tenant(w: W, rung: str) -> None:
    path, body, table, outbox, audit, committed = INSERTS[rung]
    before = await w.counts()
    r = await w.post(path, body, headers=headers(A))
    assert r.status_code == 201, r.text
    new_id = r.json()["id"]
    after = await w.counts()
    assert after[table] == before[table] + 1
    assert after["outbox_messages"] == before["outbox_messages"] + outbox
    assert after["audit_log"] == before["audit_log"] + audit
    assert write_diag.ON_COMMIT_RUNS["count"] == committed
    owner = await w.s5.admin.fetchval(
        f"SELECT tenant_id FROM {table} WHERE id = $1", new_id
    )
    assert owner == A
    if table == "bench_tenant_orders":  # the line: same tenant, same order
        line = await w.s5.admin.fetchrow(
            "SELECT tenant_id, order_id FROM bench_tenant_order_lines "
            "WHERE order_id = $1",
            new_id,
        )
        assert line is not None and line["tenant_id"] == A
    if outbox:  # the post-commit dispatch delivered it
        delivered = await w.s5.admin.fetchval(
            "SELECT count(*) FROM outbox_messages WHERE delivered_at IS NOT NULL"
        )
        assert delivered == after["outbox_messages"]


async def test_auth_stack_guards_every_rung(w: W) -> None:
    before = await w.counts()
    for path in ("/dw/1", "/dw/3", "/dw/4c/1", "/dw/5", "/dw/6"):
        assert (await w.post(path, {"name": "x", "value": 1})).status_code == 401
        canceled = headers(A, subscription_status="canceled")
        r = await w.post(path, {"name": "x", "value": 1}, headers=canceled)
        assert r.status_code == 402
    assert await w.counts() == before


# --------------------------------------------------------------------------- #
# 2. Atomicity: a failure after every write leaves nothing behind
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rung", list(INSERTS))
async def test_failure_after_the_writes_commits_nothing(w: W, rung: str) -> None:
    path, body, *_ = INSERTS[rung]
    before = await w.counts()
    r = await w.post(f"{path}?fail=true", body, headers=headers(A))
    assert r.status_code == 409, r.text
    assert await w.counts() == before
    assert write_diag.ON_COMMIT_RUNS["count"] == 0
    # ... and the next request on the SAME pooled connection is clean
    r = await w.post(path, body, headers=headers(A))
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("route", ["4o", "4c"])
async def test_update_rungs_are_atomic_and_tenant_scoped(w: W, route: str) -> None:
    start = await w.counter(A_COUNTER)
    r = await w.post(f"/dw/{route}/{A_COUNTER}", headers=headers(A))
    assert r.status_code == 200 and r.json() == {"value": start + 1}
    assert await w.counter(A_COUNTER) == start + 1
    r = await w.post(f"/dw/{route}/{A_COUNTER}?fail=true", headers=headers(A))
    assert r.status_code == 409
    assert await w.counter(A_COUNTER) == start + 1  # rolled back
    b_start = await w.counter(B_COUNTER)
    r = await w.post(f"/dw/{route}/{B_COUNTER}", headers=headers(A))
    assert r.status_code == 404
    assert await w.counter(B_COUNTER) == b_start
    r = await w.post(f"/dw/{route}/{B_COUNTER}", headers=headers(B))
    assert r.status_code == 200 and await w.counter(B_COUNTER) == b_start + 1


async def test_rls_alone_rejects_a_foreign_tenant_insert(w: W) -> None:
    """WITH CHECK: under tenant A's GUC the app role cannot write B's row."""
    await w.s5.admin.execute(f"SET ROLE {ROLE}")
    try:
        async with w.s5.admin.transaction():
            await w.s5.admin.execute(
                "SELECT set_config('sandloom.tenant_id', $1, true)", str(A)
            )
            with pytest.raises(Exception, match="row-level security"):
                async with w.s5.admin.transaction():
                    await w.s5.admin.execute(
                        "INSERT INTO bench_tenant_writes (tenant_id, name, value) "
                        "VALUES ($1, 'x', 1)",
                        B,
                    )
    finally:
        await w.s5.admin.execute("RESET ROLE")


# --------------------------------------------------------------------------- #
# 3. W6: the command contract is deduplicated on its operation key
# --------------------------------------------------------------------------- #
async def test_w6_is_deduplicated_on_its_operation_key(w: W) -> None:
    body = {"name": "dup", "value": 9, "key": f"same-{uuid.uuid4().hex}"}
    h = headers(A)  # the same user: the actor is one of the arguments
    first = await w.post("/dw/6", body, headers=h)
    before = await w.counts()
    again = await w.post("/dw/6", body, headers=h)
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()  # replayed, not re-executed
    assert await w.counts() == before
    assert write_diag.ON_COMMIT_RUNS["count"] == 1
    other = await w.post("/dw/6", {**body, "value": 10}, headers=h)
    assert other.status_code == 409  # same key, different arguments


# --------------------------------------------------------------------------- #
# 4. Protocol-level round trips per rung (after the post-commit dispatch)
# --------------------------------------------------------------------------- #
_TX = ["BEGIN", "SET_CONFIG"]
_MARK_DELIVERED = ["UPDATE"]  # after commit: ONE autocommit statement
#: audit + outbox rows of one flush: ONE statement (sandloom.core.sql_alchemy.effect_rows)
_FUSED = "AUDIT+OUTBOX"
EXPECTED_PG = {
    "/dw/1": [*_TX, "INSERT", "COMMIT"],
    "/dw/2": [*_TX, "INSERT", "INSERT", "COMMIT", *_MARK_DELIVERED],
    "/dw/3": [*_TX, "INSERT", _FUSED, "COMMIT", *_MARK_DELIVERED],
    f"/dw/4o/{A_COUNTER}": [*_TX, "SELECT", "UPDATE", "COMMIT"],
    f"/dw/4c/{A_COUNTER}": [*_TX, "UPDATE", "COMMIT"],
    "/dw/5": [*_TX, "INSERT", "INSERT", _FUSED, "COMMIT", *_MARK_DELIVERED],
    # W5 + the durable command record: claim INSERT first, result UPDATE
    # after the body (same transaction)
    "/dw/6": [
        *_TX,
        "INSERT",
        "INSERT",
        "INSERT",
        "UPDATE",
        _FUSED,
        "COMMIT",
        *_MARK_DELIVERED,
    ],
}
#: Redis commands per request: the request-start check of the subject-state
#: record (GET), the commit-time re-check of the same record before COMMIT
#: (GET, ADR-0009 / spec §4.5: every unit committing a write); XADD = the bus
#: dispatch; W6's command dedupe is in the database, not Redis
EXPECTED_REDIS = {
    "/dw/1": ["GET", "GET"],
    "/dw/2": ["GET", "GET", "XADD"],
    "/dw/3": ["GET", "GET", "XADD"],
    f"/dw/4o/{A_COUNTER}": ["GET", "GET"],
    f"/dw/4c/{A_COUNTER}": ["GET", "GET"],
    "/dw/5": ["GET", "GET", "XADD"],
    "/dw/6": ["GET", "GET", "XADD"],
}


@pytest.fixture
def redis_sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import redis.asyncio.client as rc

    sent: list[str] = []
    orig_exec, orig_pipe = rc.Redis.execute_command, rc.Pipeline.execute

    async def counting(self: Any, *args: Any, **kw: Any) -> Any:
        sent.append(str(args[0]).upper())
        return await orig_exec(self, *args, **kw)

    async def counting_pipe(self: Any, *args: Any, **kw: Any) -> Any:
        sent.append(f"PIPELINE[{len(self.command_stack)}]")
        return await orig_pipe(self, *args, **kw)

    monkeypatch.setattr(rc.Redis, "execute_command", counting)
    monkeypatch.setattr(rc.Pipeline, "execute", counting_pipe)
    return sent


@pytest.mark.parametrize("path", list(EXPECTED_PG))
async def test_round_trips_per_rung(w: W, path: str, redis_sent: list[str]) -> None:
    body = {"name": "rt", "value": 1}
    assert (await w.post(path, body, headers=headers(A))).status_code in (200, 201)
    w.s5.proxy.reset()
    redis_sent.clear()
    assert (await w.post(path, body, headers=headers(A))).status_code in (200, 201)
    trace = w.s5.proxy.reset()
    expected = merged_tx(EXPECTED_PG[path])
    assert _kinds(trace) == expected, "\n".join(rt.label() for rt in trace)
    # the bus dispatch is an XADD on the Redis Streams bus (prod; ladder rt) and
    # in-process on the test harness's memory bus: compare the rest
    assert [c for c in redis_sent if c != "XADD"] == [
        c for c in EXPECTED_REDIS[path] if c != "XADD"
    ], redis_sent


def _kinds(trace: list[Any]) -> list[str]:
    out = []
    for rt in trace:
        up = " ".join(rt.sql).upper()
        if up.startswith("B:WITH AUDIT AS (INSERT"):
            out.append(_FUSED)
            continue
        if up.startswith("B:UPDATE OUTBOX_MESSAGES") and "SYNCHRONOUS_COMMIT" in up:
            out.append("UPDATE")  # mark-delivered (async commit, one statement)
            continue
        if "SET_CONFIG" in up and up.startswith("BEGIN"):
            out.append("BEGIN+SET_CONFIG")  # DB_RLS_MERGED_BEGIN
            continue
        if "SET_CONFIG" in up:
            out.append("SET_CONFIG")
            continue
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
                    if k in up.split(" ", 1)[0] or up.startswith(f"B:{k}")
                ),
                up[:40],
            )
        )
    return out


async def test_the_seed_matches_the_stand(w: W) -> None:
    n = WRITE_COUNTERS_PER_TENANT
    rows = await w.s5.admin.fetchval("SELECT count(*) FROM bench_tenant_counters")
    assert rows == n * len(common.S5_TENANTS)


# --------------------------------------------------------------------------- #
# 5. The background relay on the RLS database (NOT on the request path)
# --------------------------------------------------------------------------- #
#: sweep (dead_letter_exhausted) | claim batch | settle batch, each its own
#: transaction; the allowlisted relay statements send NO tenant setting
EXPECTED_RELAY = ["BEGIN", "UPDATE", "COMMIT"] * 3


async def test_relay_round_trips_on_the_rls_database(w: W, sandloom_core: Any) -> None:
    from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_outbox_store
    from sandloom.core.tasks import relay

    class _Bus:
        def __init__(self) -> None:
            self.published: list[str] = []

        async def publish_event(self, *_a: Any, event_id: str, **_k: Any) -> None:
            self.published.append(event_id)

    async def seed(n: int) -> list[uuid.UUID]:
        ids = [uuid.uuid4() for _ in range(n)]
        for i in ids:  # left undelivered (a crashed dispatcher), past the grace
            await w.s5.admin.execute(
                "INSERT INTO outbox_messages (id, channel, schema_version,"
                " payload, emitted_at, delivery_attempts) VALUES ($1,"
                " 'bench.w.v1', 'v1', '{}', now() - interval '1 hour', 0)",
                i,
            )
        return ids

    factory = sandloom_core.get_uow_factory("default")
    bus = _Bus()
    await seed(2)  # warm-up: first connect + prepared statements
    await relay._relay_batch_for_store(
        factory, sql_alchemy_outbox_store(), bus, sweep=True
    )
    bus.published.clear()
    ids = await seed(5)
    w.s5.proxy.reset()
    await relay._relay_batch_for_store(
        factory, sql_alchemy_outbox_store(), bus, sweep=True
    )
    trace = w.s5.proxy.reset()
    kinds = ["UPDATE" if k.startswith("B:WITH") else k for k in _kinds(trace)]
    assert kinds == EXPECTED_RELAY, "\n".join(rt.label() for rt in trace)
    assert sorted(bus.published) == sorted(str(i) for i in ids)
    left = await w.s5.admin.fetchval(
        "SELECT count(*) FROM outbox_messages WHERE delivered_at IS NULL"
    )
    assert left == 0
