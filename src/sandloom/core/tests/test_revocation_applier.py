"""The kernel's durable token-revocation applier (``core.auth.tokens_revoked.v1``).

A revocation whose mark could not be written after commit (cache outage) is
still in the outbox event every ``revoke_user_tokens_on_commit`` writes in its
transaction; the kernel's consumer applies it in every service (ADR-0012
decision 5), and keeps failing (so the event is redelivered) until the mark
is written.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sandloom.core.app_factory.revocation_applier import apply_token_revocation
from sandloom.sdk.security import ensure_token_not_revoked, revoke_user_tokens
from sandloom.sdk.testing import InMemoryCache, revocation_mark_key, use_security_cache


def _reads_as_us(stored: str) -> int:
    """The instant (µs) a stored ``after`` value reads as. A revocation writes
    one microsecond less plus its identity digits past the microsecond, and
    every reader rounds up (``sandloom.core.auth.marks``)."""
    whole, _, frac = stored.removeprefix("v1:after:").partition(".")
    us = int(whole) * 1_000_000 + int(frac[:6].ljust(6, "0"))
    return us + (1 if frac[6:].strip("0") else 0)


@pytest.fixture
def mem_cache(monkeypatch: pytest.MonkeyPatch) -> InMemoryCache:
    fake = InMemoryCache()
    use_security_cache(monkeypatch, fake)
    return fake


def _envelope(user_id: str, valid_after: float) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "payload": {"user_id": user_id, "valid_after": valid_after, "reason": "t"},
    }


async def test_applier_writes_the_pending_mark_once_the_cache_is_back(
    mem_cache: InMemoryCache,
) -> None:
    sub = str(uuid4())
    mark = time.time() + 2.0
    envelope = _envelope(sub, mark)
    mem_cache.broken = True
    with pytest.raises(RuntimeError) as unavailable:
        await apply_token_revocation(None, envelope)  # -> redelivered
    assert type(unavailable.value).__name__ == "RevocationUnavailableError"

    mem_cache.broken = False
    await apply_token_revocation(None, envelope)
    await apply_token_revocation(None, envelope)  # duplicate delivery: no-op
    stored = mem_cache.store[revocation_mark_key(sub)]
    assert _reads_as_us(stored) == round(mark * 1_000_000)
    issued_before = {"sub": sub, "iat": int(mark) - 5}
    with pytest.raises(HTTPException) as revoked:
        await ensure_token_not_revoked(None, issued_before)
    assert revoked.value.status_code == 401


async def test_applier_never_moves_a_newer_mark_backwards(
    mem_cache: InMemoryCache,
) -> None:
    sub = str(uuid4())
    newer = time.time() + 60
    await revoke_user_tokens(sub, valid_after=newer)
    await apply_token_revocation(None, _envelope(sub, newer - 30))  # late, stale
    stored = mem_cache.store[revocation_mark_key(sub)]
    assert _reads_as_us(stored) == round(newer * 1_000_000)


# --------------------------------------------------------------------------- #
# The kernel registers it, per deployment (ADR-0012 decision 5)
# --------------------------------------------------------------------------- #
#: A configuration the boot checks accept apart from what each test sets.
_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
    "CACHE_URL": "redis://localhost:6379/0",
    "BROKER_URL": "redis://localhost:6379/1",
    "JWT_SECRET": "kb09-test-secret-0123456789abcdef0123456789abcdef",
}


@pytest.fixture
def restore_process_runtime() -> Any:
    """Restore the process default core, registry and bus after the test."""
    from sandloom.core.eventing.event_bus import get_event_bus, set_active_event_bus
    from sandloom.core.infra.active import get_active_infra, set_active_infra
    from sandloom.core.runtime import get_active_core, set_active_core

    core, infra, bus = get_active_core(), get_active_infra(), get_event_bus()
    try:
        yield
    finally:
        if core is not None:
            set_active_core(core)
        set_active_infra(infra)
        set_active_event_bus(bus)


def _cfg(**env: Any) -> Any:
    """Settings from ``env`` alone (no ``.env`` file)."""
    from sandloom.core.config.settings import Settings

    return Settings(_env_file=None, **{**_BASE_ENV, **env})  # type: ignore[call-arg]


def test_the_group_is_named_by_the_service_not_the_database() -> None:
    """Two services sharing one database get two groups; replicas share one."""
    from sandloom.core.app_factory import revocation_applier as applier

    api = _cfg(EVENT_BUS_BACKEND="streams", SANDLOOM_SERVICE_NAME="api")
    billing = _cfg(EVENT_BUS_BACKEND="streams", SANDLOOM_SERVICE_NAME="billing")
    assert api.database_url == billing.database_url  # one database
    assert applier.applier_group(api) == "sandloom:auth@tokens_revoked@api"
    assert applier.applier_group(billing) == "sandloom:auth@tokens_revoked@billing"
    replica = _cfg(EVENT_BUS_BACKEND="streams", SANDLOOM_SERVICE_NAME=" api ")
    assert applier.applier_group(replica) == applier.applier_group(api)
    descriptor = applier.applier_descriptor(api)
    assert descriptor.channel == "core.auth.tokens_revoked.v1"
    assert descriptor.group_name == "sandloom:auth@tokens_revoked@api"
    assert descriptor.dead_letter is False  # retried forever, never parked
    assert descriptor.durable and descriptor.required


@pytest.mark.parametrize(
    "env",
    [
        {"EVENT_BUS_BACKEND": "memory"},
        {"EVENT_BUS_BACKEND": "auto", "ENVIRONMENT": "dev"},
    ],
)
def test_the_in_process_bus_needs_no_name(env: dict[str, str]) -> None:
    """No other process receives the in-process bus's events: no group to
    merge, so the applier runs under ``…@local`` and nothing is refused."""
    from sandloom.core.app_factory import revocation_applier as applier

    cfg = _cfg(**env)
    assert not applier.cross_process_bus(cfg)
    assert applier.revocation_applier_config_problem(cfg) is None
    assert applier.applier_group(cfg) == "sandloom:auth@tokens_revoked@local"


@pytest.mark.parametrize("backend", ["streams", "redis"])
def test_a_shared_bus_without_a_name_is_refused(backend: str) -> None:
    """Outside production too: a silent group merge is the failure."""
    from sandloom.core.app_factory import configuration_problems
    from sandloom.core.app_factory import revocation_applier as applier
    from sandloom.core.config.common import ConfigurationError

    cfg = _cfg(EVENT_BUS_BACKEND=backend)
    problem = applier.revocation_applier_config_problem(cfg)
    assert problem is not None and "SANDLOOM_SERVICE_NAME" in problem
    assert "auth@tokens_revoked" in problem  # names the consumer
    assert problem in configuration_problems(cfg)
    with pytest.raises(ConfigurationError, match="SANDLOOM_SERVICE_NAME"):
        applier.applier_group(cfg)
    bad = _cfg(EVENT_BUS_BACKEND=backend, SANDLOOM_SERVICE_NAME="a b")
    assert "whitespace" in (applier.revocation_applier_config_problem(bad) or "")
    named = _cfg(EVENT_BUS_BACKEND=backend, SANDLOOM_SERVICE_NAME="api")
    assert applier.revocation_applier_config_problem(named) is None


@pytest.mark.parametrize("driver", ["streams", "redis", "kafka"])
def test_a_shared_bus_chosen_through_infra_is_refused_without_a_name(
    driver: str,
) -> None:
    """The bus is the ``default`` eventbus instance's resolved driver, not
    ``EVENT_BUS_BACKEND``: ``INFRA`` selecting Streams (or Pub/Sub, or a
    plugin's driver) while ``EVENT_BUS_BACKEND=auto`` outside production is
    still a shared bus. Unnamed it is refused; separate services would
    otherwise share ``…@local`` and split revocations."""

    from sandloom.core.app_factory import configuration_problems
    from sandloom.core.app_factory import revocation_applier as applier
    from sandloom.core.config.common import ConfigurationError
    from sandloom.core.infra.config import eventbus_driver

    infra = {"eventbus": {"default": {"driver": driver}}}
    cfg = _cfg(EVENT_BUS_BACKEND="auto", ENVIRONMENT="dev", INFRA=infra)
    assert eventbus_driver(cfg) == driver
    assert applier.cross_process_bus(cfg)
    problem = applier.revocation_applier_config_problem(cfg)
    assert problem is not None and "SANDLOOM_SERVICE_NAME" in problem
    assert problem in configuration_problems(cfg)
    with pytest.raises(ConfigurationError, match="SANDLOOM_SERVICE_NAME"):
        applier.applier_group(cfg)
    named = _cfg(
        EVENT_BUS_BACKEND="auto",
        ENVIRONMENT="dev",
        INFRA=infra,
        SANDLOOM_SERVICE_NAME="api",
    )
    assert applier.revocation_applier_config_problem(named) is None
    assert applier.applier_group(named) == "sandloom:auth@tokens_revoked@api"


def test_infra_selecting_the_memory_bus_is_in_process() -> None:
    """The reverse: ``INFRA`` binding the in-process bus as ``default`` wins
    over ``EVENT_BUS_BACKEND=streams`` (that bus has no other receiver)."""

    from sandloom.core.app_factory import revocation_applier as applier

    infra = {"eventbus": {"default": {"driver": "memory"}}}
    cfg = _cfg(EVENT_BUS_BACKEND="streams", INFRA=infra)
    assert not applier.cross_process_bus(cfg)
    assert applier.applier_group(cfg) == "sandloom:auth@tokens_revoked@local"


def test_an_unreadable_infra_eventbus_counts_as_shared() -> None:
    """Fail closed: an ``INFRA`` eventbus the merge refuses (an instance
    without a driver) cannot prove the bus is in-process."""

    from sandloom.core.app_factory import revocation_applier as applier

    infra = {"eventbus": {"extra": {"url": "redis://x:6379/0"}}}
    cfg = _cfg(EVENT_BUS_BACKEND="memory", INFRA=infra)
    assert applier.cross_process_bus(cfg)
    assert applier.revocation_applier_config_problem(cfg) is not None


def test_every_entry_point_refuses_a_shared_bus_without_a_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``create_app`` (the API and the worker, which imports the app), the
    ``sandloom`` CLI's configured commands and ``sandloom migrate`` all run
    ``enforce_configuration``: each refuses with INVALID_CONFIGURATION."""
    import sandloom.core.app_factory.config as app_config
    import sandloom.core.cli.migrate as migrate
    from sandloom.core.app_factory import create_app, enforce_configuration
    from sandloom.core.config.common import INVALID_CONFIGURATION, ConfigurationError
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "event_bus_backend", "streams")
    monkeypatch.setattr(settings, "sandloom_service_name", None)
    with pytest.raises(ConfigurationError) as app_refusal:
        create_app()
    assert str(app_refusal.value).startswith(INVALID_CONFIGURATION)
    assert "SANDLOOM_SERVICE_NAME" in str(app_refusal.value)
    with pytest.raises(ConfigurationError, match="SANDLOOM_SERVICE_NAME"):
        enforce_configuration(settings)  # the `sandloom` CLI's boot check
    monkeypatch.setattr(app_config, "load_settings", lambda: settings)
    with pytest.raises(ConfigurationError, match="SANDLOOM_SERVICE_NAME"):
        enforce_configuration()
    with pytest.raises(ConfigurationError, match="SANDLOOM_SERVICE_NAME"):
        migrate.main(["databases"])


