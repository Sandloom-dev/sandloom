"""``is_broker_unavailable`` decides on ``InfraError`` and keeps the raw fallbacks.

Spec 2.1: ``Unavailable`` / ``Timeout`` are outages, ``PermissionDenied`` is one
too (a bad credential must not burn event attempts), any other ``InfraError``
follows ``retryable``. A capability error in the cause chain is the driver's
verdict and wins over the raw error it wraps. Publishers that still raise raw
errors (volatile bus, pubsub, Celery) stay matched by class name.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, ClassVar

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sandloom.core.eventing.envelope import EventEnvelope
from sandloom.core.infra.contracts import BrokerUnavailableError
from sandloom.core.infra.errors import (
    InfraError,
    InvalidArgument,
    NotFound,
    PermissionDenied,
    Timeout,
    Unavailable,
)
from sandloom.core.infra.ports.eventbus import PartialPublish
from sandloom.core.sql_alchemy.models.outbox import OutboxMessage
from sandloom.core.sql_alchemy.outbox_store import sql_alchemy_outbox_store
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.tasks import relay
from sandloom.core.testing import reload_test_env
from sandloom.core.tests._pipelined_support import RecordingTransport, streams_bus
from sandloom.core.unit_of_work import dispatch as uow_mod_dispatch

CAP = "eventbus"


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest.fixture(autouse=True)
def _reset_circuit() -> Any:
    relay.broker_circuit().reset()
    yield
    relay.broker_circuit().reset()


def _chain(*errors: BaseException) -> BaseException:
    """``errors[0]`` caused by ``errors[1]`` caused by ... (``__cause__``)."""
    for outer, inner in pairwise(errors):
        outer.__cause__ = inner
    return errors[0]


# -- InfraError ----------------------------------------------------------------
def test_unavailable_is_an_outage() -> None:
    assert relay.is_broker_unavailable(Unavailable("down", capability=CAP))


def test_timeout_is_an_outage() -> None:
    assert relay.is_broker_unavailable(Timeout("slow", capability=CAP))


def test_permission_denied_is_an_outage_although_not_retryable() -> None:
    err = PermissionDenied("bad credential", capability=CAP)
    assert PermissionDenied.retryable is False
    assert relay.is_broker_unavailable(err)


def test_a_non_retryable_infra_error_is_not_an_outage() -> None:
    assert not relay.is_broker_unavailable(InvalidArgument("bad", capability=CAP))
    assert not relay.is_broker_unavailable(NotFound("gone", capability=CAP))


def test_another_infra_error_follows_retryable() -> None:
    class _Overloaded(InfraError):
        retryable: ClassVar[bool] = True

    assert relay.is_broker_unavailable(_Overloaded("busy", capability=CAP))


def test_a_mapped_error_wins_over_the_raw_error_it_wraps() -> None:
    """A driver that mapped an OSError to InvalidArgument made a verdict."""
    mapped = InvalidArgument("payload refused", capability=CAP)
    mapped.__cause__ = ConnectionRefusedError("raw")
    assert not relay.is_broker_unavailable(mapped)


def test_partial_publish_is_judged_by_its_cause_not_its_retryable() -> None:
    """The batch wrapper is retryable for any cause: a poison row is no outage."""

    def partial(cause: BaseException) -> PartialPublish:
        err = PartialPublish("batch failed")
        err.__cause__ = cause
        return err

    assert partial(RuntimeError("poison")).retryable is True
    assert not relay.is_broker_unavailable(partial(RuntimeError("poison")))
    assert not relay.is_broker_unavailable(
        partial(InvalidArgument("bad", capability=CAP))
    )
    assert relay.is_broker_unavailable(partial(ConnectionRefusedError("raw")))
    assert relay.is_broker_unavailable(partial(Unavailable("down", capability=CAP)))
    assert relay.is_broker_unavailable(
        partial(PermissionDenied("NOAUTH", capability=CAP))
    )
    assert not relay.is_broker_unavailable(PartialPublish("no cause"))


# -- raw errors (publishers that do not map yet) ---------------------------------
def test_raw_class_name_match_still_recognised() -> None:
    class ConnectionError(Exception):  # mimics redis-py's name
        pass

    class BusyLoadingError(ConnectionError):
        pass

    assert relay.is_broker_unavailable(ConnectionError("refused"))
    assert relay.is_broker_unavailable(BusyLoadingError("loading"))
    assert relay.is_broker_unavailable(BrokerUnavailableError("celery down"))
    assert relay.is_broker_unavailable(OSError("reset"))


def test_a_raw_non_outage_error_is_not_an_outage() -> None:
    assert not relay.is_broker_unavailable(ValueError("poison"))


# -- cause chain -----------------------------------------------------------------
def test_infra_error_wrapped_in_a_cause_chain_is_found() -> None:
    err = _chain(
        RuntimeError("publish failed"),
        ValueError("batch"),
        PermissionDenied("bad credential", capability=CAP),
    )
    assert relay.is_broker_unavailable(err)


def test_infra_error_in_the_context_chain_is_found() -> None:
    outer = RuntimeError("publish failed")
    outer.__context__ = Unavailable("down", capability=CAP)
    assert relay.is_broker_unavailable(outer)


def test_a_cyclic_chain_terminates() -> None:
    a, b = ValueError("a"), ValueError("b")
    a.__cause__, b.__cause__ = b, a
    assert not relay.is_broker_unavailable(a)


def test_the_chain_walk_is_bounded() -> None:
    limit = relay._CAUSE_CHAIN_LIMIT
    leaf = Unavailable("down", capability=CAP)

    def deep(wrappers: int) -> BaseException:
        return _chain(*[ValueError(str(i)) for i in range(wrappers)], leaf)

    assert relay.is_broker_unavailable(deep(limit - 1))
    assert not relay.is_broker_unavailable(deep(limit))


# -- the relay's attempt accounting ----------------------------------------------
@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not configured for tests")
    eng = create_async_engine(url, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: OutboxMessage.__table__.drop(c, checkfirst=True))
        await conn.run_sync(OutboxMessage.metadata.create_all)
    yield eng
    await uow_mod_dispatch.drain_post_commit()
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM outbox_messages"))
    await eng.dispose()


class _Refused(RecordingTransport):
    """Every publish is refused with a mapped ``PermissionDenied``."""

    async def publish(self, stream: str, envelope: EventEnvelope) -> str:
        raise PermissionDenied("NOAUTH", capability=CAP)

    async def publish_many(self, items: Any) -> list[str]:
        raise PermissionDenied("NOAUTH", capability=CAP)


@pytest.mark.asyncio
async def test_a_permission_denied_publish_consumes_no_attempt(
    engine: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    def factory() -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(session_factory=sessions)

    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)  # past grace
    async with factory() as uow:
        for i in range(3):
            uow.session.add(
                OutboxMessage(
                    channel="core.test_event.v1",
                    schema_version="v1",
                    payload={"n": i},
                    emitted_at=old + timedelta(milliseconds=i),
                )
            )

    result = await relay._relay_batch_for_store(
        factory, sql_alchemy_outbox_store(), streams_bus(_Refused()), sweep=False
    )

    assert result.unavailable and result.failed == 0 and result.delivered == 0
    assert relay.broker_circuit().is_open()
    async with sessions() as s:
        rows = list((await s.execute(select(OutboxMessage))).scalars().all())
    assert len(rows) == 3
    assert all(r.delivery_attempts == 0 and r.delivered_at is None for r in rows)
