"""Request-scoped read scope for LOCAL read contracts (shared connection + memo).

Inside a :class:`~sandloom.core.module_api.ContractReadScope` local read-contract
calls on one database share ONE contract-pool transaction (rolled back at the
end) and identical calls are memoised — without changing what a call
observes: it still never sees the caller's uncommitted writes (exactly like
over RPC), a command clears the memo, concurrent / nested calls fall back to
their own Unit of Work, and a failing call never poisons the next one.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import Table, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from sandloom.core.drivers.sqlalchemy_database import (
    SQLAlchemyDatabaseProvider,
    create_sqlalchemy_database,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.idempotency import cache_store as idempotency_cache_store
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.module_api import (
    ContractReadScopeMiddleware,
    contract_method,
    contract_read_scope,
    contract_session,
    current_contract_read_scope,
)
from sandloom.core.orm import Base
from sandloom.core.sql_alchemy.models.contract_command import ContractCommandRecord
from sandloom.core.testing import InMemoryCache
from sandloom.core.testing.workers import worker_env_values

TABLE = "sandloom_read_scope_probe"
SERVICE = "demo.scope.v1"


def _database_url() -> str:
    env = worker_env_values(Path(__file__).resolve().parents[4] / ".env.test")
    url = os.environ.get("DATABASE_URL") or env.get("DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL DATABASE_URL not configured")
    return url


class Probe:
    _calls = 0

    @classmethod
    @contract_method
    async def value(cls, key: UUID) -> int | None:
        cls._calls += 1
        row = await contract_session().execute(
            text(f"SELECT n FROM {TABLE} WHERE k = :k"), {"k": str(key)}
        )
        return row.scalar_one_or_none()

    @classmethod
    @contract_method
    async def backend_pid(cls) -> int:
        cls._calls += 1
        return int(
            (
                await contract_session().execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
        )

    @classmethod
    @contract_method
    async def boom(cls) -> int:
        await contract_session().execute(text("SELECT * FROM no_such_table_xyz"))
        return 0

    @classmethod
    @contract_method
    async def swallowed(cls) -> bool:
        # Catches its own SQL error: the transaction it ran in stays aborted.
        try:
            await contract_session().execute(text("SELECT * FROM no_such_table_xyz"))
        except DBAPIError:
            return False
        return True

    @classmethod
    @contract_method
    async def nested_pid(cls) -> list[int]:
        # A read contract calling another read contract on the same database
        # while it holds the shared connection: the inner one falls back.
        outer = int(
            (
                await contract_session().execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
        )
        inner = await _CORE[0].get_service(SERVICE).backend_pid()
        return [outer, inner]

    @classmethod
    @contract_method
    async def blocked(cls) -> int:
        # Holds the shared connection (a statement ran) until the barrier opens.
        pid = int(
            (
                await contract_session().execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
        )
        _BARRIER[0].set()
        await _BARRIER[1].wait()
        return pid

    @classmethod
    @contract_method(planned=True)
    async def pair(cls, key: UUID) -> list[str]:
        cls._calls += 1
        return [str(key), "x"]

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def bump(cls, key: UUID, operation_key: str) -> int:
        row = await contract_session().execute(
            text(f"UPDATE {TABLE} SET n = n + 1 WHERE k = :k RETURNING n"),
            {"k": str(key)},
        )
        return int(row.scalar_one())

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def blocked_insert(cls, key: UUID, operation_key: str) -> int:
        # Inserts, then pauses with its own transaction open.
        await contract_session().execute(
            text(f"INSERT INTO {TABLE} VALUES (:k, 1)"), {"k": str(key)}
        )
        _BARRIER[0].set()
        await _BARRIER[1].wait()
        return 1


_CORE: list[CoreFacade] = []
#: (the read entered and holds its connection, the read may finish)
_BARRIER: list[asyncio.Event] = []


@pytest_asyncio.fixture
async def provider(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[SQLAlchemyDatabaseProvider]:
    url = _database_url()
    mem = InMemoryCache()
    monkeypatch.setattr(idempotency_cache_store, "cache", lambda name="default": mem)
    db = create_sqlalchemy_database(
        InstanceConfig(
            capability="database",
            name="scope_probe",
            driver="sqlalchemy",
            options={"url": url},
        )
    )
    async with db.engine.begin() as conn:
        await conn.execute(
            text(f"CREATE TABLE IF NOT EXISTS {TABLE} (k text PRIMARY KEY, n int)")
        )
        # Commands dedupe through the provider's DURABLE command log
        # (``contract_command_log``, core chain), committed with the command —
        # not through the patched idempotency cache. Own that state: the table
        # must exist on a fresh database, and a record left by an earlier run
        # (same contract + operation key, other args digest, 24h TTL) would
        # turn the next run's command into a 409 conflict.
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[cast(Table, ContractCommandRecord.__table__)],
        )
        await _clear_command_log(conn)
    try:
        yield db
    finally:
        async with db.engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
            await _clear_command_log(conn)
        await db.aclose()


async def _clear_command_log(conn: AsyncConnection) -> None:
    await conn.execute(
        text("DELETE FROM contract_command_log WHERE contract LIKE :c"),
        {"c": f"{SERVICE}.%"},
    )


@pytest.fixture
def core(provider: SQLAlchemyDatabaseProvider) -> CoreFacade:
    facade = CoreFacade(app=FastAPI())
    facade.database = lambda name="default": provider  # type: ignore[method-assign]
    facade.register_service(SERVICE, Probe)
    _CORE[:] = [facade]
    Probe._calls = 0
    return facade


async def _seed(provider: SQLAlchemyDatabaseProvider, key: UUID, n: int) -> None:
    async with provider.engine.begin() as conn:
        await conn.execute(
            text(f"INSERT INTO {TABLE} VALUES (:k, :n)"), {"k": str(key), "n": n}
        )


@pytest.mark.asyncio
async def test_a_swallowed_sql_error_does_not_abort_later_calls(
    core: CoreFacade, provider: SQLAlchemyDatabaseProvider
) -> None:
    """A read contract that catches its own SQL error leaves the shared
    transaction aborted (SQLSTATE 25P02). A later call in the scope must not
    inherit it ("current transaction is aborted"): the scope drops that
    connection and the call runs in its own Unit of Work, as it would have
    without the scope; the calls after it share a fresh connection again."""
    key = uuid4()
    await _seed(provider, key, 7)
    svc = core.get_service(SERVICE)
    async with contract_read_scope(memo=False, idle_ms=-1) as scope:
        assert await svc.swallowed() is False
        assert await svc.value(key) == 7
        assert await svc.value(key) == 7
        assert await svc.backend_pid() == await svc.backend_pid()
    assert scope.stats["replaced"] == 1
    assert scope.stats["opened"] == 2


@pytest.mark.asyncio
async def test_calls_in_a_scope_share_one_connection(core: CoreFacade) -> None:
    svc = core.get_service(SERVICE)
    unscoped = {await svc.backend_pid() for _ in range(3)}
    async with contract_read_scope(memo=False, idle_ms=-1) as scope:
        scoped = {await svc.backend_pid() for _ in range(3)}
    assert len(scoped) == 1
    assert scope.stats["opened"] == 1 and scope.stats["reused"] == 2
    assert len(unscoped) >= 1  # (pooled: may or may not repeat)


@pytest.mark.asyncio
async def test_scope_never_sees_the_callers_uncommitted_writes(
    core: CoreFacade, provider: SQLAlchemyDatabaseProvider
) -> None:
    key = uuid4()
    svc = core.get_service(SERVICE)
    request_uow = core.get_uow_factory("default")()
    async with contract_read_scope(memo=False, idle_ms=-1):
        async with request_uow:
            await request_uow.session.execute(  # type: ignore[union-attr]
                text(f"INSERT INTO {TABLE} VALUES (:k, 1)"), {"k": str(key)}
            )
            # Uncommitted in the caller's transaction: invisible, as over RPC.
            assert await svc.value(key) is None
        # Committed now: READ COMMITTED — the shared transaction sees it.
        assert await svc.value(key) == 1


@pytest.mark.asyncio
async def test_memo_serves_repeats_and_a_command_invalidates_it(
    core: CoreFacade, provider: SQLAlchemyDatabaseProvider
) -> None:
    key = uuid4()
    await _seed(provider, key, 1)
    svc = core.get_service(SERVICE)
    async with contract_read_scope(idle_ms=-1) as scope:
        assert await svc.value(key) == 1
        assert await svc.value(key) == 1
        assert Probe._calls == 1 and scope.stats["memo_hits"] == 1
        assert await svc.bump(key, operation_key="op-1") == 2
        # Read-after-write: never a stale memoised value.
        assert await svc.value(key) == 2
        assert Probe._calls == 2
        assert await svc.value(uuid4()) is None  # other args: a real call
        assert Probe._calls == 3


@pytest.mark.asyncio
async def test_memo_hits_are_independent_copies(core: CoreFacade) -> None:
    key = uuid4()
    svc = core.get_service(SERVICE)
    async with contract_read_scope(idle_ms=-1) as scope:
        first = await svc.pair(key)
        first.append("mutated by the first caller")
        again = await svc.pair(key)
        third = await svc.pair(key)
        assert again == third == [str(key), "x"]
        assert again is not third
        again.clear()
        assert await svc.pair(key) == [str(key), "x"]
        assert Probe._calls == 1 and scope.stats["memo_hits"] == 3


@pytest.mark.asyncio
async def test_memo_is_keyed_by_bound_tenant(core: CoreFacade) -> None:
    from sandloom.core.tenant import bind_tenant

    svc = core.get_service(SERVICE)
    async with contract_read_scope(idle_ms=-1):
        with bind_tenant(uuid4()):
            await svc.backend_pid()
        with bind_tenant(uuid4()):
            await svc.backend_pid()
    assert Probe._calls == 2


@pytest.mark.asyncio
async def test_failed_call_discards_the_shared_transaction(core: CoreFacade) -> None:
    svc = core.get_service(SERVICE)
    async with contract_read_scope(memo=False, idle_ms=-1) as scope:
        await svc.backend_pid()
        with pytest.raises(Exception, match="no_such_table_xyz"):
            await svc.boom()
        # The aborted transaction was dropped: the next call works.
        assert isinstance(await svc.backend_pid(), int)
    assert scope.stats["opened"] == 2


@pytest.mark.asyncio
async def test_nested_and_concurrent_calls_fall_back_to_their_own_uow(
    core: CoreFacade,
) -> None:
    svc = core.get_service(SERVICE)
    async with contract_read_scope(memo=False, idle_ms=-1) as scope:
        outer, inner = await svc.nested_pid()
        assert outer != inner
        pids = await asyncio.gather(*(svc.backend_pid() for _ in range(3)))
    assert all(isinstance(p, int) for p in pids)
    assert scope.stats["fallback"] >= 1


@pytest.mark.asyncio
async def test_idle_connection_is_returned_to_the_pool(core: CoreFacade) -> None:
    svc = core.get_service(SERVICE)
    async with contract_read_scope(memo=False, idle_ms=1) as scope:
        await svc.backend_pid()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if scope.stats["released_idle"]:
                break
        assert scope.stats["released_idle"] == 1
        await svc.backend_pid()
    assert scope.stats["opened"] == 2


@pytest.mark.asyncio
async def test_default_release_on_next_loop_turn(core: CoreFacade) -> None:
    svc = core.get_service(SERVICE)
    async with contract_read_scope(memo=False, idle_ms=0) as scope:
        first = await svc.backend_pid()
        second = await svc.backend_pid()  # back-to-back: shared
        assert first == second and scope.stats["opened"] == 1
        await asyncio.sleep(0)  # the request awaits something else...
        assert scope.stats["released_idle"] == 1  # ...the connection went back
        await svc.backend_pid()
    assert scope.stats["opened"] == 2


@pytest.mark.asyncio
async def test_scope_end_rolls_back_and_releases(
    core: CoreFacade, provider: SQLAlchemyDatabaseProvider
) -> None:
    svc = core.get_service(SERVICE)
    await svc.backend_pid()  # build the contract engine
    engine = provider._contract_engine
    assert engine is not None
    rollbacks: list[int] = []

    def on_rollback(conn: object) -> None:
        rollbacks.append(1)

    event.listen(engine.sync_engine, "rollback", on_rollback)
    try:
        async with contract_read_scope(memo=False, idle_ms=-1):
            await svc.backend_pid()
            await svc.backend_pid()
            assert rollbacks == []
        assert rollbacks == [1]
        assert engine.sync_engine.pool.checkedout() == 0
    finally:
        event.remove(engine.sync_engine, "rollback", on_rollback)


@pytest.mark.asyncio
async def test_middleware_opens_one_scope_per_request(
    core: CoreFacade, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "contract_read_scope_idle_ms", 0)
    app = FastAPI()
    seen: list[object] = []

    @app.get("/two")
    async def two() -> dict[str, int]:
        svc = core.get_service(SERVICE)
        a, b = await svc.backend_pid(), await svc.backend_pid()
        seen.append(current_contract_read_scope())
        return {"same": int(a == b)}

    app.add_middleware(ContractReadScopeMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        first = await client.get("/two")
        second = await client.get("/two")
    assert first.json() == second.json() == {"same": 1}
    assert seen[0] is not None and seen[0] is not seen[1]
    assert current_contract_read_scope() is None


def test_install_service_rpc_routes_wires_the_read_scope() -> None:
    from sandloom.core.http.request_authority import RequestAuthorityMiddleware
    from sandloom.core.rpc import RpcFastPathMiddleware, install_service_rpc_routes

    app = FastAPI()
    install_service_rpc_routes(app, CoreFacade(app=app))
    classes = [m.cls for m in app.user_middleware]
    # The HTTP authority boundary must also wrap the fast RPC path.
    assert classes[:3] == [
        RequestAuthorityMiddleware,
        RpcFastPathMiddleware,
        ContractReadScopeMiddleware,
    ]


async def test_unused_scope_allocates_nothing_and_closes_at_once() -> None:
    """Most requests make no local read call: the per-request scope then
    builds no units / closing set / counters and closes without awaiting."""
    from sandloom.core import module_api
    from sandloom.core.module_api import _read_scope as module_api_read_scope

    scope = module_api.ContractReadScope()
    assert scope._units is module_api_read_scope._NO_UNITS
    assert scope._closing is None and scope._stats is None
    scope.memo_set(("k",), 1)
    assert scope.memo_get(("k",)) == 1
    await scope.aclose()
    assert scope.memo_get(("k",)) is module_api_read_scope._MEMO_MISS
    assert scope.stats["opened"] == 0 and scope.stats["memo_hits"] == 1
    assert module_api_read_scope._NO_UNITS == {}


@pytest.mark.asyncio
async def test_an_intent_window_waits_for_a_running_read_and_closes_its_unit(
    core: CoreFacade, provider: SQLAlchemyDatabaseProvider
) -> None:
    """G-UNIT-02 / G-BILLING-19: external_call() entered while another task's
    read contract holds the scope's shared connection waits for that read to
    finish; the external call never runs beside a checked-out connection, and
    the read's unit is closed (rolled back, returned to the pool)."""
    from sandloom.core.unit_of_work.intent import IntentState, external_call
    from sandloom.core.unit_of_work.request import RequestScope, bind_request_scope

    svc = core.get_service(SERVICE)
    await svc.backend_pid()  # build the contract engine
    engine = provider._contract_engine
    assert engine is not None
    _BARRIER[:] = [asyncio.Event(), asyncio.Event()]
    seen: dict[str, object] = {}
    request = RequestScope(core=None, policy=None)
    intent = request.intent = IntentState()
    with bind_request_scope(request):
        async with contract_read_scope(memo=False, idle_ms=-1) as scope:
            intent.read_scope_release = scope.release_connections
            read = asyncio.create_task(svc.blocked())
            await _BARRIER[0].wait()
            assert engine.sync_engine.pool.checkedout() == 1

            async def psp_call() -> None:
                async with external_call("psp.create"):
                    seen["read_done"] = read.done()
                    seen["checked_out"] = engine.sync_engine.pool.checkedout()

            window = asyncio.create_task(psp_call())
            for _ in range(20):  # the window is entered and waiting
                await asyncio.sleep(0)
            assert not window.done() and "read_done" not in seen
            _BARRIER[1].set()
            await window
            assert seen == {"read_done": True, "checked_out": 0}
            assert isinstance(await read, int)
            assert scope._units == {}
        assert engine.sync_engine.pool.checkedout() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["external_call", "release_check"])
