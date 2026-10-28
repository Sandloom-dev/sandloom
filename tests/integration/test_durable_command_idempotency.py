"""Durable command-contract idempotency (real PostgreSQL).

The command dedupe record lives in the PROVIDER's database and commits in the
command's own Unit of Work (``contract_command_log``). Scenarios:

* crash after the commit (before anything post-commit ran, the cache is gone)
  -> a retry replays the committed result and never re-executes;
* concurrent duplicates -> exactly one execution, every caller gets its result;
* a duplicate that outwaits the lock timeout -> ``ContractCallInProgress`` (503);
* same key, different arguments -> 409, locally and over RPC;
* a failed command leaves no record -> the retry executes;
* local and RPC calls share one record (identical replay / conflict);
* expired records are taken over and purged; oversized results never re-run;
* billing ``billing.credits.v1.spend`` under crash / retry.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from functools import partial
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sandloom.core import unit_of_work as uow_module
from sandloom.core.auth.revocation import (
    revoke_user_tokens,
    revoke_user_tokens_on_commit,
)
from sandloom.core.auth.verification import assert_token_valid_now
from sandloom.core.config.settings import settings
from sandloom.core.contract_testing import (
    rpc_proxy_for,
    rpc_request_headers,
    rpc_settings,
)
from sandloom.core.facade import CoreFacade
from sandloom.core.module_api import (
    ContractCallInProgress,
    ContractIdempotencyConflict,
    ContractResultNotReplayable,
    contract_method,
    contract_session,
)
from sandloom.core.module_api._context import caller_user_id
from sandloom.core.rpc import install_service_rpc_routes
from sandloom.core.rpc import wire as rpc_wire
from sandloom.core.testing import InMemoryCache, committing_uow
from sandloom.core.testing.fixtures import *  # noqa: F403
from sandloom.core.testing.infra import override_instance
from sandloom.core.unit_of_work.hooks import on_commit
from sandloom.core.unit_of_work.request import RequestScope, bind_request_scope
from sandloom.core.unit_of_work.risk import sensitive_effect

pytestmark = pytest.mark.asyncio

SERVICE = "demo.durable_ledger.v1"


class _Crash(BaseException):
    """The process dies right after the commit (nothing may swallow it)."""


@sensitive_effect("money")
async def _pay_out(*, operation_key: str) -> None:
    """A money effect deduped on its own key (a ledger row's)."""


#: the risk source the money effect is recorded under
PAY_OUT = f"{_pay_out.__module__}.{_pay_out.__qualname__}"


@sensitive_effect("money")
async def _pay_out_and_fail(*, operation_key: str) -> None:
    """A money effect that fails after it started (an on_commit callback's
    failure is logged, never raised: the transaction already committed)."""
    raise RuntimeError("the payout failed")


PAY_OUT_AND_FAIL = f"{_pay_out_and_fail.__module__}.{_pay_out_and_fail.__qualname__}"


class Ledger:
    """A command contract whose effect is a row in ``idem_demo_effects``."""

    _calls: list[str] = []
    _fail_next: bool = False
    _delay: float = 0.0

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def spend(cls, amount: int, operation_key: str) -> int:
        cls._calls.append(operation_key)
        if cls._delay:
            await asyncio.sleep(cls._delay)
        db = contract_session()
        await db.execute(
            text("INSERT INTO idem_demo_effects (op, amount) VALUES (:op, :amount)"),
            {"op": operation_key, "amount": amount},
        )
        if cls._fail_next:
            cls._fail_next = False
            raise RuntimeError("boom after the effect")
        return 100 - amount

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def withdraw(cls, owner: str, operation_key: str) -> str:
        """A personal resource: only its owner may withdraw (the body
        authorizes the calling end user, like billing's personal spend)."""
        if str(caller_user_id()) != owner:
            raise HTTPException(status_code=403, detail="not your account")
        cls._calls.append(operation_key)
        return f"balance-of-{owner}"

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def pay(cls, amount: int, operation_key: str) -> int:
        """Untagged: its money risk is observed only while its body runs."""
        cls._calls.append(operation_key)
        await _pay_out(operation_key=operation_key)
        await contract_session().execute(
            text("INSERT INTO idem_demo_effects (op, amount) VALUES (:op, :amount)"),
            {"op": operation_key, "amount": amount},
        )
        return 100 - amount

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def settle(cls, operation_key: str, how: str) -> int:
        """Untagged: its money effect runs in an on_commit callback after
        the commit -- the tagged effect itself, a closure around it, or a
        tagged effect that fails."""
        cls._calls.append(operation_key)
        callbacks = {
            "tagged": partial(_pay_out, operation_key=operation_key),
            "closure": lambda: _pay_out(operation_key=operation_key),
            "failing": partial(_pay_out_and_fail, operation_key=operation_key),
        }
        await on_commit(callbacks[how])
        await contract_session().execute(
            text("INSERT INTO idem_demo_effects (op, amount) VALUES (:op, 1)"),
            {"op": operation_key},
        )
        return 7

    #: set once the hanging callback's money effect completed
    _paid: asyncio.Event | None = None

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def settle_then_hang(cls, operation_key: str) -> int:
        """Untagged: an on_commit closure completes a keyed money effect,
        then hangs until its request is cancelled (a request timeout)."""
        assert operation_key not in cls._calls, "a replay re-executed the body"
        cls._calls.append(operation_key)
        paid = cls._paid = asyncio.Event()

        async def pay_then_hang() -> None:
            await _pay_out(operation_key=operation_key)
            paid.set()
            await asyncio.Event().wait()

        await on_commit(pay_then_hang)
        await contract_session().execute(
            text("INSERT INTO idem_demo_effects (op, amount) VALUES (:op, 1)"),
            {"op": operation_key},
        )
        return 7

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def revoke_self(cls, owner: str, operation_key: str) -> bool:
        """A password change: the caller revokes its own tokens."""
        cls._calls.append(operation_key)
        await revoke_user_tokens_on_commit(uuid.UUID(owner), reason="test")
        return True

    @classmethod
    @contract_method(command=True, idempotency_key="operation_key")
    async def huge(cls, operation_key: str) -> str:
        cls._calls.append(operation_key)
        return "x" * (300 * 1024)


@pytest.fixture
def sessions(db_engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def effects_table(db_engine: Any) -> AsyncIterator[None]:
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS idem_demo_effects "
                "(id serial PRIMARY KEY, op text NOT NULL, amount int NOT NULL)"
            )
        )
        await conn.execute(text("DELETE FROM idem_demo_effects"))
    yield
    async with db_engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS idem_demo_effects"))


