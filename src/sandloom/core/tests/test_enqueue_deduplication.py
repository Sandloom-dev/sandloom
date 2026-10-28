from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from celery import Celery

from sandloom.core.di import (
    TaskEnqueueConflict,
    TaskEnqueueUnavailable,
    TaskEnqueueUncertain,
)
from sandloom.core.drivers.celery_taskqueue import CeleryTaskQueueProvider
from sandloom.core.tasks import enqueue
from sandloom.core.testing import InMemoryCache
from sandloom.core.testing.workers import worker_env_values


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch):
    store = InMemoryCache()
    monkeypatch.setattr(enqueue, "idempotency_cache", lambda: store)
    app = Celery("dedupe", broker="memory://")
    app.conf.task_default_queue = f"dedupe-{uuid4().hex}"
    provider = CeleryTaskQueueProvider(
        name="probe", driver="celery", options={}, app=app
    )
    yield provider, app, store
    app.close()


async def test_successful_retries_publish_one_real_message(queue) -> None:
    provider, app, _ = queue
    first = await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")
    assert (
        await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key") == first
    )
    with app.connection_for_read() as conn:
        messages = conn.SimpleQueue(app.conf.task_default_queue)
        assert messages.qsize() == 1
        message = messages.get(block=False)
        assert message.headers["id"] == first
        message.ack()


async def test_invalid_serializer_payload_never_leaves_pending_reservation(
    queue,
) -> None:
    provider, app, _store = queue
    from kombu.exceptions import EncodeError

    with pytest.raises(EncodeError):
        await provider.enqueue(
            "probe.task", {"bad": object()}, idempotency_key="invalid-key"
        )
    # The same operation key can be corrected because no send/reservation ran.
    result = await provider.enqueue(
        "probe.task", {"ok": 1}, idempotency_key="invalid-key"
    )
    assert result
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 1


async def test_parallel_retries_never_publish_twice(queue) -> None:
    provider, app, _ = queue
    outcomes = await asyncio.gather(
        *(
            provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")
            for _ in range(8)
        ),
        return_exceptions=True,
    )
    ids = [result for result in outcomes if isinstance(result, str)]
    assert ids and len(set(ids)) == 1
    assert all(isinstance(result, (str, TaskEnqueueUncertain)) for result in outcomes)
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 1


async def test_key_cannot_be_reused_for_other_arguments(queue) -> None:
    provider, _, _ = queue
    await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")
    with pytest.raises(TaskEnqueueConflict):
        await provider.enqueue("probe.task", {"x": 2}, idempotency_key="key")


async def test_broker_credential_rotation_preserves_deduplication(queue) -> None:
    provider, app, _ = queue
    provider.options["broker"] = "redis://:old-password@broker:6379/1"
    first = await provider.enqueue("probe.task", {}, idempotency_key="key")
    provider.options["broker"] = "redis://:new-password@broker:6379/1"
    assert await provider.enqueue("probe.task", {}, idempotency_key="key") == first
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 1


async def test_uncertain_broker_ack_is_not_republished(queue, monkeypatch) -> None:
    provider, app, _ = queue
    original = app.send_task

    def lose_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise ConnectionError("ack lost after broker accepted the task")

    monkeypatch.setattr(app, "send_task", lose_ack)
    for _ in range(2):
        with pytest.raises(TaskEnqueueUncertain):
            await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 1


async def test_unserializable_payload_releases_the_reservation(queue) -> None:
    """A send that provably never reached the broker (the payload cannot be
    encoded) must not leave the key pending forever: the reservation is
    released, so a corrected retry with the same key is published."""
    from kombu.exceptions import EncodeError

    provider, app, _store = queue
    with pytest.raises(EncodeError):
        await provider.enqueue("probe.task", {"x": {1, 2}}, idempotency_key="key")
    # Before: the key stayed "pending" -> every retry 503 task_enqueue_uncertain.
    with pytest.raises(EncodeError):
        await provider.enqueue("probe.task", {"x": {1, 2}}, idempotency_key="key")
    task_id = await provider.enqueue("probe.task", {"x": [1, 2]}, idempotency_key="key")
    assert task_id
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 1


