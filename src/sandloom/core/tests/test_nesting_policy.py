"""The nesting policies of ``sql_alchemy/nesting.py`` (risk fix 6).

Spec ``design/infra-agnostic/spec.md``, "Risk fix 6, as built"; guarantee
G-UOW-09. Two policies, chosen once per engine: independent units
(PostgreSQL) and join-the-caller-as-SAVEPOINT (SQLite).

* A Hypothesis state machine per database opens and closes writing and
  read-only units, calls contract commands (one that fails among them) and
  read contracts, rolls a savepoint back around a call, queues ``on_commit``
  callbacks and publishes outbox rows. A pure Python oracle predicts which
  rows, outbox rows and command claims are committed after every step and
  which callbacks ran (each exactly once): on PostgreSQL a nested unit
  commits on its own; on SQLite a unit opened while a unit of the task holds
  the write lock commits with it, and its callbacks wait for the outermost
  COMMIT.
* A stress test: 20 tasks nest at once in one pinned host, with random
  depths, sleeps and failures; the host's nesting gate lets one task at a
  time in, so no task's statement runs inside another's savepoint and no
  row is lost or kept wrongly.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import random
import uuid
from collections import Counter
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    delete,
    event,
    func,
    insert,
    select,
)
from sqlalchemy.ext.asyncio import async_sessionmaker

import sandloom.core.runtime as _runtime
from sandloom.core.drivers import sqlalchemy_database as driver
from sandloom.core.drivers.schema_reset import schema_reset_for
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.module_api import contract_method
from sandloom.core.sql_alchemy.models.contract_command import ContractCommandRecord
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.nesting import (
    _PINNED,
    INDEPENDENT_UNITS,
    JoinCallerAsSavepoint,
    nesting_policy,
    pin_test_host,
)
from sandloom.core.sql_alchemy.portable import transaction_options_for
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.testing import reload_test_env
from sandloom.core.testing.database import assert_disposable_test_database
from sandloom.core.testing.ownership import claim_database
from sandloom.core.testing.workers import worker_env_values
from sandloom.core.unit_of_work import db_session, open_uow
from sandloom.core.unit_of_work.dispatch import drain_post_commit
from sandloom.core.unit_of_work.effects import publish_event
from sandloom.core.unit_of_work.hooks import on_commit
from sandloom.core.unit_of_work.request import db

_REPO = Path(__file__).resolve().parents[4]
KERNEL_TABLES: list[Table] = [OutboxMessage.__table__, ContractCommandRecord.__table__]  # type: ignore[list-item]


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = _REPO / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


# -- a database with a provider -----------------------------------------------


@dataclass
class _Backend:
    """One database: its provider, the facade serving the ``Items`` contract,
    the table, channel and contract name of this run, and what the
    provider's ``on_commit`` callbacks did."""

    provider: Any
    core: CoreFacade
    table: Table
    channel: str
    ref: str
    serial: bool
    ran: list[str] = field(default_factory=list)
    #: the items of the events the bus got, in order
    published: list[int] = field(default_factory=list)

    @property
    def items(self) -> Any:
        return self.core.get_service(self.ref)

    def read_only_factory(self) -> Callable[[], SQLAlchemyUnitOfWork]:
        engine = self.provider.engine
        maker = async_sessionmaker(
            bind=engine.execution_options(
                **transaction_options_for(engine, read_only=True, snapshot=None)
            ),
            expire_on_commit=False,
        )
        return lambda: SQLAlchemyUnitOfWork(session_factory=maker)


