"""The claims of ``docs/modules/between/events.md`` that were wrong, pinned to the code.

* the production default bus must be durable, whatever risk is accepted;
* a payload that fails validation fails before the handler, is not poison, and
  with ``dead_letter=False`` is retried (never parked);
* a handler error rolls the consumer unit back (G-EVENT-05), the redelivery
  applies it;
* a renamed consumer is a new group with an empty inbox.

The two database tests run on the PostgreSQL lane like the other
``consumer_inbox`` tests.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import sandloom.core.app_factory as app_factory
from sandloom.core.config.common import Environment
from sandloom.core.config.settings import settings
from sandloom.core.consumers.callbacks import (
    build_consumer_callback,
    consumer_group_name,
)
from sandloom.core.drivers.bootstrap import build_infra_registry
from sandloom.core.eventing.delivery import PoisonEventError, stream_start_id
from sandloom.core.eventing.streams_dispatch import _StreamsDispatch
from sandloom.core.facade import CoreFacade
from sandloom.core.infra.config import InstanceConfig
from sandloom.core.schema.registry import InMemorySchemaRegistry
from sandloom.core.sql_alchemy.durable_inbox import DurableConsumerInbox
from sandloom.core.sql_alchemy.models.consumer_inbox import ConsumerInboxEntry
from sandloom.core.sql_alchemy.uow import SQLAlchemyUnitOfWork
from sandloom.core.testing import reload_test_env
from sandloom.sdk import Event, on
from sandloom.sdk._decl.events import consumer_callback

# --------------------------------------------------------- the default bus --


def test_prod_default_volatile_bus_is_refused_even_with_the_accepted_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``volatile_event_bus`` admits a volatile driver on another instance; the
    default bus of a production app stays durable whatever is accepted."""
    accepted = settings.model_copy(
        update={
            "environment": Environment.prod,
            "sandloom_accepted_risks": ["volatile_event_bus"],
        }
    )
    schema_registry = InMemorySchemaRegistry()
    config = {
        "eventbus": {
            name: InstanceConfig(capability="eventbus", name=name, driver="memory")
            for name in ("default", "side")
        }
    }

    def _registry(registry: InMemorySchemaRegistry) -> Any:
        return build_infra_registry(
            registry, settings=accepted, config=config, load_plugins=False
        )

    monkeypatch.setattr(app_factory, "build_infra_registry", _registry)
    monkeypatch.setattr(settings, "environment", Environment.prod)
    monkeypatch.setattr(settings, "sandloom_accepted_risks", ["volatile_event_bus"])
    core = CoreFacade(app=app_factory.FastAPI())
    core.register_schema_registry(schema_registry)

    with pytest.raises(RuntimeError, match="does not admit a volatile default bus"):
        app_factory._bind_infrastructure(core, schema_registry)
    # The risk did admit the driver itself, on the default instance too: the
    # refusal above is the startup check, not the registry.
    assert core.eventbus_provider("default").bus() is not None
    assert core.eventbus_provider("side").bus() is not None


# ----------------------------------------------------------- poison events --


class Created(Event, channel="evclaims.thing.created.v1"):
    qty: int


def _callback(handler: Any, *, dead_letter: bool) -> Any:
    @asynccontextmanager
    async def opener(_name: str) -> AsyncIterator[object]:
        yield object()

    return build_consumer_callback(
        name="evclaims:handle",
        database="default",
        handler=handler,
        durable=False,
        opener=opener,
        metric_processed="event_consumer_events_processed_total",
        metric_failed="event_consumer_events_failed_total",
        labels={"consumer": "evclaims:handle"},
        dead_letter=dead_letter,
    )


async def test_a_payload_failing_validation_never_reaches_the_handler_and_is_not_poison() -> (
    None
):
    reached: list[Created] = []

    @on(Created, dead_letter=False)
    async def handle(event: Created) -> None:
        reached.append(event)

    (spec,) = handle.__sandloom_consumers__  # type: ignore[attr-defined]
    callback = _callback(consumer_callback(spec), dead_letter=spec.dead_letter)

    with pytest.raises(ValidationError) as failure:
        await callback({"id": "e1", "payload": {"qty": "many"}})
    assert not isinstance(failure.value, PoisonEventError)
    assert reached == []
    # The bus reads it as a plain failure (redelivery), never as poison, and a
    # dead_letter=False consumer is exempt from the delivery budget.
    outcome = await _StreamsDispatch._run_callbacks_outcome(
        Created.channel, [callback], {"id": "e1", "payload": {"qty": "many"}}
    )
    assert outcome == "failed"
    assert callback.sandloom_never_dead_letter is True

    await callback({"id": "e2", "payload": {"qty": 3}})
    assert [event.qty for event in reached] == [3]


