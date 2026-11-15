"""``TaskQueueConformance``: what a ``taskqueue`` driver must do (spec 8.2): the
``send`` side, then the worker host (execution, scheduler under the lease,
``check``).

The send groups send real messages: point ``make_provider`` at a test backend
(a throw-away broker or database). They send the task :attr:`known_task`, which
the kit never runs: they ask what ``send`` does, not what a worker does with
the message.

The host groups run ``provider.worker_host()`` for real, each host in a forked
process (``run_worker`` and ``run_scheduler`` block and own the process's
signals) with a kit-made ``WorkerBoot`` that reports every call and every run
through a pipe. They need a backend the forked processes share: point
``make_host_provider`` at one (default: ``make_provider``; a ``memory://``
broker is not shared). ``worker_host`` is abstract, so a driver that answers no
host fails them; a platform without ``fork`` skips them. The scheduler groups
run for a driver that declares ``periodic`` and need ``make_lease_store``.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import AbstractContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from types import TracebackType
from typing import Any, ClassVar, Self
from uuid import uuid4

import pytest

from sandloom.core.infra.errors import (
    InfraError,
    InvalidArgument,
    Timeout,
    TooLarge,
    Unavailable,
)
from sandloom.core.infra.ports.lease import LeaseHolder, LeaseProvider
from sandloom.core.infra.ports.taskqueue import (
    TASKQUEUE_CAPABILITY,
    PeriodicSpec,
    TaskQueueProvider,
    TaskSpec,
    TaskTable,
    WorkerHost,
)
from sandloom.driverkit.kits._harness import Call, ProviderConformance, require

_PAYLOAD = b'{"conformance":true}'

# ------------------------------------------------------------- host harness
#
# A host blocks and owns its process, so the kit forks one process per host. A
# fork shares what the kit made before it (the pipe, the boot, the provider):
# nothing is pickled, and a backend connection is opened only after the fork.

#: The tasks of the kit's table. Never confused with ``known_task`` (send groups).
SYNC_TASK = "sandloom.conformance.host.sync"
ASYNC_TASK = "sandloom.conformance.host.run_async"
#: The prefix of the periodic task's name: each test's table names its own
#: (``_Session.tick_task``).
TICK_TASK = "sandloom.conformance.host.tick"
UNKNOWN_HOST_TASK = "sandloom.conformance.host.unknown"
PERIODIC_ID = "sandloom-conformance-every-minute"

_HOST_PAYLOAD = {"conformance": "host", "n": 1}
#: Most a host takes to boot and run a message (a cold worker forks a pool).
_WAIT_SECONDS = 60.0
#: How long the kit watches for a message that must NOT arrive (a second run).
_SETTLE_SECONDS = 1.5
_DELAY_SECONDS = 3.0
_LEASE_TTL_SECONDS = 10


def _fork() -> Any:
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("the host groups fork one process per host: no fork here")
    return multiprocessing.get_context("fork")


def _exit_with(main: Callable[[], int]) -> None:
    code = main()
    raise SystemExit(code if isinstance(code, int) else 0)


class _Child:
    """One forked process running a host."""

    def __init__(self, name: str, main: Callable[[], int]) -> None:
        self.name = name
        self._process = _fork().Process(target=_exit_with, args=(main,), name=name)
        self.stopped = False

    def start(self) -> None:
        self._process.start()

    @property
    def exitcode(self) -> int | None:
        return self._process.exitcode  # type: ignore[no-any-return]

    def stop(self, timeout: float = 30.0) -> int | None:
        """SIGTERM, the way an orchestrator stops a host; SIGKILL after ``timeout``."""
        self.stopped = True
        if self._process.pid is None:
            return None
        if self._process.is_alive():
            self._process.terminate()
        self._process.join(timeout)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(5)
        return self.exitcode


class _Session:
    """What the forked hosts of one test report, and the children themselves."""

    def __init__(self) -> None:
        context = _fork()
        self._reader, self._writer = context.Pipe(duplex=False)
        self._lock = context.Lock()
        self.seen: list[dict[str, Any]] = []
        #: The task ids this test sent.
        self.sent: set[str] = set()
        #: The periodic task of this test's table, named for it: the scheduler
        #: sends it under an id the test does not know, so a tick another test
        #: left in the shared broker is told apart by its name (an unknown task
        #: runs nothing: it never reaches ``runs``).
        #: The last dot segment stays an identifier: a host may name a function
        #: after it (Celery does).
        self.tick_task = f"{TICK_TASK}_{uuid4().hex}"
        self._children: list[_Child] = []
        self._tmp = tempfile.TemporaryDirectory(prefix="sandloom-kit-")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for child in self._children:
            child.stop(10.0)
        self._tmp.cleanup()
        self._reader.close()
        self._writer.close()

    def path(self, name: str) -> str:
        return os.path.join(self._tmp.name, name)

    def put(self, **record: Any) -> None:
        with self._lock:
            self._writer.send(record)

    def launch(self, name: str, main: Callable[[], int]) -> _Child:
        child = _Child(name, main)
        child.start()
        self._children.append(child)
        return child

    def pump(self, timeout: float) -> None:
        if self._reader.poll(timeout):
            self.seen.append(self._reader.recv())

    def runs(self, task: str | None = None) -> list[dict[str, Any]]:
        """The runs of ``task`` this test caused: a message another run left in
        the broker (a run that failed midway) is not this test's."""
        return [
            r
            for r in self.seen
            if r["kind"] == "run"
            and (task is None or r["task"] == task)
            and (r["task"] == self.tick_task or r["task_id"] in self.sent)
        ]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.seen if r["kind"] == kind]

    def wait(
        self,
        done: Callable[[], bool],
        what: str,
        *,
        timeout: float = _WAIT_SECONDS,
    ) -> None:
        """Pump the reports until ``done()``; a host that exits first is the failure."""
        deadline = time.monotonic() + timeout
        while not done():
            self.require_alive(what)
            if time.monotonic() > deadline:
                raise AssertionError(f"no {what} within {timeout:.0f}s")
            self.pump(0.2)

    def require_alive(self, before: str) -> None:
        for child in self._children:
            if not child.stopped and child.exitcode is not None:
                raise AssertionError(
                    f"the {child.name} host exited with code {child.exitcode} "
                    f"before {before}"
                )

    def settle(self, seconds: float = _SETTLE_SECONDS) -> None:
        """Keep reading for ``seconds``: what must not happen, did not."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pump(0.1)


class _KitBoot:
    """The ``WorkerBoot`` the kit hands a host: it reports every call.

    It holds one persistent loop per process (``run_coroutine``, where an async
    task runs), made lazily so a forked process never reuses its parent's.
    """

    def __init__(self, session: _Session, *, periodic: bool = False) -> None:
        self._session = session
        self._periodic = periodic
        self._loops: dict[int, tuple[asyncio.AbstractEventLoop, threading.Thread]] = {}
        self._started: set[int] = set()

    def _report(self, kind: str, **fields: Any) -> None:
        self._session.put(kind=kind, pid=os.getpid(), at=time.time(), **fields)

    def _loop(self) -> asyncio.AbstractEventLoop:
        pid = os.getpid()
        if pid not in self._loops:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, daemon=True)
            thread.start()
            self._loops[pid] = (loop, thread)
        return self._loops[pid][0]

    def boot(self) -> TaskTable:
        self._report("boot")
        return TaskTable(
            tasks={
                SYNC_TASK: TaskSpec(),
                ASYNC_TASK: TaskSpec(),
                self._session.tick_task: TaskSpec(),
            },
            periodic=(
                [
                    PeriodicSpec(
                        id=PERIODIC_ID,
                        cron="* * * * *",
                        task=self._session.tick_task,
                    )
                ]
                if self._periodic
                else []
            ),
        )

    def before_fork(self) -> None:
        self._report("before_fork")

    def on_process_start(self) -> None:
        self._started.add(os.getpid())
        self._loop()
        self._report("start")

    def on_process_stop(self) -> None:
        entry = self._loops.pop(os.getpid(), None)
        if entry is not None:
            loop, thread = entry
            loop.call_soon_threadsafe(loop.stop)
            thread.join(2.0)
        self._report("stop")

    def run_coroutine(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop()).result()

    async def _loop_id(self) -> int:
        await asyncio.sleep(0)
        return id(asyncio.get_running_loop())

    def run(self, task: str, payload: bytes, *, task_id: str) -> None:
        try:
            asyncio.get_running_loop()
            outside_loop = False
        except RuntimeError:
            outside_loop = True
        fields: dict[str, Any] = {
            "task": task,
            "task_id": task_id,
            "payload": payload.decode(),
            "started": os.getpid() in self._started,
            "outside_loop": outside_loop,
        }
        if task == ASYNC_TASK:
            fields["loop"] = self.run_coroutine(self._loop_id())
        self._report("run", **fields)


@dataclass
class _Beat:
    """The state of two schedulers on one lease, after the first slot was sent."""

    session: _Session
    store: LeaseProvider
    lease_name: str
    first: _Child
    second: _Child
    holder: LeaseHolder


@pytest.mark.asyncio
class TaskQueueConformance(ProviderConformance[TaskQueueProvider]):
    """What a ``taskqueue`` driver must do, after the harness's checks.

    ``send`` publishes one message under the task id it is given and returns
    that id. The error classes carry the kernel's dedupe decision (G-TASK-03):
    ``InvalidArgument`` and ``TooLarge`` mean nothing was published, so the
    kernel releases the key; ``Unavailable`` and ``Timeout`` mean the publish
    may have happened, so the key stays pending. A driver that cannot tell
    which, raises the latter.
    """

    contract = TaskQueueProvider
    protocol = None
    spec = TASKQUEUE_CAPABILITY

    #: A task name the driver accepts (it is never run by the send groups).
    known_task: ClassVar[str] = "sandloom.conformance.task"
    #: A task name the driver refuses with ``InvalidArgument``. ``None``: the
    #: driver cannot tell an unknown task at ``send`` (the kernel layer checks
    #: the name before reserving), and the group is skipped.
    unknown_task: ClassVar[str | None] = None

    def _send(
        self,
        provider: TaskQueueProvider,
        task: str | None = None,
        payload: bytes = _PAYLOAD,
        *,
        task_id: str | None = None,
        delay: timedelta | None = None,
        at_least_once: bool = False,
        queue: str = "default",
    ) -> Call:
        return partial(
            provider.send,
            task or self.known_task,
            payload,
            task_id=task_id or uuid4().hex,
            delay=delay,
            at_least_once=at_least_once,
            queue=queue,
        )

    def feature_calls(self, provider: TaskQueueProvider) -> Mapping[str, Call]:
        return {"delay": self._send(provider, delay=timedelta(seconds=60))}

    def operation_calls(self, provider: TaskQueueProvider) -> Mapping[str, Call]:
        return {"send": self._send(provider)}

    # ------------------------------------------------------------ send groups
    async def test_send_returns_the_task_id_it_was_given(
        self, provider: TaskQueueProvider
    ) -> None:
        task_id = uuid4().hex
        answered = await provider.send(
            self.known_task, _PAYLOAD, task_id=task_id, delay=None
        )
        require(
            answered == task_id,
            f"send answered {answered!r} for the task id {task_id!r}: the "
            "kernel chose the id (and reserved it), so the driver publishes "
            "under it and returns it",
        )

    async def test_send_of_an_unknown_task_raises_invalid_argument(
        self, provider: TaskQueueProvider
    ) -> None:
        if self.unknown_task is None:
            pytest.skip("this driver cannot tell an unknown task at send")
        with pytest.raises(InvalidArgument):
            await provider.send(self.unknown_task, _PAYLOAD, task_id=uuid4().hex)

    @pytest.mark.parametrize("task", ["", None, 7])
    async def test_send_of_a_bad_task_name_raises_invalid_argument(
        self, provider: TaskQueueProvider, task: object
    ) -> None:
        with pytest.raises(InvalidArgument):
            await provider.send(task, _PAYLOAD, task_id=uuid4().hex)  # type: ignore[arg-type]

    @pytest.mark.parametrize("payload", [b"not json", b"[1, 2]", b'"text"', b""])
    async def test_send_refuses_a_payload_that_is_not_a_json_object(
        self, provider: TaskQueueProvider, payload: bytes
    ) -> None:
        with pytest.raises(InvalidArgument):
            await provider.send(self.known_task, payload, task_id=uuid4().hex)

    async def test_send_with_a_delay_when_the_driver_declares_delay(
        self, provider: TaskQueueProvider
    ) -> None:
        self.requires_feature(provider, "delay")
        task_id = uuid4().hex
        answered = await provider.send(
            self.known_task, _PAYLOAD, task_id=task_id, delay=timedelta(seconds=60)
        )
        require(answered == task_id, f"send with a delay answered {answered!r}")

    async def test_send_marks_at_least_once_only_when_the_driver_redelivers(
        self, provider: TaskQueueProvider
    ) -> None:
        task_id = uuid4().hex
        # Without the feature the flag is the host's business, never a refusal:
        # the kernel derives ``redelivery`` for the module, not for the message.
        answered = await provider.send(
            self.known_task, _PAYLOAD, task_id=task_id, at_least_once=True
        )
        require(answered == task_id, f"send answered {answered!r}")

    async def test_send_carries_the_queue_of_the_task(
        self, provider: TaskQueueProvider
    ) -> None:
        # Where the message lands is the host's to read; ``send`` accepts the
        # queue and publishes under the given id (no refusal for a non-default
        # queue: the kernel routes by the task's declaration, not the table).
        task_id = uuid4().hex
        answered = await provider.send(
            self.known_task, _PAYLOAD, task_id=task_id, queue="sandloom-conformance"
        )
        require(
            answered == task_id,
            f"send with a queue answered {answered!r} for the task id {task_id!r}",
        )

    async def test_send_errors_carry_the_dedupe_decision(
        self, make_failing_provider: Callable[[], TaskQueueProvider] | None
    ) -> None:
        """A backend that fails mid-send may have published: ``Unavailable`` or
        ``Timeout``, never the ``InvalidArgument`` / ``TooLarge`` that make the
        kernel release the key and let a retry publish twice."""
        if make_failing_provider is None:
            pytest.skip("no make_failing_provider: the error classes are not checked")
        built = make_failing_provider()
        try:
            await built.aopen()
        except InfraError:
            pass  # a driver may refuse to open on a dead backend
        try:
            await built.send(self.known_task, _PAYLOAD, task_id=uuid4().hex)
        except (Unavailable, Timeout):
            return
        except (InvalidArgument, TooLarge) as exc:
            raise AssertionError(
                f"send raised {type(exc).__name__} for a failing backend: that "
                "tells the kernel nothing was published, and it releases the "
                "idempotency key. Raise Unavailable or Timeout"
            ) from exc
        except InfraError:
            return
        finally:
            await built.aclose()
        raise AssertionError("send succeeded on a provider built to fail")

    # ------------------------------------------------------------ host groups
    @pytest.fixture
    def make_host_provider(
        self, make_provider: Callable[[], TaskQueueProvider]
    ) -> Callable[[], TaskQueueProvider]:
        """A provider whose backend the forked hosts and the test share (a real
        broker, not ``memory://``). Default: ``make_provider``."""
        return make_provider

    @pytest.fixture
    def make_lease_store(self) -> Callable[[], LeaseProvider] | None:
        """A shared ``lease`` store (unopened, built in each process that uses it)
        for the scheduler groups. Required when the driver declares ``periodic``
        (the groups fail without it); a driver without ``periodic`` skips them."""
        return None

    @pytest.fixture
    def scheduler_clock(
        self,
    ) -> Callable[[], AbstractContextManager[Callable[[], None]]] | None:
        """Optional: make the next cron slot of ``* * * * *`` come quickly. Called
        before the schedulers fork; the context it enters answers ``advance``, which
        the kit calls once both schedulers are up. ``None``: the real clock (the
        group then waits for a real minute boundary)."""
        return None

    def _host(
        self, make_host_provider: Callable[[], TaskQueueProvider]
    ) -> tuple[TaskQueueProvider, WorkerHost]:
        provider = make_host_provider()
        try:
            host = provider.worker_host()
        except NotImplementedError:
            host = None
        if host is None:
            raise AssertionError(
                "worker_host() answered no host: it is abstract on "
                "TaskQueueProvider (spec 8.2), so a driver returns the host that "
                "runs its worker and scheduler"
            )
        return provider, host

    @staticmethod
    def _start_worker(
        session: _Session, provider: TaskQueueProvider, boot: _KitBoot
    ) -> _Child:
        def main() -> int:
            return provider.worker_host().run_worker(
                boot, concurrency=1, loglevel="WARNING"
            )

        return session.launch("worker", main)

    @staticmethod
    async def _send_to_host(
        session: _Session,
        make_host_provider: Callable[[], TaskQueueProvider],
        *messages: tuple[str, str, timedelta | None],
    ) -> None:
        """Send ``(task, task_id, delay)`` messages from the test's own
        connection, opened after the host forked."""
        sender = make_host_provider()
        await sender.aopen()
        try:
            for task, task_id, delay in messages:
                session.sent.add(task_id)
                await sender.send(
                    task,
                    json.dumps(_HOST_PAYLOAD).encode(),
                    task_id=task_id,
                    delay=delay,
                )
        finally:
            await sender.aclose()

    async def test_host_runs_a_sync_task_once(
        self, make_host_provider: Callable[[], TaskQueueProvider]
    ) -> None:
        provider, _ = self._host(make_host_provider)
        with _Session() as session:
            self._start_worker(session, provider, _KitBoot(session))
            task_id = uuid4().hex
            await self._send_to_host(
                session, make_host_provider, (SYNC_TASK, task_id, None)
            )
            session.wait(lambda: bool(session.runs(SYNC_TASK)), "run of the sync task")
            session.settle()
            runs = session.runs(SYNC_TASK)
        require(
            len(runs) == 1,
            f"the host ran one sent message {len(runs)} times: a message runs once",
        )
        require(
            runs[0]["task_id"] == task_id,
            f"the host ran the message as task id {runs[0]['task_id']!r}, "
            f"sent as {task_id!r}",
        )
        require(
            json.loads(runs[0]["payload"]) == _HOST_PAYLOAD,
            f"the host ran the task with payload {runs[0]['payload']!r}, "
            "not the one sent",
        )

    async def test_host_runs_an_async_task_once(
        self, make_host_provider: Callable[[], TaskQueueProvider]
    ) -> None:
        provider, _ = self._host(make_host_provider)
        with _Session() as session:
            self._start_worker(session, provider, _KitBoot(session))
            task_id = uuid4().hex
            await self._send_to_host(
                session, make_host_provider, (ASYNC_TASK, task_id, None)
            )
            session.wait(
                lambda: bool(session.runs(ASYNC_TASK)), "run of the async task"
            )
            session.settle()
            runs = session.runs(ASYNC_TASK)
        require(len(runs) == 1, f"the host ran an async task {len(runs)} times")
        require(
            runs[0]["task_id"] == task_id and "loop" in runs[0],
            "the host did not run the async task's coroutine to its end with "
            "the message's id",
        )

    async def test_host_does_not_run_a_delayed_task_before_its_delay(
        self, make_host_provider: Callable[[], TaskQueueProvider]
    ) -> None:
        provider, _ = self._host(make_host_provider)
        self.requires_feature(provider, "delay")
        with _Session() as session:
            self._start_worker(session, provider, _KitBoot(session))
            # Consuming before the send: a worker that boots slowly would
            # otherwise hide a driver that ignores the delay.
            session.wait(lambda: bool(session.of("start")), "process start")
            sent_at = time.time()
            await self._send_to_host(
                session,
                make_host_provider,
                (SYNC_TASK, uuid4().hex, timedelta(seconds=_DELAY_SECONDS)),
            )
            session.wait(
                lambda: bool(session.runs(SYNC_TASK)),
                "run of the delayed task",
                timeout=_WAIT_SECONDS + _DELAY_SECONDS,
            )
            ran_at = session.runs(SYNC_TASK)[0]["at"]
        require(
            ran_at - sent_at >= _DELAY_SECONDS - 0.25,
            f"the host ran a task sent with a {_DELAY_SECONDS:.0f}s delay "
            f"{ran_at - sent_at:.1f}s after the send",
        )

    async def test_host_runs_nothing_for_an_unknown_task(
        self, make_host_provider: Callable[[], TaskQueueProvider]
    ) -> None:
        provider, _ = self._host(make_host_provider)
        with _Session() as session:
            self._start_worker(session, provider, _KitBoot(session))
            try:
                await self._send_to_host(
                    session,
                    make_host_provider,
                    (UNKNOWN_HOST_TASK, uuid4().hex, None),
                    (SYNC_TASK, uuid4().hex, None),
                )
            except InvalidArgument:
                return  # refused at send: nothing was published, nothing can run
            # The known task is the marker: the host has read past the unknown one.
            session.wait(lambda: bool(session.runs(SYNC_TASK)), "run of the marker")
            session.settle()
            unknown = session.runs(UNKNOWN_HOST_TASK)
        require(
            not unknown,
            "the host ran a task that is not in its task table: an unknown "
            "task runs nothing",
        )

    async def test_host_keeps_one_loop_per_process_and_stops_each_process(
        self, make_host_provider: Callable[[], TaskQueueProvider]
    ) -> None:
        provider, _ = self._host(make_host_provider)
        with _Session() as session:
            worker = self._start_worker(session, provider, _KitBoot(session))
            session.wait(lambda: bool(session.of("before_fork")), "host boot")
            await self._send_to_host(
                session,
                make_host_provider,
                *[(ASYNC_TASK, uuid4().hex, None) for _ in range(3)],
            )
            session.wait(lambda: len(session.runs(ASYNC_TASK)) >= 3, "three async runs")
            code = worker.stop()
            session.settle(0.5)
            runs = session.runs(ASYNC_TASK)
            stopped = {r["pid"] for r in session.of("stop")}
            booted = session.of("boot")
        require(len(booted) == 1, f"the host booted the kernel {len(booted)} times")
        require(
            all(r["started"] for r in runs),
            "the host ran a message in a process it never started "
            "(on_process_start): the loop of that process does not exist yet",
        )
        require(
            all(r["outside_loop"] for r in runs),
            "the host ran a message inside a running event loop: run() is "
            "called outside any loop",
        )
        loops: dict[int, set[int]] = {}
        for r in runs:
            loops.setdefault(r["pid"], set()).add(r["loop"])
        require(
            all(len(ids) == 1 for ids in loops.values()),
            f"the host's processes ran async tasks on {loops}: one loop per "
            "process, not one per message",
        )
        require(
            set(loops) <= stopped,
            f"processes {sorted(set(loops) - stopped)} ran messages and were "
            "never stopped (on_process_stop)",
        )
        require(code == 0, f"the host exited with code {code} after SIGTERM, not 0")

    @asynccontextmanager
    async def _schedulers(
        self,
        make_host_provider: Callable[[], TaskQueueProvider],
        make_lease_store: Callable[[], LeaseProvider] | None,
        scheduler_clock: Callable[[], AbstractContextManager[Callable[[], None]]]
        | None,
    ) -> AsyncIterator[_Beat]:
        """Two schedulers on one lease and a worker, the first holding the lease
        and the periodic task sent once for its slot; the second stands by.

        Yielded after the first run of the periodic task arrived. The clock the
        driver's ``scheduler_clock`` froze stays inside that slot until the
        context ends.
        """
        provider, _ = self._host(make_host_provider)
        self.requires_feature(provider, "periodic")
        if make_lease_store is None:
            raise AssertionError(
                "the driver declares 'periodic' but the kit has no "
                "make_lease_store: the scheduler groups need a shared lease store "
                "to run two schedulers on (override the make_lease_store fixture)"
            )
        lease_name = f"sandloom:conformance:beat:{uuid4().hex}"
        clock = scheduler_clock() if scheduler_clock else nullcontext(lambda: None)
        store = make_lease_store()
        await store.aopen()
        try:
            with _Session() as session, clock as advance:
                boot = _KitBoot(session, periodic=True)
                self._start_worker(session, provider, boot)

                def scheduler(name: str) -> _Child:
                    state = session.path(f"{name}-state")

                    def main() -> int:
                        leased = make_lease_store()
                        boot.run_coroutine(leased.aopen())
                        return provider.worker_host().run_scheduler(
                            boot,
                            lease=leased,
                            lease_name=lease_name,
                            lease_ttl_seconds=_LEASE_TTL_SECONDS,
                            loglevel="WARNING",
                            state_file=state,
                        )

                    return session.launch(name, main)

                first = scheduler("first")
                holder = None
                deadline = time.monotonic() + _WAIT_SECONDS
                while holder is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.2)
                    holder = await store.holder(lease_name)
                    session.require_alive("a scheduler took the lease")
                    session.pump(0)
                if holder is None:
                    raise AssertionError(
                        "no scheduler took the lease it was given within "
                        f"{_WAIT_SECONDS:.0f}s"
                    )
                boots = len(session.of("boot"))
                second = scheduler("second")
                session.wait(
                    lambda: len(session.of("boot")) > boots, "second scheduler start"
                )
                session.settle(2.0)  # the second one tries the lease and stands by
                advance()
                session.wait(
                    lambda: bool(session.runs(session.tick_task)),
                    "run of the periodic task",
                    timeout=_WAIT_SECONDS + 70.0,
                )
                session.settle(3.0)
                yield _Beat(session, store, lease_name, first, second, holder)
        finally:
            await store.aclose()

    async def test_scheduler_sends_once_per_slot_while_holding_the_lease(
        self,
        make_host_provider: Callable[[], TaskQueueProvider],
        make_lease_store: Callable[[], LeaseProvider] | None,
        scheduler_clock: Callable[[], AbstractContextManager[Callable[[], None]]]
        | None,
    ) -> None:
        async with self._schedulers(
            make_host_provider, make_lease_store, scheduler_clock
        ) as beat:
            ticks = beat.session.runs(beat.session.tick_task)
            after = await beat.store.holder(beat.lease_name)
            beat.first.stop()
        require(
            len(ticks) == 1,
            f"two schedulers on one lease sent the periodic task {len(ticks)} "
            "times for one slot: the holder sends once per slot and a second "
            "scheduler sends nothing",
        )
        require(
            after is not None and after.owner == beat.holder.owner,
            "the lease changed hands while the first scheduler was running",
        )

    async def test_scheduler_that_takes_over_inside_a_slot_does_not_send_it_again(
        self,
        make_host_provider: Callable[[], TaskQueueProvider],
        make_lease_store: Callable[[], LeaseProvider] | None,
        scheduler_clock: Callable[[], AbstractContextManager[Callable[[], None]]]
        | None,
    ) -> None:
        """The holder sent slot S and stops; the standby takes the lease inside
        the same slot (the frozen clock never leaves it): S is sent once, not
        again by the new holder."""
        async with self._schedulers(
            make_host_provider, make_lease_store, scheduler_clock
        ) as beat:
            before = len(beat.session.runs(beat.session.tick_task))
            beat.first.stop()
            taken = None
            deadline = time.monotonic() + _LEASE_TTL_SECONDS + _WAIT_SECONDS
            while taken is None and time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                now = await beat.store.holder(beat.lease_name)
                if now is not None and now.owner != beat.holder.owner:
                    taken = now
                beat.session.pump(0)
                beat.session.require_alive("the second scheduler took the lease")
            require(
                taken is not None,
                "the second scheduler did not take the lease after the first one "
                f"stopped, within {_LEASE_TTL_SECONDS + _WAIT_SECONDS:.0f}s",
            )
            beat.session.settle(4.0)  # the new holder's ticks inside the same slot
            ticks = beat.session.runs(beat.session.tick_task)
        require(
            before == 1,
            f"the first scheduler sent the periodic task {before} times for one slot",
        )
        require(
            len(ticks) == 1,
            f"after a takeover inside the slot the periodic task was sent "
            f"{len(ticks)} times: the slot the first holder sent is not sent "
            "again by the lease's next holder",
        )

    async def test_check_reports_a_live_worker(
        self, make_host_provider: Callable[[], TaskQueueProvider]
    ) -> None:
        provider, host = self._host(make_host_provider)
        require(
            host.check(timeout_seconds=2.0).status != "ok",
            "check reported a live worker before any was started",
        )
        with _Session() as session:
            worker = self._start_worker(session, provider, _KitBoot(session))
            deadline = time.monotonic() + _WAIT_SECONDS
            live = False
            while not live and time.monotonic() < deadline:
                live = host.check(timeout_seconds=5.0).status == "ok"
                if not live:
                    session.require_alive("check reported it live")
                    session.settle(0.2)
            require(live, "check never reported the running worker as live")
            worker.stop()
            after = host.check(timeout_seconds=2.0)
        require(
            after.status != "ok",
            "check still reported a worker as live after it was stopped",
        )


__all__ = ["TaskQueueConformance"]