def _items_provider(backend: _Backend) -> type:
    table, channel, ref, ran = backend.table, backend.channel, backend.ref, backend.ran

    class Items:
        """Commands write a row, publish and queue a callback; reads report
        what is committed."""

        @classmethod
        @contract_method(command=True, idempotency_key="operation_key")
        async def add(cls, item: int, operation_key: str) -> int:
            await db().execute(insert(table).values(id=item))
            await publish_event(channel, {"item": item})
            await on_commit(lambda: ran.append(f"call {item}"))
            return item

        @classmethod
        @contract_method(command=True, idempotency_key="operation_key")
        async def fail(cls, item: int, operation_key: str) -> int:
            await db().execute(insert(table).values(id=item))
            raise LookupError("the provider failed")

        @classmethod
        @contract_method
        async def ids(cls) -> list[int]:
            rows = await db().execute(select(table.c.id).order_by(table.c.id))
            return [int(i) for i in rows.scalars()]

        @classmethod
        @contract_method
        async def effects(cls) -> list[int]:
            outbox = await db().execute(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.channel == channel)
            )
            claims = await db().execute(
                select(func.count())
                .select_from(ContractCommandRecord)
                .where(ContractCommandRecord.contract.startswith(ref))
            )
            delivered = await db().execute(
                select(func.count())
                .select_from(OutboxMessage)
                .where(
                    OutboxMessage.channel == channel,
                    OutboxMessage.delivered_at.is_not(None),
                )
            )
            return [
                int(outbox.scalar_one()),
                int(claims.scalar_one()),
                int(delivered.scalar_one()),
            ]

    return Items


def _pg_url() -> str | None:
    url = worker_env_values(_REPO / ".env.test")["DATABASE_URL"]
    return url if url.startswith("postgresql") else None


async def _open_backend(url: str) -> _Backend:
    provider = driver.create_sqlalchemy_database(
        InstanceConfig(
            capability="database",
            name="nesting",
            driver="sqlalchemy",
            options={"url": url},
        )
    )
    run = uuid.uuid4().hex[:10]
    table = Table(
        f"nesting_items_{run}", MetaData(), Column("id", Integer, primary_key=True)
    )
    async with provider.engine.begin() as conn:
        await conn.run_sync(
            lambda c: OutboxMessage.metadata.create_all(c, tables=KERNEL_TABLES)
        )
        await conn.run_sync(table.metadata.create_all)
    facade = CoreFacade(app=FastAPI())
    facade.database = lambda name="default": provider  # type: ignore[method-assign]
    backend = _Backend(
        provider=provider,
        core=facade,
        table=table,
        channel=f"nesting{run}.item.added.v1",
        ref=f"nesting{run}.items.v1",
        # the oracle's expectation comes from the database, not the code
        serial=url.startswith("sqlite"),
    )
    facade.register_service(backend.ref, _items_provider(backend))
    facade.set_event_bus(_RecordingBus(backend))
    return backend


class _RecordingBus:
    """The bus the post-commit dispatch publishes to: records each event."""

    in_process = False

    def __init__(self, backend: _Backend) -> None:
        self.backend = backend

    def preflight(self, channel: str, payload: dict[str, Any]) -> None:
        pass

    async def publish_event(
        self, channel: str, payload: dict[str, Any], *_a: Any, **_k: Any
    ) -> None:
        if channel == self.backend.channel:
            self.backend.published.append(int(payload["item"]))


async def _close_backend(backend: _Backend) -> None:
    try:
        await drain_post_commit(timeout=5)
    finally:
        try:
            async with backend.provider.engine.begin() as conn:
                await conn.execute(
                    delete(OutboxMessage).where(
                        OutboxMessage.channel == backend.channel
                    )
                )
                await conn.execute(
                    delete(ContractCommandRecord).where(
                        ContractCommandRecord.contract.startswith(backend.ref)
                    )
                )
                await conn.run_sync(backend.table.metadata.drop_all)
        finally:
            await backend.provider.aclose()


def _backend_url(database: str, tmp_path: Path) -> str:
    if database == "sqlite":
        return f"sqlite+aiosqlite:///{tmp_path / 'nesting.db'}"
    url = _pg_url()
    if url is None:
        pytest.skip("the test database is not PostgreSQL (a SQLite test run)")
    assert_disposable_test_database(url)
    claim_database(url)
    return url


# -- the oracle ----------------------------------------------------------------


@dataclass
class _Layer:
    """An open unit in the model: ``own`` (a transaction of its own),
    ``nested`` (a SAVEPOINT in its caller's) or ``read`` (read-only)."""

    kind: str
    rows: list[int] = field(default_factory=list)
    outbox: list[int] = field(default_factory=list)
    claims: int = 0
    effects: list[str] = field(default_factory=list)
    #: an ``own`` unit's transaction took the write lock (SQLite: its first
    #: statement); units opened above it then nest in it
    locked: bool = False

    def take(self, other: _Layer) -> None:
        self.rows += other.rows
        self.outbox += other.outbox
        self.claims += other.claims
        self.effects += other.effects