def test_create_app_refuses_an_infra_selected_shared_bus_without_a_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The INFRA scenario through the real entry point."""
    from sandloom.core.app_factory import create_app
    from sandloom.core.config.common import INVALID_CONFIGURATION, ConfigurationError
    from sandloom.core.config.settings import settings

    assert not settings.is_prod
    monkeypatch.setattr(settings, "event_bus_backend", "auto")
    monkeypatch.setattr(
        settings, "infra", {"eventbus": {"default": {"driver": "streams"}}}
    )
    monkeypatch.setattr(settings, "sandloom_service_name", None)
    with pytest.raises(ConfigurationError) as refusal:
        create_app()
    assert str(refusal.value).startswith(INVALID_CONFIGURATION)
    assert "SANDLOOM_SERVICE_NAME" in str(refusal.value)


@pytest.mark.usefixtures("restore_process_runtime")
async def test_a_lean_app_registers_the_local_applier_and_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SANDLOOM_MODULES=[]`` on the in-process bus: no name needed, the
    applier runs (a failed after-commit mark is still re-applied) and
    ``/ready`` answers 200."""
    from httpx import ASGITransport, AsyncClient

    from sandloom.core.app_factory import create_app
    from sandloom.core.config.settings import settings

    monkeypatch.setattr(settings, "sandloom_modules", [])
    monkeypatch.setattr(settings, "module_required", [])
    monkeypatch.setattr(settings, "event_bus_backend", "memory")
    monkeypatch.setattr(settings, "sandloom_service_name", None)
    application = create_app()
    consumers = application.state.core.event_consumers
    assert [(c.name, c.group_name) for c in consumers] == [
        ("auth@tokens_revoked", "sandloom:auth@tokens_revoked@local")
    ]
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.get("/ready")
    assert response.status_code == 200, response.json()
    assert response.json()["revocation_consumers_ready"] is True