async def test_deterministic_send_error_releases_the_reservation(
    queue, monkeypatch
) -> None:
    from celery.exceptions import NotRegistered

    provider, app, _ = queue
    original = app.send_task

    def unknown(*args, **kwargs):
        raise NotRegistered("probe.task")

    monkeypatch.setattr(app, "send_task", unknown)
    with pytest.raises(NotRegistered):
        await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")
    monkeypatch.setattr(app, "send_task", original)
    assert await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")


async def test_ambiguous_send_error_still_keeps_the_key_pending(
    queue, monkeypatch
) -> None:
    provider, app, _ = queue

    def ambiguous(*args, **kwargs):
        raise ValueError("I/O operation on closed file")  # mid-send: unknown

    monkeypatch.setattr(app, "send_task", ambiguous)
    for _ in range(2):
        with pytest.raises(TaskEnqueueUncertain):
            await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")


async def test_unavailable_store_prevents_publish(queue, monkeypatch) -> None:
    provider, app, store = queue

    async def unavailable(*args, **kwargs):
        raise ConnectionError("cache unavailable")

    monkeypatch.setattr(store, "add", unavailable)
    with pytest.raises(TaskEnqueueUnavailable):
        await provider.enqueue("probe.task", {}, idempotency_key="key")
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 0


async def test_lost_finalize_ack_keeps_the_original_id_on_retry(
    queue, monkeypatch
) -> None:
    provider, app, store = queue
    original = store.compare_and_set

    async def lose_ack(*args, **kwargs):
        await original(*args, **kwargs)
        raise ConnectionError("cache ack lost after saving success")

    monkeypatch.setattr(store, "compare_and_set", lose_ack)
    with pytest.raises(TaskEnqueueUncertain) as error:
        await provider.enqueue("probe.task", {}, idempotency_key="key")
    replayed = await provider.enqueue("probe.task", {}, idempotency_key="key")
    assert replayed == error.value.detail["task_id"]
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 1


async def test_tenants_do_not_share_enqueue_keys(queue) -> None:
    from sandloom.core.di import bind_tenant

    provider, app, _ = queue
    ids = []
    for _ in range(2):
        with bind_tenant(uuid4()):
            ids.append(await provider.enqueue("probe.task", {}, idempotency_key="key"))
    assert ids[0] != ids[1]
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 2


async def test_payload_is_snapshotted_before_reserving(queue, monkeypatch) -> None:
    provider, app, store = queue
    payload = {"nested": {"x": 1}}
    original = store.add

    async def mutate_during_reservation(*args, **kwargs):
        payload["nested"]["x"] = 2
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "add", mutate_during_reservation)
    first = await provider.enqueue("probe.task", payload, idempotency_key="key")
    assert (
        await provider.enqueue(
            "probe.task", {"nested": {"x": 1}}, idempotency_key="key"
        )
        == first
    )
    with app.connection_for_read() as conn:
        message = conn.SimpleQueue(app.conf.task_default_queue).get(block=False)
        assert message.payload[1] == {"nested": {"x": 1}}
        message.ack()


async def test_replay_window_expires_on_real_redis(queue, monkeypatch) -> None:
    from sandloom.core.di import bind_tenant
    from sandloom.core.drivers.redis_cache import RedisCacheProvider

    env = worker_env_values(Path(__file__).resolve().parents[4] / ".env.test")
    store = RedisCacheProvider(
        name="enqueue", driver="redis", options={"url": env["CACHE_URL"]}, shared=False
    )
    monkeypatch.setattr(enqueue, "idempotency_cache", lambda: store)
    provider, app, _ = queue
    provider.options["dedupe_ttl_seconds"] = 1
    try:
        with bind_tenant(uuid4()):
            first = await provider.enqueue("probe.task", {}, idempotency_key="key")
            assert (
                await provider.enqueue("probe.task", {}, idempotency_key="key") == first
            )
            await asyncio.sleep(1.1)
            assert (
                await provider.enqueue("probe.task", {}, idempotency_key="key") != first
            )
        with app.connection_for_read() as conn:
            assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 2
    finally:
        await store.aclose()
