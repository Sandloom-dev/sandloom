"""The outbox relay judging what the Redis Streams driver raises (database test).

The relay trusts the mapped ``InfraError``: a retryable one is an outage (no
attempt spent, channel held, circuit open), anything else spends an attempt and
ends in the dead-letter state. These tests run the real relay against the real
``RedisStreamsTransport`` over a scripted client, so the driver's mapping and
the relay's verdict are one path:

- an unknown ``ERR ...`` reply (an oversized value, a script error) consumes an
  attempt and reaches the dead letters after ``OUTBOX_RELAY_MAX_ATTEMPTS``,
  where mapped to ``Unavailable`` it parked the row for ever;
- a ``PartialPublish`` built by the driver's batch path is judged by its MAPPED
  cause (``PermissionDenied`` is an outage, the raw ``NoPermissionError`` below
  it is not recognised by the relay).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from redis import exceptions as rx
from sandloom_redis.eventbus.streams_transport import RedisStreamsTransport
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from test_redis_errors import _PipelineClient, _streams_transport

from sandloom.core.config.settings import settings as _settings
from sandloom.core.eventing.streams_bus import StreamsEventBus
from sandloom.core.infra.contracts import RelayBatchResult
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_outbox_store
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.tasks import relay
from sandloom.core.testing import reload_test_env
from sandloom.driverkit import (
    InfraError,
    InvalidArgument,
    PermissionDenied,
    Unavailable,
)

CHANNEL = "core.test_event.v1"
MAX_ATTEMPTS = 3


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[3] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest.fixture(autouse=True)
def _reset_circuit() -> Any:
    relay.broker_circuit().reset()
    yield
    relay.broker_circuit().reset()


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    url = os.getenv("DATABASE_URL")
    if not url:
        # never a skip: the redis-drivers lane has its PostgreSQL service, and a
        # lane that cannot reach it must turn red, not green with no test run
        pytest.fail("DATABASE_URL not configured for tests (.env.test or the lane)")
    eng = create_async_engine(url, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: OutboxMessage.__table__.drop(c, checkfirst=True))
        await conn.run_sync(OutboxMessage.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: OutboxMessage.__table__.drop(c, checkfirst=True))
    await eng.dispose()


def _bus(transport: RedisStreamsTransport) -> StreamsEventBus:
    registry = InMemorySchemaRegistry()
    registry.register(
        CHANNEL,
        "test",
        {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
    )
    return StreamsEventBus(registry, transport, poll_interval=0.01)


async def _seed(sessions: async_sessionmaker[AsyncSession], rows: int) -> None:
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)  # past grace
    async with SQLAlchemyUnitOfWork(session_factory=sessions) as uow:
        for i in range(rows):
            uow.session.add(
                OutboxMessage(
                    channel=CHANNEL,
                    schema_version="v1",
                    payload={"n": i},
                    emitted_at=old + timedelta(milliseconds=i),
                )
            )


async def _rows(sessions: async_sessionmaker[AsyncSession]) -> list[OutboxMessage]:
    async with sessions() as session:
        result = await session.execute(
            select(OutboxMessage).order_by(OutboxMessage.emitted_at)
        )
        return list(result.scalars().all())


async def _relay_once(
    sessions: async_sessionmaker[AsyncSession], bus: StreamsEventBus
) -> RelayBatchResult:
    def factory() -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(session_factory=sessions)

    async with sessions.begin() as session:  # a retry is due now, not after backoff
        await session.execute(update(OutboxMessage).values(next_attempt_at=None))
    return await relay._relay_batch_for_store(
        factory, sql_alchemy_outbox_store(), bus, sweep=False
    )


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


def _unknown_err() -> BaseException:
    return rx.ResponseError(
        "ERR Protocol error: invalid bulk length redis.internal:6379"
    )


@pytest.mark.parametrize("n_rows", [1, 3], ids=["per-row-path", "batch-path"])
@pytest.mark.parametrize("shape", ["raises", "result-error"])
async def test_an_unknown_err_reply_consumes_attempts_and_reaches_the_dead_letters(
    sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    n_rows: int,
    shape: str,
) -> None:
    monkeypatch.setattr(_settings, "outbox_relay_max_attempts", MAX_ATTEMPTS)
    await _seed(sessions, n_rows)
    outcome: BaseException | list[Any] = (
        _unknown_err() if shape == "raises" else [_unknown_err()] * n_rows
    )
    bus = _bus(_streams_transport(_UnknownErrClient(outcome)))

    first = await _relay_once(sessions, bus)

    # an attempt was spent: this is no outage, so the circuit stays closed
    assert not first.unavailable
    assert not relay.broker_circuit().is_open()
    head = (await _rows(sessions))[0]
    assert head.delivery_attempts == 1 and head.dead_at is None

    for _ in range(MAX_ATTEMPTS - 1):
        await _relay_once(sessions, bus)

    head = (await _rows(sessions))[0]
    assert head.delivery_attempts == MAX_ATTEMPTS
    assert head.dead_at is not None  # parked for ever before: now dead-lettered
    assert head.delivered_at is None
    assert "redis.internal" not in (head.last_error or "")


class _UnknownErrClient(_PipelineClient):
    """The batch path answers ``outcome``; the per-row ``XADD`` raises it."""

    async def xadd(self, *args: Any, **kwargs: Any) -> str:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        raise self.outcome[0]


async def test_a_transient_reply_is_still_an_outage(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the same branch: ``LOADING`` spends no attempt."""
    monkeypatch.setattr(_settings, "outbox_relay_max_attempts", MAX_ATTEMPTS)
    await _seed(sessions, 3)
    loading = rx.ResponseError("LOADING Redis is loading the dataset in memory")
    bus = _bus(_streams_transport(_UnknownErrClient(loading)))

    result = await _relay_once(sessions, bus)

    assert result.unavailable
    assert relay.broker_circuit().is_open()
    assert all(r.delivery_attempts == 0 for r in await _rows(sessions))


# (the raw error, the mapped class, whether the relay must call it an outage)
BATCH_CASES: list[tuple[str, Callable[[], BaseException], type[InfraError], bool]] = [
    ("unknown-ERR", _unknown_err, InvalidArgument, False),
    ("NOPERM-class", lambda: rx.NoPermissionError("NOPERM x"), PermissionDenied, True),
    ("NOPERM-text", lambda: rx.ResponseError("NOPERM x"), PermissionDenied, True),
    ("LOADING-text", lambda: rx.ResponseError("LOADING x"), Unavailable, True),
    ("connection", lambda: rx.ConnectionError("down"), Unavailable, True),
]


@pytest.mark.parametrize(("_id", "make", "mapped", "outage"), BATCH_CASES)
@pytest.mark.parametrize("shape", ["execute-raises", "error-in-exec-results"])
async def test_a_partial_publish_is_judged_by_its_mapped_cause(
    _id: str,
    make: Callable[[], BaseException],
    mapped: type[InfraError],
    outage: bool,
    shape: str,
) -> None:
    raw = make()
    transport = _streams_transport(
        _PipelineClient(raw if shape == "execute-raises" else ["1-0", raw])
    )
    items: Any = [("s", {"id": "1"}), ("s", {"id": "2"})]
    with pytest.raises(Exception) as caught:
        await transport.publish_many(items)  # the batch path, as the driver builds it

    partial = caught.value
    assert type(partial.__cause__) is mapped
    assert partial.__cause__.__cause__ is raw
    assert relay.is_broker_unavailable(partial) is outage
    # the relay stops at the mapped error: its verdict never depends on the raw one
    assert relay.outage_reason(partial) == (mapped.__name__ if outage else None)