# --------------------------------------------------------------------------- #
# Proof on a Streams bus (one transport = one Redis; one bus = one process)
# --------------------------------------------------------------------------- #
CHANNEL = "core.auth.tokens_revoked.v1"


def _counting_callback(applied: list[str], deployment: str) -> Any:
    """The kernel's applier behind the runtime's callback, recording which
    deployment applied which event (no database: the claim is per group)."""
    from contextlib import asynccontextmanager

    from sandloom.core.consumers.callbacks import build_consumer_callback

    @asynccontextmanager
    async def _no_session(_database: str) -> Any:
        yield None

    async def _handler(session: Any, envelope: Any) -> None:
        await apply_token_revocation(session, envelope)
        applied.append(f"{deployment}:{envelope['id']}")

    return build_consumer_callback(
        name=deployment,
        database="default",
        handler=_handler,
        durable=False,
        opener=_no_session,
        metric_processed="event_consumer_events_processed_total",
        metric_failed="event_consumer_events_failed_total",
        labels={"consumer": deployment},
        dead_letter=False,
    )


def _bus(transport: Any) -> Any:
    from sandloom.core.eventing.streams_bus import StreamsEventBus
    from sandloom.core.schema.registry import InMemorySchemaRegistry

    return StreamsEventBus(InMemorySchemaRegistry(), transport, poll_interval=0.005)