@dataclass
class _Oracle:
    serial: bool
    stack: list[_Layer] = field(default_factory=list)
    committed: _Layer = field(default_factory=lambda: _Layer("committed"))

    def lock_held(self) -> bool:
        return self.serial and any(
            layer.kind == "nested" or (layer.kind == "own" and layer.locked)
            for layer in self.stack
        )

    def top_writer(self) -> _Layer | None:
        for layer in reversed(self.stack):
            if layer.kind != "read":
                return layer
        return None

    def touch(self, layer: _Layer) -> None:
        """A statement in ``layer``'s transaction (SQLite: the write lock)."""
        if layer.kind == "own":
            layer.locked = True

    def open(self, *, writable: bool) -> None:
        if not writable:
            self.stack.append(_Layer("read"))
        else:
            self.stack.append(_Layer("nested" if self.lock_held() else "own"))

    def close(self, *, commit: bool) -> None:
        layer = self.stack.pop()
        if not commit or layer.kind == "read":
            return
        if layer.kind == "own":
            self.committed.take(layer)
        else:  # released into its caller's transaction: commits with it
            host = self.top_writer()
            assert host is not None
            host.take(layer)

    def call(self) -> _Layer:
        """Where a contract command's effects go: the caller's transaction
        (SQLite, the task holds the lock) or their own, committed now."""
        if self.lock_held():
            host = self.top_writer()
            assert host is not None
            return host
        return self.committed


