"""A pooled connection whose backend died while it sat idle never causes a 500.

Real PostgreSQL, pool of ONE, the idle ping effectively off (the bug lives in
the window below ``DB_POOL_PING_IDLE_SECONDS``), for the plain path and
``tenant_rls`` with merged ``BEGIN; set_config`` ON and OFF.

Connection RECOVERY happens only when provably nothing reached the server:

* the backend was killed and asyncpg processed the close (``is_closed()``):
  the checkout replaces the connection (no round trip);
* asyncpg read the FATAL but not the close yet (``ERROR_CONSUME``: the next
  message is refused with ``cannot switch to state N; another operation (2) is
  in progress`` before a byte is written). Reproduced exactly by feeding the
  protocol the ``57P01`` ErrorResponse a ``pg_terminate_backend`` sends.

Anything that reached a dying connection is NEVER retried: it is a 503
(:class:`DatabaseUnavailable`) with the transaction rolled back. The fault
matrix (kill before SQL sent / during the first statement / after the INSERT
before COMMIT / during COMMIT) checks business, outbox and audit rows.
"""

from __future__ import annotations

import asyncio
import os
import struct
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from sandloom.core.config.settings import settings
from sandloom.core.di import publish_event, record_audit
from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.exceptions import DatabaseUnavailable
from sandloom.core.http.error_handlers import http_status_of
from sandloom.core.observability.metrics import db_dead_connection_replaced_total
from sandloom.core.orm import Base
from sandloom.core.sql_alchemy.dead_connection import (
    is_dead_connection_error,
    never_sent,
)
from sandloom.core.sql_alchemy.models.audit import AuditLogEntry
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.planner_adapter import SQLAlchemyExecutionAdapter
from sandloom.core.sql_alchemy.tenant_rls import install_tenant_rls
from sandloom.core.testing import (
    committing_uow,
    register_event_schemas,
    reload_test_env,
)

pytestmark = pytest.mark.asyncio

KINDS = ("plain", "rls_merged", "rls_unmerged")
_PHASE = {"plain": "begin", "rls_merged": "merged_begin", "rls_unmerged": "tenant_sync"}
CHANNEL = "deadconn.order.created.v1"
ACTION = "deadconn.order.created"


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest.fixture(autouse=True)
def _channel_schema() -> None:
    register_event_schemas([CHANNEL])


def _url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url or not url.startswith("postgresql+asyncpg"):
        pytest.skip("needs the PostgreSQL test DATABASE_URL")
    return url


def _fatal() -> bytes:
    """The ErrorResponse a backend sends when ``pg_terminate_backend`` hits it."""
    fields = (
        b"SFATAL\0VFATAL\0C57P01\0"
        b"Mterminating connection due to administrator command\0\0"
    )
    return b"E" + struct.pack("!i", len(fields) + 4) + fields


def _engine(monkeypatch: pytest.MonkeyPatch, kind: str) -> AsyncEngine:
    monkeypatch.setattr(settings, "db_pool_ping_idle_seconds", 3600.0)
    engine = driver._pooled_engine(_url(), pool_size=1, max_overflow=0)
    if kind != "plain":
        install_tenant_rls(engine, merged_begin=kind == "rls_merged")
    return engine


@pytest_asyncio.fixture
async def admin() -> AsyncIterator[AsyncEngine]:
    engine = driver.build_disposable_engine(_url())
    yield engine
    await engine.dispose()