async def _until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def _group(name: str) -> str:
    from sandloom.core.app_factory import revocation_applier as applier

    return applier.applier_group(
        _cfg(EVENT_BUS_BACKEND="streams", SANDLOOM_SERVICE_NAME=name)
    )


async def test_three_replicas_and_two_deployments_on_one_bus(
    mem_cache: InMemoryCache,
) -> None:
    """Three replicas of ``api`` share its group (each event applied once
    there); ``billing`` on the same bus has its own group and also applies
    every event. Published once, every revocation reaches both deployments."""
    from sandloom.core.eventing.memory_transport import MemoryTransport

    # A delivery in flight is reclaimed by another replica only once idle
    # (Redis: XAUTOCLAIM min-idle), never 1 ms later.
    transport = MemoryTransport(retry_base_ms=60_000, retry_max_ms=60_000)
    applied: list[str] = []
    buses = [_bus(transport) for _ in range(4)]
    for bus in buses[:3]:
        await bus.subscribe(
            CHANNEL, _counting_callback(applied, "api"), group=_group("api")
        )
    await buses[3].subscribe(
        CHANNEL, _counting_callback(applied, "billing"), group=_group("billing")
    )
    events = [_envelope(str(uuid4()), time.time() + 1) for _ in range(9)]
    try:
        for envelope in events:
            await transport.publish(CHANNEL, {**envelope, "channel": CHANNEL})
        await _until(lambda: len(applied) == 2 * len(events))
        await asyncio.sleep(0.1)  # nothing more arrives
    finally:
        for bus in buses:
            await bus.close()
    for deployment in ("api", "billing"):
        assert sorted(a for a in applied if a.startswith(deployment + ":")) == sorted(
            f"{deployment}:{e['id']}" for e in events
        )
    for envelope in events:
        sub = envelope["payload"]["user_id"]
        assert revocation_mark_key(sub) in mem_cache.store