@asynccontextmanager
async def _real_database(sandloom_core: Any) -> AsyncIterator[Any]:
    """The app's ``default`` database pointed at the test database."""
    async with override_instance(
        sandloom_core, "database", driver="sqlalchemy", url=os.environ["DATABASE_URL"]
    ):
        yield sandloom_core.database("default")


@pytest.fixture
async def provider_core(
    sandloom_core: Any, db_engine: Any, effects_table: None, monkeypatch: Any
) -> AsyncIterator[tuple[FastAPI, CoreFacade]]:
    """A bare provider facade on the REAL test database, serving RPC too."""
    # The process is keyed as ``ledger`` calling itself over RPC.
    for name, value in rpc_settings(
        "ledger", allowed_calls={"ledger": [SERVICE]}
    ).items():
        monkeypatch.setattr(settings, name, value)
    Ledger._calls.clear()
    Ledger._fail_next = False
    Ledger._delay = 0.0
    Ledger._paid = None
    async with _real_database(sandloom_core) as provider:
        app = FastAPI()
        core = CoreFacade(app=app)
        app.state.core = core
        core.database = lambda name="default": provider  # type: ignore[method-assign]
        core.register_service(SERVICE, Ledger)
        install_service_rpc_routes(app, core)
        yield app, core


async def _effects(sessions: async_sessionmaker[AsyncSession], op: str) -> int:
    async with sessions() as db:
        return int(
            (
                await db.execute(
                    text("SELECT count(*) FROM idem_demo_effects WHERE op = :op"),
                    {"op": op},
                )
            ).scalar_one()
        )


