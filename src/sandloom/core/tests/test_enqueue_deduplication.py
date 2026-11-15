"""Enqueue dedupe (G-TASK-03) in the kernel's taskqueue layer above the
celery driver: the layer reserves the job id, the driver only sends."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from celery import Celery
from sandloom_celery.taskqueue import CeleryTaskQueueProvider

from sandloom.core.di import (
    TaskEnqueueConflict,
    TaskEnqueueUnavailable,
    TaskEnqueueUncertain,
)
from sandloom.core.infra.errors import InvalidArgument, Unsupported
from sandloom.core.tasks import enqueue
from sandloom.core.tasks.queue import KernelTaskQueue
from sandloom.core.testing import InMemoryCache
from sandloom.core.testing.workers import worker_env_values
from sandloom.core.tests.test_taskqueue_enqueue_layer import register_probe_task


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch):
    store = InMemoryCache()
    monkeypatch.setattr(enqueue, "atomic_store", lambda role: store)
    register_probe_task()
    # Not the process's current app: closing it deregisters it, and a closed
    # current app would swallow every later shared_task (NotRegistered).
    app = Celery("dedupe", broker="memory://", set_as_current=False)
    app.conf.task_default_queue = f"dedupe-{uuid4().hex}"
    provider = CeleryTaskQueueProvider(
        name="probe", driver="celery", options={}, app=app
    )
    yield _Layered(provider), app, store
    app.close()


class _Layered(KernelTaskQueue):
    """The kernel layer, with the provider's options reachable for a test."""

    __slots__ = ()

    @property
    def options(self) -> dict[str, object]:
        return self._provider.options  # type: ignore[return-value]


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

    with pytest.raises(InvalidArgument):
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


async def test_unserializable_payload_reserves_nothing(queue) -> None:
    """A payload that does not serialise is refused by the layer before the
    key is reserved, so a corrected retry with the same key is published."""
    provider, app, store = queue
    for _ in range(2):
        with pytest.raises(InvalidArgument):
            await provider.enqueue("probe.task", {"x": object()}, idempotency_key="key")
        assert store.store == {}
    task_id = await provider.enqueue("probe.task", {"x": [1, 2]}, idempotency_key="key")
    assert task_id
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 1


async def test_the_driver_refuses_a_payload_that_is_not_a_json_object(queue) -> None:
    layered, app, _ = queue
    provider = layered._provider
    for body in (b"not json", b"[1, 2]", b'"text"'):
        with pytest.raises(InvalidArgument):
            await provider.send("probe.task", body, task_id="t-1")
    with pytest.raises(InvalidArgument):
        await provider.send("", b"{}", task_id="t-1")
    with app.connection_for_read() as conn:
        assert conn.SimpleQueue(app.conf.task_default_queue).qsize() == 0


async def test_deterministic_send_error_releases_the_reservation(
    queue, monkeypatch
) -> None:
    from celery.exceptions import NotRegistered

    provider, app, _ = queue
    original = app.send_task

    def unknown(*args, **kwargs):
        raise NotRegistered("probe.task")

    monkeypatch.setattr(app, "send_task", unknown)
    with pytest.raises(InvalidArgument) as error:
        await provider.enqueue("probe.task", {"x": 1}, idempotency_key="key")
    assert isinstance(error.value.__cause__, NotRegistered)
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
    from sandloom_redis.cache import RedisCacheProvider

    from sandloom.core.di import bind_tenant

    env = worker_env_values(Path(__file__).resolve().parents[4] / ".env.test")
    store = RedisCacheProvider(
        name="enqueue", driver="redis", options={"url": env["CACHE_URL"]}, shared=False
    )
    monkeypatch.setattr(enqueue, "atomic_store", lambda role: store)
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


async def test_a_driver_without_features_still_serves_idempotency_keys(
    queue, monkeypatch
) -> None:
    """``idempotency_key`` is the kernel layer's, on every driver."""
    layered, app, _ = queue
    monkeypatch.setattr(layered._provider, "features", lambda: frozenset())
    first = await layered.enqueue("probe.task", {}, idempotency_key="key")
    assert await layered.enqueue("probe.task", {}, idempotency_key="key") == first
    assert _published(app) == 1


async def test_without_an_idempotency_store_nothing_is_published(
    queue, monkeypatch
) -> None:
    """A key needs the ``idempotency`` ``atomic_store``; without one the
    enqueue fails closed."""
    layered, app, _ = queue

    def missing(role):
        raise RuntimeError("no atomic_store")

    monkeypatch.setattr(enqueue, "atomic_store", missing)
    with pytest.raises(TaskEnqueueUnavailable):
        await layered.enqueue("probe.task", {}, idempotency_key="key")
    assert _published(app) == 0