async def _pooled_raw(engine: AsyncEngine) -> Any:
    """The asyncpg connection idling in the (size-1) pool, and its pid."""
    async with engine.connect() as conn:
        raw = (await conn.get_raw_connection()).driver_connection
        pid = int((await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one())
        await conn.rollback()
    return raw, pid


def _poison(raw: Any) -> None:
    """asyncpg reads the FATAL of a killed backend; the close is still in flight."""
    raw._protocol.data_received(_fatal())


async def _request_pid(engine: AsyncEngine) -> int:
    """One 'request': a Unit-of-Work-shaped session transaction."""
    async with AsyncSession(engine) as session, session.begin():
        return int(
            (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )


def _replaced(phase: str) -> float:
    return float(db_dead_connection_replaced_total.labels(phase=phase)._value.get())


def _all_replaced() -> dict[str, float]:
    return {p: _replaced(p) for p in (*_PHASE.values(), "checkout", "planner")}


async def _terminate(admin: AsyncEngine, pid: int) -> None:
    async with admin.connect() as conn:
        await conn.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
        await conn.commit()
    for _ in range(100):
        async with admin.connect() as conn:
            alive = (
                await conn.execute(
                    text("SELECT count(*) FROM pg_stat_activity WHERE pid = :p"),
                    {"p": pid},
                )
            ).scalar_one()
        if not alive:
            return
        await asyncio.sleep(0.02)


async def _wait_closed(raw: Any) -> None:
    for _ in range(200):
        if raw.is_closed():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("asyncpg never noticed the killed backend")


# --------------------------------------------------------------- recovery


@pytest.mark.parametrize("kind", KINDS)
async def test_dead_before_first_message_sent_is_recovered(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    engine = _engine(monkeypatch, kind)
    try:
        raw, first = await _pooled_raw(engine)
        _poison(raw)
        assert not raw.is_closed()  # the checkout cannot see it: no round trip
        before = _replaced(_PHASE[kind])
        second = await _request_pid(engine)  # was a 500
        assert second != first
        assert _replaced(_PHASE[kind]) == before + 1
        assert raw.is_closed()  # the dead connection was discarded
        # The pool is healthy: the next request reuses the new connection.
        assert await _request_pid(engine) == second
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()


@pytest.mark.parametrize("kind", KINDS)
async def test_backend_killed_while_idle_is_replaced_at_checkout(
    monkeypatch: pytest.MonkeyPatch, admin: AsyncEngine, kind: str
) -> None:
    engine = _engine(monkeypatch, kind)
    try:
        raw, first = await _pooled_raw(engine)
        await _terminate(admin, first)
        await _wait_closed(raw)
        before = _replaced("checkout")
        second = await _request_pid(engine)
        assert second != first
        assert _replaced("checkout") == before + 1
    finally:
        await engine.dispose()


async def test_recovery_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "db_dead_connection_retry", False)
    engine = _engine(monkeypatch, "plain")
    try:
        raw, first = await _pooled_raw(engine)
        _poison(raw)
        with pytest.raises(DatabaseUnavailable):  # a 503, never a 500
            await _request_pid(engine)
        assert await _request_pid(engine) != first
    finally:
        await engine.dispose()


async def test_autocommit_statement_never_sent_is_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch, "plain")
    try:
        raw, first = await _pooled_raw(engine)
        _poison(raw)
        async with engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            pid = (await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        assert pid != first
    finally:
        await engine.dispose()


@pytest.mark.parametrize("method", ["execute", "fetchval"])
async def test_planner_lease_first_message_is_recovered(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    engine = _engine(monkeypatch, "plain")
    try:
        raw, first = await _pooled_raw(engine)
        adapter = SQLAlchemyExecutionAdapter(engine)
        before = _replaced("planner")
        lease = adapter.lease()
        async with lease as (_conn, leased):
            assert leased is raw
            _poison(leased)
            if method == "execute":
                live = await lease.first("execute", "BEGIN READ ONLY")
                pid = await live.fetchval("SELECT pg_backend_pid()")
                await live.execute("COMMIT")
            else:
                pid = await lease.first("fetchval", "SELECT pg_backend_pid()")
        assert pid != first
        assert _replaced("planner") == before + 1
        assert await _request_pid(engine) == pid
    finally:
        await engine.dispose()


# ----------------------------------------------- never retried: a clean 503


@pytest.mark.parametrize("kind", KINDS)
async def test_dead_after_a_statement_ran_is_a_503_not_a_retry(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    engine = _engine(monkeypatch, kind)
    try:
        raw, first = await _pooled_raw(engine)
        before = _all_replaced()
        with pytest.raises(DatabaseUnavailable) as caught:
            async with AsyncSession(engine) as session, session.begin():
                await session.execute(text("SELECT 1"))  # the transaction ran
                _poison(raw)
                await session.execute(text("SELECT 2"))  # refused, NOT re-sent
        assert http_status_of(caught.value) == 503
        # chained to the driver error; the connection is already invalidated
        assert is_dead_connection_error(caught.value.__cause__)
        assert caught.value.connection_invalidated
        assert _all_replaced() == before
        assert raw.is_closed()  # invalidated: never pooled again
        assert await _request_pid(engine) != first
    finally:
        await engine.dispose()


async def test_concurrent_use_is_not_reclassified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine concurrent-use error on a LIVE connection keeps its type."""
    engine = _engine(monkeypatch, "plain")
    try:
        async with engine.connect() as conn:
            raw = (await conn.get_raw_connection()).driver_connection
            busy = asyncio.create_task(raw.execute("SELECT pg_sleep(0.3)"))
            await asyncio.sleep(0.05)
            with pytest.raises(DBAPIError) as caught:
                await conn.execute(text("SELECT 1"))
            await busy
            await conn.rollback()
        assert "another operation is in progress" in str(caught.value)
        assert not isinstance(caught.value, DatabaseUnavailable)
        assert not caught.value.connection_invalidated
        assert not is_dead_connection_error(caught.value)
        assert not raw.is_closed()
    finally:
        await engine.dispose()


async def test_classification() -> None:
    from asyncpg import exceptions as pg

    consume = pg.InternalClientError(
        "cannot switch to state 12; another operation (2) is in progress"
    )
    assert is_dead_connection_error(consume) and never_sent(consume)
    closed = pg.InterfaceError("connection is closed")
    assert is_dead_connection_error(closed) and never_sent(closed)
    gone = pg.ConnectionDoesNotExistError("connection was closed in the middle")
    assert is_dead_connection_error(gone) and not never_sent(gone)
    assert is_dead_connection_error(ConnectionResetError())
    assert is_dead_connection_error(pg.AdminShutdownError("terminating"))
    wrapped = RuntimeError("translated")
    wrapped.__cause__ = gone
    assert is_dead_connection_error(wrapped)
    # Not a dead connection: a busy protocol, a query error, a timeout.
    for alive in (
        pg.InternalClientError(
            "cannot switch to state 12; another operation (12) is in progress"
        ),
        pg.InterfaceError("cannot perform operation: another operation is in progress"),
        pg.UndefinedTableError("no such table"),
        TimeoutError(),
        RuntimeError("connection is closed"),  # not a driver error
    ):
        assert not is_dead_connection_error(alive), alive


# ------------------------------------------------- fault matrix, per phase


@pytest_asyncio.fixture
async def tables(admin: AsyncEngine) -> AsyncIterator[None]:
    """A business table whose COMMIT can be made slow (a deferred trigger),
    plus the core outbox / audit tables."""
    async with admin.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[OutboxMessage.__table__, AuditLogEntry.__table__],
        )
        await conn.execute(text("DROP TABLE IF EXISTS deadconn_orders"))
        await conn.execute(
            text("CREATE TABLE deadconn_orders (id serial PRIMARY KEY, note text)")
        )
        await conn.execute(
            text(
                "CREATE OR REPLACE FUNCTION deadconn_slow_commit() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                "IF NEW.note = 'slow-commit' THEN PERFORM pg_sleep(1.5); END IF; "
                "RETURN NULL; END $$"
            )
        )
        await conn.execute(
            text(
                "CREATE CONSTRAINT TRIGGER deadconn_slow AFTER INSERT ON "
                "deadconn_orders DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION deadconn_slow_commit()"
            )
        )
        await conn.execute(
            text("DELETE FROM outbox_messages WHERE channel = :c"), {"c": CHANNEL}
        )
        await conn.execute(
            text("DELETE FROM audit_log WHERE action = :a"), {"a": ACTION}
        )
    yield
    async with admin.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS deadconn_orders"))
        await conn.execute(text("DROP FUNCTION IF EXISTS deadconn_slow_commit()"))
        await conn.execute(
            text("DELETE FROM outbox_messages WHERE channel = :c"), {"c": CHANNEL}
        )
        await conn.execute(
            text("DELETE FROM audit_log WHERE action = :a"), {"a": ACTION}
        )


async def _counts(admin: AsyncEngine, note: str) -> tuple[int, int, int]:
    async with admin.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT count(*) FROM deadconn_orders WHERE note = :n"),
                {"n": note},
            )
        ).scalar_one()
        outbox = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM outbox_messages WHERE channel = :c "
                    "AND payload->>'note' = :n"
                ),
                {"c": CHANNEL, "n": note},
            )
        ).scalar_one()
        audit = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_log WHERE action = :a "
                    "AND resource_id = :n"
                ),
                {"a": ACTION, "n": note},
            )
        ).scalar_one()
    return int(rows), int(outbox), int(audit)


async def _write(
    engine: AsyncEngine, note: str, *, insert_sql: str | None = None, hook: Any = None
) -> None:
    """One write request: business row + outbox event + audit, one UoW."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with committing_uow(sessions) as db:
        await db.execute(
            text(insert_sql or "INSERT INTO deadconn_orders (note) VALUES (:n)"),
            {"n": note},
        )
        await publish_event(CHANNEL, {"note": note})
        record_audit(ACTION, actor_type="system", resource_id=note, required=True)
        await db.flush()
        if hook is not None:
            await hook(db)


async def _driver(db: AsyncSession) -> Any:
    conn = await db.connection()
    return (await conn.get_raw_connection()).driver_connection


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "phase",
    [
        "dead_before_sql_sent",
        "killed_before_sql_sent",
        "killed_during_first_statement",
        "dead_after_insert_before_commit",
        "killed_after_insert_before_commit",
        "killed_during_commit",
    ],
)
async def test_fault_matrix(
    monkeypatch: pytest.MonkeyPatch,
    admin: AsyncEngine,
    tables: None,
    kind: str,
    phase: str,
) -> None:
    engine = _engine(monkeypatch, kind)
    note = f"{kind}-{phase}"
    try:
        raw, pid = await _pooled_raw(engine)
        outcome: BaseException | None = None
        if phase == "dead_before_sql_sent":
            _poison(raw)
            await _write(engine, note)
        elif phase == "killed_before_sql_sent":
            await _terminate(admin, pid)
            await _wait_closed(raw)
            await _write(engine, note)
        elif phase == "killed_during_first_statement":
            slow = "INSERT INTO deadconn_orders (note) SELECT :n FROM pg_sleep(2)"

            async def killer() -> None:
                await asyncio.sleep(0.4)
                await _terminate(admin, pid)

            kill = asyncio.create_task(killer())
            try:
                await _write(engine, note, insert_sql=slow)
            except BaseException as error:  # noqa: BLE001 - asserted below
                outcome = error
            await kill
        elif phase in ("dead_after_insert_before_commit",):

            async def poison(db: AsyncSession) -> None:
                _poison(await _driver(db))

            try:
                await _write(engine, note, hook=poison)
            except BaseException as error:  # noqa: BLE001 - the outcome under test
                outcome = error
        elif phase == "killed_after_insert_before_commit":

            async def kill_now(db: AsyncSession) -> None:
                live = await _driver(db)
                await _terminate(admin, live.get_server_pid())
                await _wait_closed(live)

            try:
                await _write(engine, note, hook=kill_now)
            except BaseException as error:  # noqa: BLE001 - the outcome under test
                outcome = error
        else:  # killed_during_commit: the deferred trigger sleeps in COMMIT
            committing = asyncio.Event()

            async def mark(db: AsyncSession) -> None:
                committing.set()

            async def killer() -> None:
                await committing.wait()
                await asyncio.sleep(0.5)
                await _terminate(admin, pid)

            kill = asyncio.create_task(killer())
            try:
                await _write(engine, "slow-commit", hook=mark)
            except BaseException as error:  # noqa: BLE001 - the outcome under test
                outcome = error
            await kill
            note = "slow-commit"

        counts = await _counts(admin, note)
        if phase.endswith("before_sql_sent"):
            # Provably nothing was sent: recovered, written exactly once.
            assert outcome is None
            assert counts == (1, 1, 1)
        else:
            # Reached a dying connection: a clean 503, rolled back, no retry.
            assert isinstance(outcome, DatabaseUnavailable), repr(outcome)
            assert http_status_of(outcome) == 503
            assert counts == (0, 0, 0)
        # The pool recovers for the next request in every case.
        assert await _request_pid(engine) != pid
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()


async def test_idle_ping_is_one_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The idle ping is ``SELECT 1`` on the driver (1 RT), never the dialect's
    BEGIN / ``;`` / ROLLBACK (3 RT)."""
    monkeypatch.setattr(settings, "db_pool_ping_idle_seconds", 0.0)
    engine = driver._pooled_engine(_url(), pool_size=1, max_overflow=0)

    def no_dialect_ping(*_a: Any) -> bool:
        raise AssertionError("the 3-round-trip dialect ping was used")

    monkeypatch.setattr(engine.sync_engine.dialect, "do_ping", no_dialect_ping)
    try:
        first = await _request_pid(engine)
        assert await _request_pid(engine) == first  # pinged OK, reused
    finally:
        await engine.dispose()


async def test_recovery_keeps_and_then_resets_checkout_characteristics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only checkout (the planner's ``postgresql_readonly``) recovered
    on a new connection stays read-only, and the pool still resets it at
    checkin: the next request can write (soak regression: the reset finalizer
    was lost with the old connection -> "cannot execute INSERT in a read-only
    transaction" on every later request of that pooled connection)."""
    engine = _engine(monkeypatch, "rls_merged")
    try:
        raw, _first = await _pooled_raw(engine)
        async with engine.connect() as conn:
            conn = await conn.execution_options(postgresql_readonly=True)
            _poison(raw)
            mode = (await conn.execute(text("SHOW transaction_read_only"))).scalar()
            assert mode == "on"
            await conn.rollback()
        async with AsyncSession(engine) as session, session.begin():
            mode = (await session.execute(text("SHOW transaction_read_only"))).scalar()
        assert mode == "off"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("kind", KINDS)
async def test_contract_pool_command_claim_recovered_when_never_sent(
    monkeypatch: pytest.MonkeyPatch, admin: AsyncEngine, kind: str
) -> None:
    """The contract pool (``_build_contract_engine``) runs a command's durable
    idempotency claim INSERT as the FIRST statement of its own UoW: a pooled
    connection that died while idle is recovered there too (provably unsent),
    and the claim lands exactly once."""
    from sandloom.core.sql_alchemy.command_log import sql_alchemy_command_log_store
    from sandloom.core.sql_alchemy.models.contract_command import ContractCommandRecord

    async with admin.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all, tables=[ContractCommandRecord.__table__]
        )
    key = f"deadconn-{kind}".ljust(64, "0")
    async with admin.begin() as conn:
        await conn.execute(
            text("DELETE FROM contract_command_log WHERE dedupe_key = :k"), {"k": key}
        )
    monkeypatch.setattr(settings, "db_pool_ping_idle_seconds", 3600.0)
    monkeypatch.setattr(settings, "contract_db_pool_size", 1)
    monkeypatch.setattr(settings, "contract_db_max_overflow", 0)
    monkeypatch.setattr(settings, "db_rls_merged_begin", kind == "rls_merged")
    engine = driver._build_contract_engine(_url(), tenant_rls=kind != "plain")
    try:
        raw, first = await _pooled_raw(engine)
        _poison(raw)
        before = _replaced(_PHASE[kind])
        store = sql_alchemy_command_log_store()
        async with AsyncSession(engine) as session, session.begin():
            claim = await store.claim(
                session,
                dedupe_key=key,
                contract="deadconn.v1.charge",
                tenant=None,
                args_digest="d" * 64,
                ttl_seconds=60,
            )
            await store.complete(session, dedupe_key=key, status="ok", result="1")
        assert claim.outcome == "claimed"
        assert _replaced(_PHASE[kind]) == before + 1
        async with admin.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM contract_command_log WHERE dedupe_key = :k"
                    ),
                    {"k": key},
                )
            ).scalar_one()
        assert rows == 1
        assert await _request_pid(engine) != first
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(
                text("DELETE FROM contract_command_log WHERE dedupe_key = :k"),
                {"k": key},
            )
