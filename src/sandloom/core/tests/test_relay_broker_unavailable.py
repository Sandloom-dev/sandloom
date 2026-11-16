"""``is_broker_unavailable`` decides on ``InfraError`` and keeps the raw fallbacks.

Spec 2.1: ``Unavailable`` / ``Timeout`` are outages, ``PermissionDenied`` is one
too (a bad credential must not burn event attempts), any other ``InfraError``
follows ``retryable``. A capability error in the cause chain is the driver's
verdict and wins over the raw error it wraps; a ``TypeError`` / ``ValueError`` at
the top is never an outage and a suppressed ``__context__`` is not followed.
Publishers that still raise raw errors (the volatile bus; pubsub, which
re-raises what its broadcast driver raises) stay matched by class name.
``BrokerUnavailableError`` is not one of those: it is the relay's own wrapper
(caught in the outbox store) and it stays.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, ClassVar, NoReturn

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


def _raise(error: BaseException) -> NoReturn:
    """Raise ``error``; called from an ``except`` it gets that error as ``__context__``."""
    raise error


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


def test_a_suppressed_context_is_not_followed() -> None:
    outer = RuntimeError("publish failed")
    outer.__context__ = Unavailable("down", capability=CAP)
    outer.__suppress_context__ = True  # ``raise RuntimeError(...) from None``
    assert not relay.is_broker_unavailable(outer)


def test_a_raise_from_none_inside_an_except_is_not_an_outage() -> None:
    try:
        try:
            raise Unavailable("down", capability=CAP)
        except Unavailable:
            raise RuntimeError("unrelated") from None
    except RuntimeError as err:
        assert not relay.is_broker_unavailable(err)


def test_a_cause_is_followed_even_with_a_context() -> None:
    outer = RuntimeError("publish failed")
    outer.__context__ = ValueError("handling")
    outer.__cause__ = PermissionDenied("bad credential", capability=CAP)
    assert relay.is_broker_unavailable(outer)


@pytest.mark.parametrize("poison", [TypeError("t"), ValueError("v"), UnicodeError("u")])
def test_a_poison_error_at_the_top_is_never_an_outage(poison: Exception) -> None:
    # Whether the outage sits in its ``__cause__`` or in its ``__context__``.
    via_cause = type(poison)("poison")
    via_cause.__cause__ = Unavailable("down", capability=CAP)
    via_context = type(poison)("poison")
    via_context.__context__ = Unavailable("down", capability=CAP)
    assert not relay.is_broker_unavailable(via_cause)
    assert not relay.is_broker_unavailable(via_context)


def test_a_value_error_raised_inside_except_unavailable_is_not_an_outage() -> None:
    try:
        try:
            raise Unavailable("down", capability=CAP)
        except Unavailable:
            _raise(ValueError("a bad row, found while handling the outage"))
    except ValueError as err:
        assert isinstance(err.__context__, Unavailable)
        assert not relay.is_broker_unavailable(err)


def test_a_non_poison_error_raised_inside_except_unavailable_is_an_outage() -> None:
    try:
        try:
            raise Unavailable("down", capability=CAP)
        except Unavailable:
            _raise(RuntimeError("publish failed"))
    except RuntimeError as err:
        assert relay.is_broker_unavailable(err)


def test_the_outage_reason_is_a_class_name() -> None:
    assert relay.outage_reason(Unavailable("secret://x", capability=CAP)) == (
        "Unavailable"
    )
    wrapped = _chain(RuntimeError("w"), PermissionDenied("x", capability=CAP))
    assert relay.outage_reason(wrapped) == "PermissionDenied"
    assert relay.outage_reason(OSError("reset")) == "OSError"
    assert relay.outage_reason(ValueError("poison")) is None


class ResponseError(Exception):
    """A stand-in with the redis-py class name: the relay matches by name."""


HINT = "driver says: look here"


@pytest.mark.parametrize("wrapped", [False, True], ids=["top", "in-chain"])
def test_a_hinted_infra_error_labels_the_outage_reason(wrapped: bool) -> None:
    error: BaseException = Unavailable("down", capability=CAP, hint=HINT)
    if wrapped:  # a wrapper of the publisher around the driver's error
        error = _chain(RuntimeError("w"), error)
    assert relay.outage_reason(error) == f"Unavailable ({HINT})"


def test_the_hint_of_a_raw_cause_below_the_driver_error_is_found() -> None:
    # The driver chains ``raise Unavailable(...) from exc``: the hint sits on the
    # mapped error, the raw client error below it names nothing.
    mapped = Unavailable("down", capability=CAP, hint=HINT)
    mapped.__cause__ = ResponseError("MOVED 1234 10.0.0.1:6379")
    assert relay.outage_reason(mapped) == f"Unavailable ({HINT})"


@pytest.mark.parametrize(
    "error",
    [
        ResponseError("LOADING dataset"),
        # The kernel does not read a reply word any more: a raw redirect is an
        # outage (the reply prefixes) with no label; only a driver hints.
        ResponseError("MOVED 1 10.0.0.1:6379"),
        OSError("MOVED 1 10.0.0.1:6379"),
        Unavailable("MOVED 1 10.0.0.1:6379", capability=CAP),
    ],
    ids=["LOADING", "raw-MOVED", "OSError", "Unavailable-message"],
)
def test_an_outage_without_a_hint_is_labelled_by_its_class_name(
    error: BaseException,
) -> None:
    assert relay.outage_reason(error) == type(error).__name__


@pytest.mark.parametrize(
    "hint",
    ["bad\nWARNING forged line", "bell\x07", "tab\there", "\x1b[31mred"],
    ids=["newline", "bell", "tab", "escape"],
)
def test_a_hint_with_a_control_character_is_not_printed(hint: str) -> None:
    # A third-party driver's hint never forges a log line: the line keeps the
    # class name and the outage verdict stays.
    error = Unavailable("down", capability=CAP, hint=hint)
    assert relay.outage_reason(error) == "Unavailable"
    assert relay.is_broker_unavailable(error)


def test_a_long_hint_is_cut_to_a_bound() -> None:
    reason = relay.outage_reason(Unavailable("down", capability=CAP, hint="x" * 500))
    assert reason == "Unavailable (" + "x" * 120 + ")"


def test_a_hint_of_the_bound_length_is_kept_whole() -> None:
    hint = "y" * 120
    assert relay.outage_reason(Unavailable("d", capability=CAP, hint=hint)) == (
        f"Unavailable ({hint})"
    )


def test_a_hint_never_makes_an_error_an_outage() -> None:
    # The hint labels a line only; the verdict is the class and ``retryable``.
    refused = InvalidArgument("bad", capability=CAP, hint=HINT)
    assert relay.outage_reason(refused) is None
    assert not relay.is_broker_unavailable(refused)
    # A poison error at the top stays poison, whatever its chain holds.
    poison = _chain(ValueError("x"), Unavailable("down", capability=CAP, hint=HINT))
    assert relay.outage_reason(poison) is None
    assert not relay.is_broker_unavailable(poison)


def test_a_hint_walk_over_a_cyclic_chain_terminates() -> None:
    a = Unavailable("a", capability=CAP)
    b = RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert relay.outage_reason(a) == "Unavailable"


def test_a_cyclic_chain_terminates() -> None:
    a, b = ValueError("a"), ValueError("b")
    a.__cause__, b.__cause__ = b, a
    assert not relay.is_broker_unavailable(a)


def test_the_chain_walk_is_bounded() -> None:
    limit = relay._CAUSE_CHAIN_LIMIT
    leaf = Unavailable("down", capability=CAP)

    def deep(wrappers: int) -> BaseException:
        return _chain(*[RuntimeError(str(i)) for i in range(wrappers)], leaf)

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


def _redirected() -> Unavailable:
    """An outage a driver mapped and hinted (the Redis drivers do it for MOVED)."""
    return Unavailable("down", capability=CAP, hint=HINT)


class _Redirected(RecordingTransport):
    """Every publish raises a hinted ``Unavailable``."""

    async def publish(self, stream: str, envelope: EventEnvelope) -> str:
        raise _redirected()

    async def publish_many(self, items: Any) -> list[str]:
        raise _redirected()


@pytest.mark.asyncio
async def test_the_outage_warning_carries_the_hint_of_the_driver_error(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    sessions = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    def factory() -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(session_factory=sessions)

    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)  # past grace
    async with factory() as uow:
        uow.session.add(
            OutboxMessage(
                channel="core.test_event.v1",
                schema_version="v1",
                payload={"n": 0},
                emitted_at=old,
            )
        )
    with caplog.at_level(logging.WARNING, logger=relay.__name__):
        result = await relay._relay_batch_for_store(
            factory, sql_alchemy_outbox_store(), streams_bus(_Redirected()), sweep=False
        )
    assert result.unavailable
    lines = [r.getMessage() for r in caplog.records if "broker unavailable" in r.msg]
    assert len(lines) == 1
    assert f"Unavailable ({HINT})" in lines[0]
    assert "down" not in lines[0]  # the message stays out of the line


@pytest.mark.asyncio
async def test_a_permission_denied_publish_consumes_no_attempt(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
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

    with caplog.at_level(logging.WARNING, logger=relay.__name__):
        result = await relay._relay_batch_for_store(
            factory, sql_alchemy_outbox_store(), streams_bus(_Refused()), sweep=False
        )

    assert result.unavailable and result.failed == 0 and result.delivered == 0
    # The log line names the class that decided the outage, never its message.
    lines = [r.getMessage() for r in caplog.records if "broker unavailable" in r.msg]
    assert len(lines) == 1
    assert "(PermissionDenied)" in lines[0] and "NOAUTH" not in lines[0]
    assert relay.broker_circuit().is_open()
    async with sessions() as s:
        rows = list((await s.execute(select(OutboxMessage))).scalars().all())
    assert len(rows) == 3
    assert all(r.delivery_attempts == 0 and r.delivered_at is None for r in rows)