async def test_an_ambiguous_driver_error_keeps_the_key_pending(
    queue, monkeypatch
) -> None:
    """Unavailable / Timeout (8.2) may have published: the key stays pending."""
    from sandloom.core.infra.errors import Timeout, Unavailable

    layered, _, _ = queue
    for error in (Unavailable, Timeout):

        async def maybe_sent(*_args, _error=error, **_kwargs):
            raise _error("lost", capability="taskqueue")

        monkeypatch.setattr(layered._provider, "send", maybe_sent)
        key = f"key-{error.__name__}"
        for _ in range(2):
            with pytest.raises(TaskEnqueueUncertain):
                await layered.enqueue("probe.task", {}, idempotency_key=key)


async def test_too_large_releases_the_reservation(queue, monkeypatch) -> None:
    from sandloom.core.infra.errors import TooLarge

    layered, _, _ = queue
    original = layered._provider.send

    async def too_large(*_args, **_kwargs):
        raise TooLarge("payload over the broker's limit", capability="taskqueue")

    monkeypatch.setattr(layered._provider, "send", too_large)
    with pytest.raises(TooLarge):
        await layered.enqueue("probe.task", {}, idempotency_key="key")
    monkeypatch.setattr(layered._provider, "send", original)
    assert await layered.enqueue("probe.task", {}, idempotency_key="key")


async def test_a_payload_without_canonical_form_reserves_nothing(queue) -> None:
    layered, _, store = queue
    with pytest.raises(InvalidArgument, match="refused"):
        await layered.enqueue("probe.task", {"bad": object()}, idempotency_key="key")
    assert store.store == {}


# -- checks before reserving (spec 8.1): a refusal leaves no key behind ------


def _published(app) -> int:
    with app.connection_for_read() as conn:
        return conn.SimpleQueue(app.conf.task_default_queue).qsize()


@pytest.mark.parametrize("task", ["", None, 7, b"probe.task"])
async def test_a_bad_task_name_reserves_nothing(queue, task) -> None:
    layered, app, store = queue
    with pytest.raises(InvalidArgument, match="task name"):
        await layered.enqueue(task, {}, idempotency_key="key")
    assert store.store == {}
    assert _published(app) == 0


@pytest.mark.parametrize(
    "delay",
    [
        "5",
        5,
        5.0,
        True,
        timedelta(seconds=-1),
        enqueue.MAX_DELAY + timedelta(seconds=1),
        timedelta(days=10**6),
    ],
)
async def test_a_bad_delay_reserves_nothing(queue, delay) -> None:
    layered, app, store = queue
    with pytest.raises(InvalidArgument, match="delay"):
        await layered.enqueue("probe.task", {}, delay=delay, idempotency_key="key")
    assert store.store == {}
    assert _published(app) == 0


async def test_an_unknown_task_reserves_nothing(queue) -> None:
    layered, app, store = queue
    with pytest.raises(InvalidArgument, match="not declared"):
        await layered.enqueue("probe.nobody", {}, idempotency_key="key")
    assert store.store == {}
    assert _published(app) == 0


async def test_the_delay_cap_and_zero_are_accepted(queue) -> None:
    layered, app, _ = queue
    assert enqueue.MAX_DELAY == timedelta(days=30)
    await layered.enqueue("probe.task", {}, delay=timedelta(0), idempotency_key="a")
    await layered.enqueue(
        "probe.task", {}, delay=enqueue.MAX_DELAY, idempotency_key="b"
    )
    assert _published(app) == 2


async def test_a_delay_without_the_feature_reserves_nothing(queue, monkeypatch) -> None:
    layered, app, store = queue
    monkeypatch.setattr(layered._provider, "features", lambda: frozenset())
    delay = timedelta(seconds=5)
    with pytest.raises(Unsupported, match="delay"):
        await layered.enqueue("probe.task", {}, delay=delay, idempotency_key="k")
    assert store.store == {}
    # Without a key the same refusal holds: nothing is published.
    with pytest.raises(Unsupported, match="delay"):
        await layered.enqueue("probe.task", {}, delay=delay)
    assert _published(app) == 0


async def test_a_bad_delay_without_a_key_publishes_nothing(queue) -> None:
    layered, app, _ = queue
    with pytest.raises(InvalidArgument, match="delay"):
        await layered.enqueue("probe.task", {}, delay=timedelta(days=10**6))
    assert _published(app) == 0