class _Driver:
    """Runs every step in one long-lived task: the units the machine opens
    stay bound in that task's context from one rule to the next."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.inbox: asyncio.Queue[tuple[Any, asyncio.Future[Any]]] = asyncio.Queue()
        self.task = loop.create_task(self._serve())
        #: a step raised: the task that owned the open units ended
        self.broken = False

    async def _serve(self) -> None:
        while True:
            job, done = await self.inbox.get()
            if job is None:
                done.set_result(None)
                return
            try:
                done.set_result(await job())
            except BaseException as exc:
                # the rule fails with it; the units' task ends with it
                done.set_exception(exc)
                raise

    def __call__(self, job: Callable[[], Awaitable[Any]] | None) -> Any:
        if self.task.done():  # a step raised: serve the teardown afresh
            self.broken = True
            self.task = self.loop.create_task(self._serve())
        done: asyncio.Future[Any] = self.loop.create_future()
        self.inbox.put_nowait((job, done))
        return self.loop.run_until_complete(done)

    def drain(self) -> None:
        """Finish the background outbox marks of what committed (the
        machine calls it while the task holds no write lock: on SQLite a
        mark would wait for the task's own lock)."""
        self.loop.run_until_complete(drain_post_commit(timeout=5))


def _machine(backend: _Backend, loop: asyncio.AbstractEventLoop) -> type:
    class NestedUnits(RuleBasedStateMachine):
        def __init__(self) -> None:
            super().__init__()
            self.run = _Driver(loop)
            self.oracle = _Oracle(serial=backend.serial)
            self.units: list[Any] = []
            self.next_id = 1
            backend.ran.clear()
            backend.published.clear()

        def _id(self) -> int:
            self.next_id += 1
            return self.next_id

        # -- units --------------------------------------------------------

        @precondition(lambda self: len(self.units) < 3)  # SQLite's pool: 5
        @rule(kind=st.sampled_from(["writer", "writer that writes", "read-only"]))
        def open_unit(self, kind: str) -> None:
            writable = kind != "read-only"
            factory = None if writable else backend.read_only_factory()
            item = self._id()

            async def job() -> list[int] | None:
                unit = open_uow(backend.core, factory=factory)
                session = await unit.__aenter__()
                self.units.append(unit)
                if kind == "writer that writes":
                    await session.execute(insert(backend.table).values(id=item))
                if writable:
                    return None
                rows = await session.execute(select(backend.table.c.id))
                return sorted(int(i) for i in rows.scalars())

            seen = self.run(job)
            self.oracle.open(writable=writable)
            top = self.oracle.stack[-1]
            if kind == "writer that writes":
                top.rows.append(item)
                self.oracle.touch(top)
            if not writable:  # a read-only unit sees what is committed
                assert seen == sorted(self.oracle.committed.rows)

        @precondition(lambda self: self.units)
        @rule(commit=st.booleans())
        def close_unit(self, commit: bool) -> None:
            unit = self.units.pop()

            async def job() -> None:
                if commit:
                    await unit.__aexit__(None, None, None)
                else:
                    failure = RuntimeError("the unit failed")
                    await unit.__aexit__(RuntimeError, failure, None)

            self.run(job)
            self.oracle.close(commit=commit)

        # -- work in the innermost writing unit ---------------------------

        def _top_writes(self) -> bool:
            return bool(self.oracle.stack) and self.oracle.stack[-1].kind != "read"

        @precondition(lambda self: self._top_writes())
        @rule()
        def write_row(self) -> None:
            item = self._id()
            self.run(lambda: db().execute(insert(backend.table).values(id=item)))
            top = self.oracle.stack[-1]
            top.rows.append(item)
            self.oracle.touch(top)

        @precondition(lambda self: self._top_writes())
        @rule()
        def publish(self) -> None:
            item = self._id()

            async def job() -> None:
                await publish_event(backend.channel, {"item": item})
                await db().flush()

            self.run(job)
            top = self.oracle.stack[-1]
            top.outbox.append(item)
            self.oracle.touch(top)

        @precondition(lambda self: self._top_writes())
        @rule()
        def queue_callback(self) -> None:
            tag = f"unit {self._id()}"
            self.run(lambda: on_commit(lambda: backend.ran.append(tag)))
            self.oracle.stack[-1].effects.append(tag)

        # -- contract calls ----------------------------------------------

        @rule()
        def call_command(self) -> None:
            item = self._id()
            self.run(lambda: backend.items.add(item, operation_key=f"k-{item}"))
            into = self.oracle.call()
            into.take(_Layer("call", [item], [item], 1, [f"call {item}"]))

        @rule()
        def call_failing_command(self) -> None:
            item = self._id()

            async def job() -> None:
                with pytest.raises(LookupError):
                    await backend.items.fail(item, operation_key=f"k-{item}")

            self.run(job)  # rolled back to its savepoint, or its own rollback

        @precondition(lambda self: self._top_writes())
        @rule(release=st.booleans())
        def savepoint_around_call(self, release: bool) -> None:
            item = self._id()

            async def job() -> None:
                savepoint = await db().begin_nested()
                await backend.items.add(item, operation_key=f"k-{item}")
                if release:
                    await savepoint.commit()
                else:
                    await savepoint.rollback()

            # The SAVEPOINT is emitted at the session's next statement: it
            # takes no lock of its own, so the call nests only when the
            # task already holds it.
            self.run(job)
            effect = _Layer("call", [item], [item], 1, [f"call {item}"])
            into = self.oracle.call()
            if into is self.oracle.committed or release:
                into.take(effect)

        # -- what the oracle predicts -------------------------------------

        @invariant()
        def committed_state_matches(self) -> None:
            drained = not self.oracle.lock_held()
            if drained:
                self.run.drain()
            ids = self.run(lambda: backend.items.ids())
            outbox, claims, delivered = self.run(lambda: backend.items.effects())
            committed = self.oracle.committed
            assert ids == sorted(committed.rows)
            assert (outbox, claims) == (len(committed.outbox), committed.claims)
            # an event is published only once its row committed, and once
            published = Counter(backend.published)
            assert all(n == 1 for n in published.values()), published
            assert published <= Counter(committed.outbox)
            if drained:  # each committed event was dispatched after its COMMIT
                assert published == Counter(committed.outbox)
                assert delivered == outbox

        @invariant()
        def callbacks_ran_exactly_once(self) -> None:
            ran = Counter(backend.ran)
            assert all(n == 1 for n in ran.values()), ran
            assert ran == Counter(self.oracle.committed.effects)

        def _abandon_units(self) -> None:
            """A step raised (the example fails): close the sessions of the
            units left open, from the new task, with no unit protocol."""
            units, self.units = self.units, []

            async def job() -> None:
                for unit in reversed(units):
                    session = getattr(unit._uow, "session", None)
                    if session is not None:
                        with contextlib.suppress(Exception):
                            await session.close()

            self.run(job)

        def teardown(self) -> None:
            try:
                if self.run.task.done() or self.run.broken:
                    self._abandon_units()
                    return
                while self.units:
                    self.close_unit(commit=False)
                self.committed_state_matches()
                self.callbacks_ran_exactly_once()
            finally:

                async def clean() -> None:
                    async with db_session() as session:
                        await session.execute(delete(backend.table))
                        await session.execute(
                            delete(OutboxMessage).where(
                                OutboxMessage.channel == backend.channel
                            )
                        )
                        await session.execute(
                            delete(ContractCommandRecord).where(
                                ContractCommandRecord.contract.startswith(backend.ref)
                            )
                        )

                try:
                    self.run(clean)
                finally:
                    self.run(None)

    return NestedUnits


#: The fast suite's run; the ``slow`` profile below explores further.
FAST = settings(
    derandomize=True,  # the fast suite runs the same examples every time
    max_examples=25,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
SLOW = settings(
    max_examples=200,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _run_machine(database: str, tmp_path: Path, profile: settings) -> None:
    url = _backend_url(database, tmp_path)
    loop = asyncio.new_event_loop()
    try:
        backend = loop.run_until_complete(_open_backend(url))
        _runtime.set_active_core(backend.core)
        try:
            run_state_machine_as_test(  # type: ignore[no-untyped-call]
                lambda: _machine(backend, loop)(), settings=profile
            )
        finally:
            _runtime.clear_active_core()
            loop.run_until_complete(_close_backend(backend))
    finally:
        loop.close()


@pytest.mark.parametrize("database", ["sqlite", "postgresql"])
def test_nested_units_match_the_oracle(database: str, tmp_path: Path) -> None:
    _run_machine(database, tmp_path, FAST)


@pytest.mark.slow
@pytest.mark.parametrize("database", ["sqlite", "postgresql"])
def test_nested_units_match_the_oracle_at_length(database: str, tmp_path: Path) -> None:
    _run_machine(database, tmp_path, SLOW)


# -- the policy is chosen once per engine -----------------------------------------


@pytest_asyncio.fixture
async def lite(tmp_path: Path) -> AsyncGenerator[_Backend, None]:
    backend = await _open_backend(f"sqlite+aiosqlite:///{tmp_path / 'lite.db'}")
    _runtime.set_active_core(backend.core)
    try:
        yield backend
    finally:
        _runtime.clear_active_core()
        await _close_backend(backend)


@pytest.mark.asyncio
async def test_the_policy_is_chosen_once_per_engine(lite: _Backend) -> None:
    policy = nesting_policy(lite.provider.session_factory)
    assert isinstance(policy, JoinCallerAsSavepoint)
    assert nesting_policy(lite.provider.session_factory) is policy
    assert lite.provider.engine.sync_engine._sandloom_nesting_policy is policy
    read_only = lite.read_only_factory()()
    assert nesting_policy(read_only._session_factory) is INDEPENDENT_UNITS
    url = _pg_url()
    if url is not None:
        provider = driver.create_sqlalchemy_database(
            InstanceConfig(
                capability="database",
                name="pg",
                driver="sqlalchemy",
                options={"url": url},
            )
        )
        try:
            assert nesting_policy(provider.session_factory) is INDEPENDENT_UNITS
        finally:
            await provider.aclose()


@pytest.mark.asyncio
async def test_a_pinned_host_is_seen_only_in_its_own_context() -> None:
    host = object()
    seen: dict[str, tuple[Any, ...]] = {}

    async def pinned() -> None:
        with pin_test_host(host):
            await asyncio.sleep(0.02)
            seen["pinned"] = _PINNED.get()

    async def beside() -> None:
        await asyncio.sleep(0.01)
        seen["beside"] = _PINNED.get()

    await asyncio.gather(pinned(), beside())
    assert seen == {"pinned": (host,), "beside": ()}
    assert _PINNED.get() == ()


@pytest.mark.asyncio
async def test_a_task_in_a_fresh_context_does_not_nest_in_a_pinned_host(
    lite: _Backend,
) -> None:
    """A task started in a fresh ``contextvars.Context`` (an in-memory bus
    delivery, a dispatcher mark) does not see the pin: its unit opens a
    transaction of its own, waits for the host's write lock, and commits
    once the host releases it."""
    engine = lite.provider.engine
    table = lite.table
    maker = async_sessionmaker(
        schema_reset_for(engine).unit_engine(engine), expire_on_commit=False
    )
    released = asyncio.Event()

    async def unit() -> bool:
        uow = SQLAlchemyUnitOfWork(lite.provider.session_factory)
        async with uow:
            assert uow.session is not None
            await uow.session.execute(insert(table).values(id=2))
            nested = uow._nested is not None
        assert released.is_set()  # it committed after the host let go
        return nested

    async with maker() as host:
        with pin_test_host(host):
            await asyncio.create_task(host.execute(insert(table).values(id=1)))
            loop = asyncio.get_running_loop()
            fresh = contextvars.Context().run(loop.create_task, unit())
            await asyncio.sleep(0.3)
            assert not fresh.done()  # waiting for the lock, not nested
            released.set()
            await host.rollback()
            assert await asyncio.wait_for(fresh, 10) is False
    async with engine.connect() as conn:
        ids = (await conn.execute(select(table.c.id))).scalars().all()
    assert list(ids) == [2]


# -- many tasks nest in one pinned host ---------------------------------------------

TASKS = 20


@dataclass
class _Plan:
    depth: int
    fail_at: int | None  # the level that raises; its parent catches it
    sleeps: list[float]

    def kept(self, task: int) -> list[int]:
        last = self.depth if self.fail_at is None else self.fail_at - 1
        return [task * 10 + level for level in range(1, last + 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_concurrent_tasks_nest_one_at_a_time(lite: _Backend, seed: int) -> None:
    """20 tasks open nested units in one pinned host at once (random depth,
    sleeps and failures): every task's committed levels are in the host, no
    failed level is, each task ends as planned, and no task's statement runs
    while another task's savepoint is open."""
    rng = random.Random(seed)
    plans = {
        task: _Plan(
            depth=(depth := rng.randint(1, 3)),
            fail_at=rng.choice([None, None, *range(1, depth + 1)]),
            sleeps=[rng.uniform(0, 0.004) for _ in range(2 * depth)],
        )
        for task in range(1, TASKS + 1)
    }
    table = lite.table
    statements: list[tuple[Any, str]] = []

    def _seen(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        statements.append((asyncio.current_task(), statement.upper()))

    async def level(task: int, plan: _Plan, k: int) -> None:
        async with db_session() as session:
            await session.execute(insert(table).values(id=task * 10 + k))
            await asyncio.sleep(plan.sleeps[2 * k - 2])
            if k == plan.fail_at:
                raise LookupError(f"task {task} fails at level {k}")
            if k < plan.depth:
                try:
                    await level(task, plan, k + 1)
                except LookupError:
                    if plan.fail_at != k + 1:
                        raise
            await asyncio.sleep(plan.sleeps[2 * k - 1])

    engine = lite.provider.engine
    maker = async_sessionmaker(
        schema_reset_for(engine).unit_engine(engine), expire_on_commit=False
    )
    async with maker() as host:
        with pin_test_host(host):
            # the host's lock is taken by a task that finished (a fixture's)
            await asyncio.create_task(host.execute(insert(table).values(id=1)))
            event.listen(engine.sync_engine, "before_cursor_execute", _seen)
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(
                        *(level(task, plan, 1) for task, plan in plans.items()),
                        return_exceptions=True,
                    ),
                    timeout=30,
                )
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", _seen)
            try:
                assert _interleaved(statements) == []
                for (task, plan), result in zip(plans.items(), results, strict=True):
                    if plan.fail_at == 1:
                        assert isinstance(result, LookupError), (task, result)
                    else:
                        assert result is None, (task, result)
                ids = (await host.execute(select(table.c.id))).scalars().all()
                expected = [1] + [
                    i for task, plan in plans.items() for i in plan.kept(task)
                ]
                assert sorted(ids) == sorted(expected)
            finally:
                await host.rollback()


def _interleaved(statements: list[tuple[Any, str]]) -> list[str]:
    """Statements of a task run while another task's savepoint was open."""
    owner: Any = None
    depth = 0
    found: list[str] = []
    for task, statement in statements:
        if depth and task is not owner:
            found.append(statement)
        if statement.startswith("SAVEPOINT"):
            owner = task
            depth += 1
        elif statement.startswith(("RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")):
            depth -= 1
    return found