async def test_poison_raised_by_the_handler_is_dead_lettered_at_once() -> None:
    async def handler(_session: object, _envelope: object) -> None:
        raise PoisonEventError("never applies")

    callback = _callback(handler, dead_letter=False)
    outcome = await _StreamsDispatch._run_callbacks_outcome(
        Created.channel, [callback], {"id": "e1", "payload": {}}
    )
    assert outcome == "poison"


# --------------------------------------------------- consumer unit (real DB) --


@pytest.fixture(scope="module", autouse=True)
def _load_test_env() -> None:
    env_test = Path(__file__).resolve().parents[4] / ".env.test"
    if env_test.exists():
        reload_test_env(env_test)


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.fail("DATABASE_URL not configured for tests")
    engine: AsyncEngine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: ConsumerInboxEntry.__table__.create(c, checkfirst=True)
        )
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    yield factory
    async with factory() as session, session.begin():
        await session.execute(
            ConsumerInboxEntry.__table__.delete().where(
                ConsumerInboxEntry.consumer.like("evclaims%")
            )
        )
    await engine.dispose()


async def _inbox_rows(
    factory: async_sessionmaker[AsyncSession], event_id: str
) -> dict[str, int]:
    async with factory() as session:
        rows = await session.execute(
            select(ConsumerInboxEntry.consumer, func.count())
            .where(
                ConsumerInboxEntry.consumer.like("evclaims%"),
                ConsumerInboxEntry.event_id == event_id,
            )
            .group_by(ConsumerInboxEntry.consumer)
        )
        return dict(rows.all())  # type: ignore[arg-type]


class _Boom(Exception):
    pass


async def test_a_handler_error_rolls_back_the_consumer_unit(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """G-EVENT-05: the handler's database writes and the inbox claim roll back
    together on an error; the redelivery claims and applies the event."""

    @asynccontextmanager
    async def opener(_name: str) -> AsyncIterator[AsyncSession]:
        async with SQLAlchemyUnitOfWork(session_factory=sessionmaker) as uow:
            assert uow.session is not None
            yield uow.session

    effect = DurableConsumerInbox("evclaims:effect")  # the handler's own write
    failing = True

    async def handler(session: AsyncSession, envelope: Any) -> None:
        assert await effect.claim(session, envelope["id"]) is True
        if failing:
            raise _Boom

    callback = build_consumer_callback(
        name="evclaims:rollback",
        database="default",
        handler=handler,
        durable=True,
        opener=opener,
        metric_processed="event_consumer_events_processed_total",
        metric_failed="event_consumer_events_failed_total",
        labels={"consumer": "evclaims:rollback"},
    )

    with pytest.raises(_Boom):
        await callback({"id": "e-rollback", "payload": {}})
    assert await _inbox_rows(sessionmaker, "e-rollback") == {}  # nothing committed

    failing = False
    await callback({"id": "e-rollback", "payload": {}})  # the redelivery
    assert await _inbox_rows(sessionmaker, "e-rollback") == {
        "evclaims:effect": 1,
        "evclaims:rollback": 1,
    }
    await callback({"id": "e-rollback", "payload": {}})  # a duplicate is skipped
    assert await _inbox_rows(sessionmaker, "e-rollback") == {
        "evclaims:effect": 1,
        "evclaims:rollback": 1,
    }


# ---------------------------------------------------------- renamed handler --


async def test_a_renamed_consumer_is_a_new_group_with_an_empty_inbox(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    old = consumer_group_name("old", "evclaims", None)
    new = consumer_group_name("new", "evclaims", None)
    assert (old, new) == ("evclaims:old", "evclaims:new")
    # The same module in two services resolves to the same group; a pin keeps it.
    assert consumer_group_name("old", "evclaims", None) == old
    assert consumer_group_name("new", "evclaims", old) == old
    # A new group reads the retained stream from its start by default.
    assert stream_start_id("earliest") == "0"

    async with sessionmaker() as session, session.begin():
        assert await DurableConsumerInbox(old).claim(session, "e-1") is True
    async with sessionmaker() as session, session.begin():
        # The old group saw the event; the renamed one applies it again.
        assert await DurableConsumerInbox(old).claim(session, "e-1") is False
        assert await DurableConsumerInbox(new).claim(session, "e-1") is True