async def test_cache_failure_redelivers_then_applies_and_restart_replays_idempotently(
    mem_cache: InMemoryCache,
) -> None:
    """The cache fails while the applier runs: the delivery is retried (never
    dead-lettered) and applied once the cache is back. A restarted process
    reading the stream again from the start re-applies nothing new: the
    stored mark stays where it is (marks are monotonic)."""
    from sandloom.core.eventing.memory_transport import MemoryTransport

    transport = MemoryTransport(max_deliveries=2, retry_base_ms=1, retry_max_ms=10)
    applied: list[str] = []
    group = _group("api")
    sub = str(uuid4())
    mark = time.time() + 2
    envelope = {**_envelope(sub, mark), "channel": CHANNEL}
    mem_cache.broken = True
    bus = _bus(transport)
    await bus.subscribe(CHANNEL, _counting_callback(applied, "api"), group=group)
    try:
        await transport.publish(CHANNEL, envelope)
        await _until(lambda: transport.pending_count(CHANNEL, group) == 1)
        await _until(
            lambda: transport._delivery_count.get((CHANNEL, group, "1-0"), 0) >= 3
        )
        assert applied == []  # past max_deliveries, still not parked
        mem_cache.broken = False
        await _until(lambda: applied == [f"api:{envelope['id']}"])
        await _until(lambda: transport.pending_count(CHANNEL, group) == 0)
    finally:
        await bus.close()
    assert transport.dlq_depth(CHANNEL) == 0
    stored = mem_cache.store[revocation_mark_key(sub)]

    # restart + replay: a new process, a group created again from the start
    replay = _bus(transport)
    replayed: list[str] = []
    await replay.subscribe(
        CHANNEL,
        _counting_callback(replayed, "api"),
        group=group + "-replay",
        start_from="earliest",
    )
    try:
        await _until(lambda: replayed == [f"api:{envelope['id']}"])
    finally:
        await replay.close()
    assert mem_cache.store[revocation_mark_key(sub)] == stored


async def test_a_channel_version_change_keeps_the_deployment_group(
    mem_cache: InMemoryCache,
) -> None:
    """The group names the deployment, not the channel version: during a roll
    to a ``.v2`` channel (dual-published) the same deployment group drains
    both streams, each event applied there."""
    from sandloom.core.eventing.memory_transport import MemoryTransport

    v2 = "core.auth.tokens_revoked.v2"
    group = _group("api")
    assert "v1" not in group
    transport = MemoryTransport(retry_base_ms=1)
    applied: list[str] = []
    bus = _bus(transport)
    await bus.subscribe(CHANNEL, _counting_callback(applied, "v1"), group=group)
    await bus.subscribe(v2, _counting_callback(applied, "v2"), group=group)
    sub = str(uuid4())
    old = {**_envelope(sub, time.time() + 1), "channel": CHANNEL}
    new = {**_envelope(sub, time.time() + 3), "channel": v2}
    try:
        await transport.publish(CHANNEL, old)
        await transport.publish(v2, new)
        await _until(lambda: len(applied) == 2)
    finally:
        await bus.close()
    assert sorted(applied) == sorted([f"v1:{old['id']}", f"v2:{new['id']}"])
    stored = mem_cache.store[revocation_mark_key(sub)]
    assert _reads_as_us(stored) == round(new["payload"]["valid_after"] * 1_000_000)