async def _records(sessions: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with sessions() as db:
        return list(
            (
                await db.execute(
                    text(
                        "SELECT contract, status, result, expires_at "
                        "FROM contract_command_log"
                    )
                )
            ).all()
        )


async def _rpc(app: FastAPI, method: str, args: list[Any]) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        body = rpc_wire._pack({"args": args})
        headers = rpc_request_headers(settings, f"{SERVICE}/{method}", body, sign=True)
        return await client.post(
            f"/_sandloom/rpc/{SERVICE}/{method}", content=body, headers=headers
        )


async def test_crash_after_commit_replays_and_never_reexecutes(
    provider_core: tuple[FastAPI, CoreFacade],
    sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, core = provider_core
    svc = core.get_service(SERVICE)
    real_dispatch = uow_module._dispatch_outbox_after_commit

    async def crash_after_commit(*_: Any, **__: Any) -> None:
        raise _Crash

    # The command commits, then the process "dies" before any post-commit
    # step (the old design stored its dedupe result in the cache HERE).
    monkeypatch.setattr(uow_module, "_dispatch_outbox_after_commit", crash_after_commit)
    with pytest.raises(_Crash):
        await svc.spend(10, "op-crash")
    monkeypatch.setattr(uow_module, "_dispatch_outbox_after_commit", real_dispatch)
    # Whatever the cache holds is irrelevant: it is broken now.
    from sandloom.core.idempotency import cache_store as idempotency_cache_store

    monkeypatch.setattr(
        idempotency_cache_store,
        "cache",
        lambda name="default": InMemoryCache(broken=True),
    )

    assert await svc.spend(10, "op-crash") == 90  # replayed
    assert Ledger._calls == ["op-crash"]  # the body ran once
    assert await _effects(sessions, "op-crash") == 1
    [record] = await _records(sessions)
    assert record.contract == f"{SERVICE}.spend"
    assert record.status == "succeeded"


async def test_concurrent_duplicates_execute_exactly_once(
    provider_core: tuple[FastAPI, CoreFacade],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, core = provider_core
    svc = core.get_service(SERVICE)
    Ledger._delay = 0.2
    results = await asyncio.gather(*(svc.spend(25, "op-race") for _ in range(5)))
    assert results == [75] * 5
    assert Ledger._calls == ["op-race"]
    assert await _effects(sessions, "op-race") == 1


async def test_duplicate_outwaiting_the_lock_timeout_is_in_progress(
    sandloom_core: Any,
    db_engine: Any,
    effects_table: None,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork

    # A contract pool whose lock_timeout (CONTRACT_DB_LOCK_TIMEOUT_MS in
    # production) bounds how long a duplicate waits for the in-flight call.
    engine = create_async_engine(
        os.environ["DATABASE_URL"],
        connect_args={"server_settings": {"lock_timeout": "200"}},
    )
    contract_sessions = async_sessionmaker(engine, expire_on_commit=False)

    def contract_uow() -> Any:
        return SQLAlchemyUnitOfWork(session_factory=contract_sessions)

    Ledger._calls.clear()
    Ledger._delay = 1.0
    try:
        async with _real_database(sandloom_core) as provider:
            core = CoreFacade(app=FastAPI())
            core.database = lambda name="default": provider  # type: ignore[method-assign]
            core.get_contract_uow_factory = (  # type: ignore[method-assign]
                lambda name="default": contract_uow
            )
            core.register_service(SERVICE, Ledger)
            svc = core.get_service(SERVICE)
            first = asyncio.create_task(svc.spend(5, "op-slow"))
            await asyncio.sleep(0.3)
            with pytest.raises(ContractCallInProgress) as exc_info:
                await svc.spend(5, "op-slow")
            assert exc_info.value.status_code == 503
            assert await first == 95
            Ledger._delay = 0.0
            assert await svc.spend(5, "op-slow") == 95  # now a replay
    finally:
        Ledger._delay = 0.0
        await engine.dispose()
    assert Ledger._calls == ["op-slow"]
    assert await _effects(sessions, "op-slow") == 1


async def test_same_key_other_arguments_conflicts_locally_and_over_rpc(
    provider_core: tuple[FastAPI, CoreFacade],
) -> None:
    app, core = provider_core
    svc = core.get_service(SERVICE)
    assert await svc.spend(10, "op-key") == 90
    with pytest.raises(ContractIdempotencyConflict) as exc_info:
        await svc.spend(20, "op-key")
    assert exc_info.value.status_code == 409
    resp = await _rpc(app, "spend", [30, "op-key"])
    assert resp.status_code == 409
    assert (
        rpc_wire._unpack(resp.content)["error"]["code"]
        == "contract_idempotency_conflict"
    )
    assert Ledger._calls == ["op-key"]


async def test_failed_command_leaves_no_record_and_retry_executes(
    provider_core: tuple[FastAPI, CoreFacade],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, core = provider_core
    svc = core.get_service(SERVICE)
    Ledger._fail_next = True
    with pytest.raises(Exception, match="boom"):
        await svc.spend(10, "op-fail")
    assert await _records(sessions) == []
    assert await _effects(sessions, "op-fail") == 0  # rolled back with the claim
    assert await svc.spend(10, "op-fail") == 90
    assert Ledger._calls == ["op-fail", "op-fail"]
    assert await _effects(sessions, "op-fail") == 1


async def test_local_and_rpc_share_one_record(
    provider_core: tuple[FastAPI, CoreFacade],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    app, core = provider_core
    svc = core.get_service(SERVICE)
    # local first, RPC retry replays
    assert await svc.spend(10, "op-l") == 90
    resp = await _rpc(app, "spend", [10, "op-l"])
    assert resp.status_code == 200
    assert rpc_wire._unpack(resp.content)["result"] == 90
    # RPC first, local retry replays
    resp = await _rpc(app, "spend", [40, "op-r"])
    assert rpc_wire._unpack(resp.content)["result"] == 60
    assert await svc.spend(40, "op-r") == 60
    assert Ledger._calls == ["op-l", "op-r"]
    assert await _effects(sessions, "op-l") == 1
    assert await _effects(sessions, "op-r") == 1


@contextmanager
def _as_user(user: uuid.UUID) -> Iterator[None]:
    """Run as a request of ``user`` (local calls read the subject; a remote
    call carries it as the MAC-covered ``usr``)."""
    scope = RequestScope(core=None, policy=None)
    scope.subject, scope.claims = str(user), {"sub": str(user)}
    with bind_request_scope(scope):
        yield


async def test_a_replay_never_serves_another_callers_result(
    provider_core: tuple[FastAPI, CoreFacade],
) -> None:
    """G-BILLING-20 at the framework level: a command's dedupe record is
    scoped to the end user it ran for. Bob reusing Alice's key gets his own
    authorization decision (403), never Alice's stored result; Alice's retry
    still replays (locally and over the signed RPC path, one record)."""
    app, core = provider_core
    svc = core.get_service(SERVICE)
    alice, bob = uuid.uuid4(), uuid.uuid4()
    with _as_user(alice):
        assert await svc.withdraw(str(alice), "op-own") == f"balance-of-{alice}"
    with _as_user(bob):
        with pytest.raises(Exception) as exc_info:
            await svc.withdraw(str(alice), "op-own")
        assert getattr(exc_info.value, "status_code", None) == 403
        resp = await _rpc(app, "withdraw", [str(alice), "op-own"])
        assert resp.status_code == 403
    with _as_user(alice):
        assert await svc.withdraw(str(alice), "op-own") == f"balance-of-{alice}"
        resp = await _rpc(app, "withdraw", [str(alice), "op-own"])
        assert rpc_wire._unpack(resp.content)["result"] == f"balance-of-{alice}"
    assert Ledger._calls == ["op-own"]  # Alice's retries replayed


async def test_a_personal_spend_replay_is_the_spenders_only(
    sandloom_core: Any,
    db_engine: Any,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The reviewer's repro: Alice spends with key K (77 left); Bob with a
    fresh key is refused (403); Bob with Alice's key K is refused too, never
    served her balance; Alice's retry replays 77 with one debit."""
    from sqlalchemy import func, select

    from sandloom_billing.models import CreditLedgerEntry
    from sandloom_billing.services import BillingService

    alice, bob = uuid.uuid4(), uuid.uuid4()
    async with committing_uow(sessions) as db:
        customer = await BillingService.get_or_create_customer(db, user_id=alice)
        await BillingService.add_credits(customer, 100, f"grant-{alice}", db)
    key = f"spend-{alice}"
    async with _real_database(sandloom_core):
        credits = sandloom_core.get_service("billing.credits.v1")
        with _as_user(alice):
            assert await credits.spend("user", alice, 23, key) == 77
        with _as_user(bob):
            for operation_key in (f"fresh-{bob}", key):
                with pytest.raises(Exception) as exc_info:
                    await credits.spend("user", alice, 23, operation_key)
                assert getattr(exc_info.value, "status_code", None) == 403
        with _as_user(alice):
            assert await credits.spend("user", alice, 23, key) == 77
    async with sessions() as db:
        debits = (
            await db.execute(
                select(func.count())
                .select_from(CreditLedgerEntry)
                .where(CreditLedgerEntry.operation_key == key)
            )
        ).scalar_one()
    assert debits == 1


async def test_expired_record_is_taken_over_and_purged(
    provider_core: tuple[FastAPI, CoreFacade],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from sandloom.core.tasks.relay import purge_expired_commands

    _, core = provider_core
    svc = core.get_service(SERVICE)
    assert await svc.spend(10, "op-ttl") == 90
    async with sessions() as db, db.begin():
        await db.execute(
            text("UPDATE contract_command_log SET expires_at = now() - interval '1s'")
        )
    # Past CONTRACT_COMMAND_DEDUPE_TTL_SECONDS the key runs afresh.
    assert await svc.spend(10, "op-ttl") == 90
    assert Ledger._calls == ["op-ttl", "op-ttl"]
    assert len(await _records(sessions)) == 1
    async with sessions() as db, db.begin():
        await db.execute(
            text("UPDATE contract_command_log SET expires_at = now() - interval '1s'")
        )
    factory = core.get_uow_factory("default")
    assert await purge_expired_commands(core, "default", factory) == 1
    assert await _records(sessions) == []


async def test_claims_and_completions_reuse_prebuilt_statements(
    provider_core: tuple[FastAPI, CoreFacade],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from sandloom.core.sql_alchemy import command_log

    _, core = provider_core
    svc = core.get_service(SERVICE)
    assert await svc.spend(1, "op-s1") == 99
    upsert = command_log._upserts["postgresql"]
    completion = command_log._completions[True]
    assert await svc.spend(2, "op-s2") == 98
    assert await svc.spend(2, "op-s2") == 98  # replay: the prebuilt lookup
    assert command_log._upserts["postgresql"] is upsert
    assert command_log._completions[True] is completion
    assert command_log._replay_lookup is not None
    assert Ledger._calls == ["op-s1", "op-s2"]
    assert len(await _records(sessions)) == 2


async def test_oversized_result_is_never_reexecuted(
    provider_core: tuple[FastAPI, CoreFacade],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, core = provider_core
    svc = core.get_service(SERVICE)
    assert len(await svc.huge("op-big")) == 300 * 1024
    with pytest.raises(ContractResultNotReplayable):
        await svc.huge("op-big")
    assert Ledger._calls == ["op-big"]
    [record] = await _records(sessions)
    assert record.status == "not_replayable" and record.result is None


async def test_billing_credit_spend_survives_crash_and_retry(
    sandloom_core: Any,
    db_engine: Any,
    sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import func, select

    from sandloom_billing.models import CreditLedgerEntry
    from sandloom_billing.services import BillingService

    org_id = uuid.uuid4()
    async with committing_uow(sessions) as db:
        customer = await BillingService.get_or_create_customer(
            db, organization_id=org_id
        )
        await BillingService.add_credits(customer, 100, "grant-1", db)
        customer_id = customer.id

    async def crash_after_commit(*_: Any, **__: Any) -> None:
        raise _Crash

    async with _real_database(sandloom_core):
        credits = sandloom_core.get_service("billing.credits.v1")
        real_dispatch = uow_module._dispatch_outbox_after_commit
        monkeypatch.setattr(
            uow_module, "_dispatch_outbox_after_commit", crash_after_commit
        )
        with pytest.raises(_Crash):
            await credits.spend("organization", org_id, 30, "spend-1")
        monkeypatch.setattr(uow_module, "_dispatch_outbox_after_commit", real_dispatch)

        # A later grant moves the balance; the retried spend still replays
        # ITS result from the durable record (no second debit).
        async with committing_uow(sessions) as db:
            customer = await BillingService.get_or_create_customer(
                db, organization_id=org_id
            )
            await BillingService.add_credits(customer, 50, "grant-2", db)
        assert await credits.spend("organization", org_id, 30, "spend-1") == 70
        with pytest.raises(ContractIdempotencyConflict):
            await credits.spend("organization", org_id, 31, "spend-1")
        assert await credits.get_balance("organization", org_id) == 120

    async with sessions() as db:
        debits = (
            await db.execute(
                select(func.count())
                .select_from(CreditLedgerEntry)
                .where(CreditLedgerEntry.operation_key == "spend-1")
            )
        ).scalar_one()
        assert debits == 1
        assert await BillingService.get_credit_balance(customer_id, db) == 120


async def test_provider_killed_mid_command_does_not_block_the_retry(
    provider_core: tuple[FastAPI, CoreFacade],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """SIGKILL mid-command: the uncommitted claim dies with the transaction."""
    import signal
    import sys
    import time
    from pathlib import Path

    _, core = provider_core
    script = Path(__file__).with_name("_crash_provider.py")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        SERVICE,
        "10",
        "op-killed",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    try:
        assert proc.stdout is not None
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
        assert line.strip() == b"CLAIMED", (
            line,
            (await proc.stderr.read()) if proc.stderr else b"",
        )
    finally:
        proc.send_signal(signal.SIGKILL)
        await proc.wait()

    started = time.monotonic()
    assert await core.get_service(SERVICE).spend(10, "op-killed") == 90
    elapsed = time.monotonic() - started
    print(f"retry after SIGKILL: {elapsed * 1000:.1f} ms")
    assert elapsed < 5.0, f"retry blocked for {elapsed:.1f}s"
    assert Ledger._calls == ["op-killed"]  # the retry ran the body afresh
    assert await _effects(sessions, "op-killed") == 1  # the killed one vanished
    [record] = await _records(sessions)
    assert record.status == "succeeded"


class _SessionUoW:
    """A minimal committing UoW over one session (``purge_expired``'s factory)."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self.session: AsyncSession | None = None

    async def __aenter__(self) -> _SessionUoW:
        self.session = self._sessions()
        await self.session.begin()
        return self

    async def __aexit__(self, exc_type: Any, *_: Any) -> None:
        assert self.session is not None
        if exc_type is None:
            await self.session.commit()
        else:
            await self.session.rollback()
        await self.session.close()


async def test_purge_never_deletes_a_record_taken_over_concurrently(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """R4: purge picks an expired key, blocks on a takeover's row lock, and the
    takeover commits a LIVE record — the purge must re-check expiry and keep it
    (else the command's retry re-executes)."""
    from sandloom.core.sql_alchemy.command_log import sql_alchemy_command_log_store

    store = sql_alchemy_command_log_store()
    async with sessions() as db, db.begin():
        await db.execute(text("DELETE FROM contract_command_log"))
        await db.execute(
            text(
                "INSERT INTO contract_command_log (dedupe_key, contract, tenant_id, "
                "args_digest, status, result, created_at, expires_at) VALUES "
                "('k-race', 'c', NULL, 'd', 'ok', NULL, "
                "now() - interval '2h', now() - interval '1h')"
            )
        )
    takeover = sessions()
    await takeover.begin()
    try:
        claim = await store.claim(
            takeover,
            dedupe_key="k-race",
            contract="c",
            tenant=None,
            args_digest="d",
            ttl_seconds=3600,
        )
        assert claim.outcome == "claimed"
        purge = asyncio.create_task(
            store.purge_expired(lambda: _SessionUoW(sessions), limit=10)
        )
        # Wait until the purge is blocked on the takeover's row lock.
        for _ in range(200):
            async with sessions() as probe:
                waiting = (
                    await probe.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE "
                            "wait_event_type = 'Lock' AND query ILIKE "
                            "'DELETE FROM contract_command_log%'"
                        )
                    )
                ).scalar_one()
            if waiting:
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - diagnostic
            pytest.fail("purge never blocked on the takeover lock")
        await takeover.commit()
    finally:
        await takeover.close()
    assert await purge == 0
    records = await _records(sessions)
    assert len(records) == 1


async def test_an_in_flight_claim_past_its_ttl_is_never_taken_over(
    db_engine: Any, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Review GLM-3: the TTL never lets a duplicate take over a RUNNING
    command — the claim is uncommitted, so a duplicate waits on the unique
    index (bounded by lock_timeout -> ``CommandLogBusy``) however old the
    claim's ``expires_at`` is. And a command whose transaction outlives the
    TTL commits a LIVE record (expiry counted from completion), so its retry
    replays instead of running the body again."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from sandloom.core.infra.contracts import CommandLogBusy
    from sandloom.core.sql_alchemy.command_log import sql_alchemy_command_log_store

    store = sql_alchemy_command_log_store()
    claim_args = {
        "dedupe_key": "k-long",
        "contract": "c",
        "tenant": None,
        "args_digest": "d",
        "ttl_seconds": 1,
    }
    async with sessions() as db, db.begin():
        await db.execute(text("DELETE FROM contract_command_log"))
    engine = create_async_engine(
        os.environ["DATABASE_URL"],
        connect_args={"server_settings": {"lock_timeout": "200"}},
    )
    busy_sessions = async_sessionmaker(engine, expire_on_commit=False)
    first = sessions()
    await first.begin()
    try:
        assert (await store.claim(first, **claim_args)).outcome == "claimed"
        await asyncio.sleep(1.2)  # the running claim's expires_at has passed
        async with busy_sessions() as dup, dup.begin():
            with pytest.raises(CommandLogBusy):
                await store.claim(dup, **claim_args)
        await store.complete(first, dedupe_key="k-long", status="ok", result="r")
        await first.commit()
    finally:
        await first.close()
        await engine.dispose()
    async with sessions() as retry, retry.begin():
        claim = await store.claim(retry, **claim_args)
    assert claim.outcome == "replay"
    assert claim.result == "r"


# --------------------------------------------------------------------------- #
# A replay marks its caller like the first execution; caller facts (RPC parity)
# --------------------------------------------------------------------------- #
class _LoseFirstAnswer(httpx.AsyncBaseTransport):
    """The provider runs the first ``method`` call to its end, then its
    answer is lost on the way back (the proxy retries the call)."""

    def __init__(self, inner: httpx.AsyncBaseTransport, method: str) -> None:
        self.inner, self.method, self.lost = inner, method, 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if (
            not self.lost
            and request.method == "POST"
            and request.url.path.endswith(f"/{self.method}")
        ):
            self.lost += 1
            await response.aread()
            await response.aclose()
            raise httpx.ReadError("the answer was lost", request=request)
        return response


def _user_scope(user: uuid.UUID, claims: dict[str, Any] | None = None) -> RequestScope:
    scope = RequestScope(core=None, policy=None)
    scope.subject = str(user)
    scope.claims = claims if claims is not None else {"sub": str(user)}
    return scope


def _marks(scope: RequestScope) -> Any:
    state = scope.risk
    if state is None:
        return None
    return (
        {risk: sorted(sources) for risk, sources in state.risks.items()},
        state.elsewhere,
    )


async def test_a_durable_replay_marks_the_request_like_its_first_execution(
    provider_core: tuple[FastAPI, CoreFacade],
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """(G-SENS-04, replay) an untagged command's body runs a money effect.
    Its durable record keeps what the execution observed, so a replay (the
    body skipped) marks the caller's request with the same risk and audit
    obligation: locally (the same caller's next request) and over the signed
    RPC path (the first answer is lost, the proxy's automatic retry
    replays)."""
    app, core = provider_core
    svc = core.get_service(SERVICE)
    alice = uuid.uuid4()
    expected = ({"money": [PAY_OUT]}, True)
    for _ in range(2):
        scope = _user_scope(alice)
        with bind_request_scope(scope):
            assert await svc.pay(10, "op-pay") == 90
        assert _marks(scope) == expected
    transport = _LoseFirstAnswer(ASGITransport(app=app), "pay")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        proxy = rpc_proxy_for(core, SERVICE, client, "http://test")
        scope = _user_scope(alice)
        with bind_request_scope(scope):
            assert await proxy.pay(10, "op-pay-rpc") == 90
    assert transport.lost == 1
    assert _marks(scope) == expected
    assert Ledger._calls == ["op-pay", "op-pay-rpc"]  # each body ran once
    assert await _effects(sessions, "op-pay-rpc") == 1


async def test_an_acknowledged_self_revocation_crosses_rpc(
    provider_core: tuple[FastAPI, CoreFacade],
) -> None:
    """(G-REVOKE-23) a request whose committed unit revoked its own subject
    ("check, then revoke") commits its later units without the re-check
    refusing them: a later command commits locally, and over RPC alike (the
    acknowledgement crosses as a signed caller fact). Without it both are
    401."""
    app, core = provider_core
    svc = core.get_service(SERVICE)
    alice = uuid.uuid4()
    claims = {"sub": str(alice), "iat_us": int((time.time() - 60) * 1_000_000)}
    assert await revoke_user_tokens(alice)

    def armed(acknowledged: bool) -> RequestScope:
        scope = _user_scope(alice, claims)
        scope.check = partial(assert_token_valid_now, claims)
        if acknowledged:
            scope.self_revoked = frozenset({str(alice)})
        return scope

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        proxy = rpc_proxy_for(core, SERVICE, client, "http://test")
        for mode, target in (("local", svc), ("split", proxy)):
            with bind_request_scope(armed(False)):
                with pytest.raises(HTTPException) as refused:
                    await target.spend(1, f"op-revoked-{mode}")
            assert refused.value.status_code == 401, mode
            with bind_request_scope(armed(True)):
                assert await target.spend(1, f"op-ack-{mode}") == 99, mode


async def test_a_durable_replay_keeps_the_acknowledged_self_revocation(
    provider_core: tuple[FastAPI, CoreFacade],
) -> None:
    """(G-REVOKE-23, replay) a command revoking its caller's own tokens
    acknowledges the self-revocation for the request's later units; its
    replay (the same caller's retry, locally or the proxy's automatic retry
    after a lost answer) acknowledges it alike, so the request's later
    commits are not refused by a revocation the request itself made."""
    app, core = provider_core
    svc = core.get_service(SERVICE)

    def armed(user: uuid.UUID) -> RequestScope:
        claims = {"sub": str(user), "iat_us": int(time.time() * 1_000_000)}
        scope = _user_scope(user, claims)
        scope.check = partial(assert_token_valid_now, claims)
        return scope

    alice, carol = uuid.uuid4(), uuid.uuid4()
    for _ in range(2):  # the first run, then the same caller's replay
        scope = armed(alice)
        with bind_request_scope(scope):
            assert await svc.revoke_self(str(alice), "op-rev") is True
        assert str(alice) in scope.self_revoked
    transport = _LoseFirstAnswer(ASGITransport(app=app), "revoke_self")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        proxy = rpc_proxy_for(core, SERVICE, client, "http://test")
        scope = armed(carol)
        with bind_request_scope(scope):
            assert await proxy.revoke_self(str(carol), "op-rev-rpc") is True
    assert transport.lost == 1
    assert str(carol) in scope.self_revoked
    assert Ledger._calls == ["op-rev", "op-rev-rpc"]


async def _stored_effects(
    sessions: async_sessionmaker[AsyncSession], method: str = "settle"
) -> list[Any]:
    """The call effects of every ``method`` record."""
    async with sessions() as db:
        rows = await db.execute(
            text("SELECT effects FROM contract_command_log WHERE contract = :c"),
            {"c": f"{SERVICE}.{method}"},
        )
        return [json.loads(raw) if raw else None for (raw,) in rows.all()]


_SETTLED = {"tagged": PAY_OUT, "closure": PAY_OUT, "failing": PAY_OUT_AND_FAIL}


@pytest.mark.parametrize("how", sorted(_SETTLED))
async def test_a_durable_replay_marks_the_request_with_its_on_commit_callbacks(
    provider_core: tuple[FastAPI, CoreFacade],
    sessions: async_sessionmaker[AsyncSession],
    how: str,
) -> None:
    """(G-SENS-04, replay, round 5) an untagged command's on_commit callback
    runs a keyed money effect after the commit (the tagged effect, a closure
    around it, or one that fails once started). The first execution marks
    its caller with money and the audit obligation; the durable record
    keeps what the callbacks left too, so a replay marks the caller alike:
    locally and over RPC (the first answer is lost, the proxy retries)."""
    app, core = provider_core
    svc = core.get_service(SERVICE)
    alice = uuid.uuid4()
    expected = ({"money": [_SETTLED[how]]}, True)
    for _ in range(2):
        scope = _user_scope(alice)
        with bind_request_scope(scope):
            assert await svc.settle("op-settle", how) == 7
        assert _marks(scope) == expected
    transport = _LoseFirstAnswer(ASGITransport(app=app), "settle")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        proxy = rpc_proxy_for(core, SERVICE, client, "http://test")
        scope = _user_scope(alice)
        with bind_request_scope(scope):
            assert await proxy.settle("op-settle-rpc", how) == 7
    assert transport.lost == 1
    assert _marks(scope) == expected
    assert Ledger._calls == ["op-settle", "op-settle-rpc"]  # each body ran once
    assert await _stored_effects(sessions) == [{"rk": {"money": [_SETTLED[how]]}}] * 2


@pytest.mark.parametrize(
    ("how", "replayed"),
    [("tagged", ({"money": [PAY_OUT]}, True)), ("closure", None)],
)
async def test_a_crash_before_the_callbacks_leaves_a_fail_closed_record(
    provider_core: tuple[FastAPI, CoreFacade],
    monkeypatch: pytest.MonkeyPatch,
    how: str,
    replayed: Any,
) -> None:
    """(G-SENS-04, round 5) the process dies between the command's commit
    and its on_commit callbacks: the record committed with the command keeps
    what the body left plus what each queued callback DECLARES
    (``@sensitive_effect``, fail closed: it may have run), so the replay of
    a tagged callback still marks money and the audit obligation; a closure
    declares nothing, and it never ran, so its replay marks nothing."""
    _, core = provider_core
    svc = core.get_service(SERVICE)
    real_dispatch = uow_module._dispatch_outbox_after_commit

    async def crash_after_commit(*_: Any, **__: Any) -> None:
        raise _Crash

    alice = uuid.uuid4()
    monkeypatch.setattr(uow_module, "_dispatch_outbox_after_commit", crash_after_commit)
    with pytest.raises(_Crash), bind_request_scope(_user_scope(alice)):
        await svc.settle("op-crash-settle", how)
    monkeypatch.setattr(uow_module, "_dispatch_outbox_after_commit", real_dispatch)
    scope = _user_scope(alice)  # the same caller's retry
    with bind_request_scope(scope):
        assert await svc.settle("op-crash-settle", how) == 7  # replayed
    assert Ledger._calls == ["op-crash-settle"]
    assert _marks(scope) == replayed


async def _cancel(task: asyncio.Task[Any], *, level: bool) -> None:
    """Cancel ``task`` like a request timeout: once (asyncio), or again at
    every await until it ends (anyio's level-triggered cancel scopes, which
    Starlette uses: a ``finally`` that awaits unshielded is cancelled too)."""
    task.cancel()
    while level and not task.done():
        await asyncio.sleep(0)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("cancel", ["once", "level"])
async def test_a_cancelled_on_commit_callback_keeps_its_effects_on_the_record(
    provider_core: tuple[FastAPI, CoreFacade],
    sessions: async_sessionmaker[AsyncSession],
    cancel: str,
) -> None:
    """(G-SENS-04, round 6) an untagged command's on_commit closure completes
    a money effect, then hangs and its request is cancelled (a timeout). The
    live request was marked with money; the durable record is amended with
    what the callbacks left on every exit after the COMMIT -- shielded from
    the cancellation -- so the same caller's retry replays it and owes the
    audit row too."""
    _, core = provider_core
    svc = core.get_service(SERVICE)
    alice = uuid.uuid4()
    first = _user_scope(alice)
    expected = ({"money": [PAY_OUT]}, True)

    async def call() -> int:
        with bind_request_scope(first):
            return int(await svc.settle_then_hang("op-hang"))

    task = asyncio.create_task(call())
    for _ in range(500):
        if Ledger._paid is not None and Ledger._paid.is_set():
            break
        await asyncio.sleep(0.01)
    assert Ledger._paid is not None and Ledger._paid.is_set()
    await _cancel(task, level=cancel == "level")
    assert _marks(first) == expected  # the live request had money
    stored: list[Any] = []
    for _ in range(500):  # the shielded amendment outlives its caller
        stored = await _stored_effects(sessions, "settle_then_hang")
        if stored != [None]:
            break
        await asyncio.sleep(0.01)
    assert stored == [{"rk": {"money": [PAY_OUT]}}]
    scope = _user_scope(alice)  # the same caller's retry
    with bind_request_scope(scope):
        assert await svc.settle_then_hang("op-hang") == 7  # replayed
    assert Ledger._calls == ["op-hang"]
    assert _marks(scope) == expected