async def test_an_intent_window_never_runs_beside_a_running_provider_unit(
    core: CoreFacade, provider: SQLAlchemyDatabaseProvider, window: str
) -> None:
    """G-UNIT-02 / G-BILLING-19, both directions: a child command that
    inserted and paused with its transaction open is waited for before the
    window opens (nothing checked out, its row committed); a command started
    while the window is open is refused (SANDLOOM-R008)."""
    from sandloom.core.unit_of_work.intent import (
        IntentState,
        ResolvedAccount,
        external_call,
        release_check,
    )
    from sandloom.core.unit_of_work.request import (
        RequestScope,
        UnitRuleError,
        bind_request_scope,
    )

    svc = core.get_service(SERVICE)
    await svc.backend_pid()  # build the contract engine
    engine = provider._contract_engine
    assert engine is not None
    _BARRIER[:] = [asyncio.Event(), asyncio.Event()]
    seen: dict[str, object] = {}
    key = uuid4()
    request = RequestScope(core=None, policy=None)
    request.intent = IntentState()

    async def in_window() -> None:
        seen["command_done"] = command.done()
        seen["checked_out"] = engine.sync_engine.pool.checkedout()
        with pytest.raises(UnitRuleError, match="SANDLOOM-R008"):
            await svc.bump(key=key, operation_key=f"late-{window}")

    async def enter_window() -> None:
        if window == "external_call":
            async with external_call("psp.create"):
                await in_window()
        else:
            await release_check(
                ResolvedAccount(kind="org", id=uuid4()), authority=in_window
            )

    with bind_request_scope(request):
        command = asyncio.create_task(
            svc.blocked_insert(key=key, operation_key=f"child-{window}")
        )
        await _BARRIER[0].wait()
        assert engine.sync_engine.pool.checkedout() >= 1
        entered = asyncio.create_task(enter_window())
        for _ in range(20):  # the window is entered and waiting
            await asyncio.sleep(0)
        assert "command_done" not in seen  # never beside the open transaction
        _BARRIER[1].set()
        await entered
        assert await command == 1
    assert seen == {"command_done": True, "checked_out": 0}


@pytest.mark.asyncio
async def test_an_intent_window_inside_a_running_read_is_refused() -> None:
    """A window entered from inside the read holding the shared connection
    cannot wait for it: refused (SANDLOOM-R008), never entered beside it."""
    from sandloom.core import module_api
    from sandloom.core.module_api import _context
    from sandloom.core.module_api._read_scope import _ScopedUnit
    from sandloom.core.unit_of_work.request import UnitRuleError

    scope = module_api.ContractReadScope()
    scoped = _ScopedUnit()
    scoped.session = object()
    scope._units = {(0, "default"): scoped}
    token = _context._contract_session_var.set(scoped.session)
    try:
        with pytest.raises(UnitRuleError, match="SANDLOOM-R008"):
            await scope.release_connections()
    finally:
        _context._contract_session_var.reset(token)
