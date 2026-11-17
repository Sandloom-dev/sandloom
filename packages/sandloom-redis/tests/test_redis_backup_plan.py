"""``sandloom backup plan`` on the standard stack: what the Redis drivers say.

The kernel's plan names no driver; each driver's ``backup_hint`` supplies its
part (``sandloom_redis.backup``). This runs the kernel's plan over the real
drivers and pins what an operator reads, which is what the plan said before the
hints moved out of the kernel (the same steps, in the same order).
"""

from __future__ import annotations

from typing import Any

import pytest

import sandloom.core.infra.config as infra_config
from sandloom.core.config.settings import settings
from sandloom.core.migrations.backup import PlanItem, build_backup_plan, format_plan

AOF = (
    "appendonly yes + appendfsync everysec (AOF) AND periodic RDB "
    "snapshots (save / BGSAVE); copy dump.rdb + the appendonly dir off-host"
)
NOEVICTION = (
    "maxmemory-policy noeviction (an evicted block mark = an unblocked "
    "user); a SEPARATE instance from the best-effort cache in prod"
)
RECONCILE = (
    "a snapshot older than a later block / revocation LIFTS it silently: "
    "run sandloom backup reconcile-security --apply before admitting traffic"
)
REVOKE = (
    "if the snapshot age is unknown and the outbox no longer covers the "
    "access-token lifetime: sandloom security revoke-sessions --all (or rotate "
    "the JWT signing key)"
)
STREAMS_LOSS = (
    "events delivered to the stream after the snapshot are lost from it "
    "(the outbox already marked them delivered): rebuild projections "
    "(sandloom projection rebuild) and replay affected consumers by hand"
)
OFFSETS = "consumer-group offsets come back older: consumers re-read and dedupe"
CELERY_LOSS = (
    "queued Celery tasks newer than the snapshot are lost and older ones "
    "may run again: task effects need DB-atomic idempotency; re-enqueue "
    "what is missing (docs/operate/operator-reference.md: "
    "uncertain task enqueues)"
)
CACHE_LOSS = (
    "losing it is tolerable: rate-limit windows reset, cache-backed "
    "idempotency replays are forgotten (DB-atomic dedupe is unaffected)"
)


def _plan(monkeypatch: pytest.MonkeyPatch, infra: dict[str, Any]) -> list[PlanItem]:
    monkeypatch.setattr(settings, "infra", {})
    monkeypatch.setattr(infra_config, "_APP", None)
    monkeypatch.setattr(infra_config, "_PIN", infra_config._checked(infra))
    return build_backup_plan(settings)


def _redis(items: list[PlanItem]) -> dict[str, PlanItem]:
    return {i.target: i for i in items if i.kind == "redis"}


def test_the_standard_stack_plan_says_what_it_said_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache and security store on one server, a Streams bus with no url of its
    own (on that server too), a broker on another."""
    items = _plan(
        monkeypatch,
        {
            "cache": {
                "default": {"driver": "redis", "url": "redis://cachehost:6379/3"}
            },
            "atomic_store": {
                "security": {"driver": "redis", "url": "redis://cachehost:6379/3"}
            },
            "eventbus": {"bus": {"driver": "streams"}},
            "taskqueue": {
                "default": {"driver": "celery", "broker": "redis://brokerhost:6379/4"}
            },
        },
    )
    servers = _redis(items)
    shared = servers["redis://cachehost:6379/3"]
    assert shared.roles == [
        "cache:default",
        "atomic_store:security [SECURITY: revocation marks, user blocks, "
        "RPC nonces, idempotency]",
        "eventbus:bus",
    ]
    assert shared.name == "cache:default, atomic_store:security, eventbus:bus"
    assert shared.criticality == "critical"
    assert shared.backup == [NOEVICTION, AOF]
    assert shared.restore == [RECONCILE, REVOKE, STREAMS_LOSS, OFFSETS]
    broker = servers["redis://brokerhost:6379/4"]
    assert broker.roles == ["taskqueue:default"]
    assert broker.criticality == "optional"
    assert broker.backup == [AOF]
    assert broker.restore == [CELERY_LOSS]


def test_a_cache_alone_says_its_loss_is_tolerable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = _plan(
        monkeypatch,
        {"cache": {"default": {"driver": "redis", "url": "redis://cachehost:6379/3"}}},
    )
    cache = _redis(items)["redis://cachehost:6379/3"]
    assert cache.criticality == "optional"
    assert cache.backup == [AOF]
    assert cache.restore == [CACHE_LOSS]


def test_a_streams_bus_alone_is_required_data_not_a_tolerable_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = _plan(
        monkeypatch,
        {
            "cache": {
                "default": {"driver": "redis", "url": "redis://cachehost:6379/3"}
            },
            "eventbus": {"bus": {"driver": "streams", "url": "redis://buscost:6379/0"}},
        },
    )
    bus = _redis(items)["redis://buscost:6379/0"]
    assert bus.criticality == "required"
    assert bus.restore == [STREAMS_LOSS, OFFSETS]
    cache = _redis(items)["redis://cachehost:6379/3"]
    assert cache.restore == [CACHE_LOSS]  # not shared with the bus: still tolerable


def test_the_volatile_broadcast_bus_and_a_non_redis_broker_add_no_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = _plan(
        monkeypatch,
        {
            "eventbus": {"bus": {"driver": "broadcast"}},
            "taskqueue": {
                "default": {"driver": "celery", "broker": "amqp://guest@mq:5672//"}
            },
        },
    )
    assert not _redis(items)
    assert "amqp" not in format_plan(items)


def test_a_url_with_a_password_is_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    items = _plan(
        monkeypatch,
        {"cache": {"default": {"driver": "redis", "url": "redis://:s3cret@c:6379/0"}}},
    )
    assert "s3cret" not in format_plan(items)
    assert _redis(items)


def test_an_instance_that_states_no_url_lists_no_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing fills a missing url: nothing is listed, never a guess."""
    items = _plan(
        monkeypatch,
        {
            "cache": {"default": {"driver": "redis"}},
            "taskqueue": {"default": {"driver": "celery"}},
        },
    )
    assert not _redis(items)
