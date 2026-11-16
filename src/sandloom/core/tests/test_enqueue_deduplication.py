"""Enqueue dedupe (G-TASK-03) in the kernel's taskqueue layer: the layer reserves
the job id, the driver only sends.

The driver here is the recording one of ``test_taskqueue_enqueue_layer``: what it
was asked to send is what was published. What needs a real driver (the messages
on a Celery broker, its send errors, a real Redis replay window) is
``packages/sandloom-celery/tests/test_celery_enqueue_deduplication.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from sandloom.core.di import (
    TaskEnqueueConflict,
    TaskEnqueueUnavailable,
    TaskEnqueueUncertain,
)
from sandloom.core.infra.errors import InvalidArgument, Unsupported
from sandloom.core.tasks import enqueue
from sandloom.core.tasks.queue import KernelTaskQueue
from sandloom.core.testing import InMemoryCache
from sandloom.core.tests.test_taskqueue_enqueue_layer import (
    RecordingProvider,
    register_probe_task,
)


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch):
    store = InMemoryCache()
    monkeypatch.setattr(enqueue, "atomic_store", lambda role: store)
    register_probe_task()
    driver = RecordingProvider()
    yield KernelTaskQueue(driver), driver, store


async def test_retries_with_the_key_send_one_message(queue) -> None:
    layered, driver, _ = queue
    first = await layered.enqueue("probe.task", {"x": 1}, idempotency_key="key")
    assert await layered.enqueue("probe.task", {"x": 1}, idempotency_key="key") == first
    assert [sent["task_id"] for sent in driver.sent] == [first]


async def test_parallel_retries_send_one_message(queue) -> None:
    layered, driver, _ = queue
    outcomes = await asyncio.gather(
        *(
            layered.enqueue("probe.task", {"x": 1}, idempotency_key="key")
            for _ in range(8)
        ),
        return_exceptions=True,
    )
    ids = [result for result in outcomes if isinstance(result, str)]
    assert ids and len(set(ids)) == 1
    assert all(isinstance(result, (str, TaskEnqueueUncertain)) for result in outcomes)
    assert len(driver.sent) == 1


async def test_invalid_serializer_payload_never_leaves_pending_reservation(
    queue,
) -> None:
    layered, driver, _store = queue

    with pytest.raises(InvalidArgument):
        await layered.enqueue(
            "probe.task", {"bad": object()}, idempotency_key="invalid-key"
        )
    # The same operation key can be corrected because no send/reservation ran.
    result = await layered.enqueue(
        "probe.task", {"ok": 1}, idempotency_key="invalid-key"
    )
    assert result
    assert len(driver.sent) == 1


async def test_key_cannot_be_reused_for_other_arguments(queue) -> None:
    layered, _, _ = queue
    await layered.enqueue("probe.task", {"x": 1}, idempotency_key="key")
    with pytest.raises(TaskEnqueueConflict):
        await layered.enqueue("probe.task", {"x": 2}, idempotency_key="key")


async def test_unserializable_payload_reserves_nothing(queue) -> None:
    """A payload that does not serialise is refused by the layer before the
    key is reserved, so a corrected retry with the same key is published."""
    layered, driver, store = queue
    for _ in range(2):
        with pytest.raises(InvalidArgument):
            await layered.enqueue("probe.task", {"x": object()}, idempotency_key="key")
        assert store.store == {}
    task_id = await layered.enqueue("probe.task", {"x": [1, 2]}, idempotency_key="key")
    assert task_id
    assert len(driver.sent) == 1


async def test_unavailable_store_prevents_publish(queue, monkeypatch) -> None:
    layered, driver, store = queue

    async def unavailable(*args, **kwargs):
        raise ConnectionError("cache unavailable")

    monkeypatch.setattr(store, "add", unavailable)
    with pytest.raises(TaskEnqueueUnavailable):
        await layered.enqueue("probe.task", {}, idempotency_key="key")
    assert driver.sent == []


async def test_lost_finalize_ack_keeps_the_original_id_on_retry(
    queue, monkeypatch
) -> None:
    layered, driver, store = queue
    original = store.compare_and_set

    async def lose_ack(*args, **kwargs):
        await original(*args, **kwargs)
        raise ConnectionError("cache ack lost after saving success")

    monkeypatch.setattr(store, "compare_and_set", lose_ack)
    with pytest.raises(TaskEnqueueUncertain) as error:
        await layered.enqueue("probe.task", {}, idempotency_key="key")
    replayed = await layered.enqueue("probe.task", {}, idempotency_key="key")
    assert replayed == error.value.detail["task_id"]
    assert len(driver.sent) == 1


async def test_tenants_do_not_share_enqueue_keys(queue) -> None:
    from sandloom.core.di import bind_tenant

    layered, driver, _ = queue
    ids = []
    for _ in range(2):
        with bind_tenant(uuid4()):
            ids.append(await layered.enqueue("probe.task", {}, idempotency_key="key"))
    assert ids[0] != ids[1]
    assert len(driver.sent) == 2


async def test_payload_is_snapshotted_before_reserving(queue, monkeypatch) -> None:
    layered, driver, store = queue
    payload = {"nested": {"x": 1}}
    original = store.add

    async def mutate_during_reservation(*args, **kwargs):
        payload["nested"]["x"] = 2
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "add", mutate_during_reservation)
    first = await layered.enqueue("probe.task", payload, idempotency_key="key")
    assert (
        await layered.enqueue("probe.task", {"nested": {"x": 1}}, idempotency_key="key")
        == first
    )
    (sent,) = driver.sent
    assert json.loads(sent["payload"]) == {"nested": {"x": 1}}


async def test_a_driver_without_features_still_serves_idempotency_keys(
    monkeypatch,
) -> None:
    """``idempotency_key`` is the kernel layer's, on every driver."""
    store = InMemoryCache()
    monkeypatch.setattr(enqueue, "atomic_store", lambda role: store)
    register_probe_task()
    driver = RecordingProvider(features=frozenset())
    layered = KernelTaskQueue(driver)
    first = await layered.enqueue("probe.task", {}, idempotency_key="key")
    assert await layered.enqueue("probe.task", {}, idempotency_key="key") == first
    assert len(driver.sent) == 1


async def test_without_an_idempotency_store_nothing_is_published(
    queue, monkeypatch
) -> None:
    """A key needs the ``idempotency`` ``atomic_store``; without one the
    enqueue fails closed."""
    layered, driver, _ = queue

    def missing(role):
        raise RuntimeError("no atomic_store")

    monkeypatch.setattr(enqueue, "atomic_store", missing)
    with pytest.raises(TaskEnqueueUnavailable):
        await layered.enqueue("probe.task", {}, idempotency_key="key")
    assert driver.sent == []


async def test_an_ambiguous_driver_error_keeps_the_key_pending(
    queue, monkeypatch
) -> None:
    """Unavailable / Timeout (8.2) may have published: the key stays pending."""
    from sandloom.core.infra.errors import Timeout, Unavailable

    layered, driver, _ = queue
    for error in (Unavailable, Timeout):

        async def maybe_sent(*_args, _error=error, **_kwargs):
            raise _error("lost", capability="taskqueue")

        monkeypatch.setattr(driver, "send", maybe_sent)
        key = f"key-{error.__name__}"
        for _ in range(2):
            with pytest.raises(TaskEnqueueUncertain):
                await layered.enqueue("probe.task", {}, idempotency_key=key)


async def test_too_large_releases_the_reservation(queue, monkeypatch) -> None:
    from sandloom.core.infra.errors import TooLarge

    layered, driver, _ = queue
    original = driver.send

    async def too_large(*_args, **_kwargs):
        raise TooLarge("payload over the broker's limit", capability="taskqueue")

    monkeypatch.setattr(driver, "send", too_large)
    with pytest.raises(TooLarge):
        await layered.enqueue("probe.task", {}, idempotency_key="key")
    monkeypatch.setattr(driver, "send", original)
    assert await layered.enqueue("probe.task", {}, idempotency_key="key")


async def test_a_payload_without_canonical_form_reserves_nothing(queue) -> None:
    layered, _, store = queue
    with pytest.raises(InvalidArgument, match="refused"):
        await layered.enqueue("probe.task", {"bad": object()}, idempotency_key="key")
    assert store.store == {}


# -- checks before reserving (spec 8.1): a refusal leaves no key behind ------


@pytest.mark.parametrize("task", ["", None, 7, b"probe.task"])
async def test_a_bad_task_name_reserves_nothing(queue, task) -> None:
    layered, driver, store = queue
    with pytest.raises(InvalidArgument, match="task name"):
        await layered.enqueue(task, {}, idempotency_key="key")
    assert store.store == {}
    assert driver.sent == []


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
    layered, driver, store = queue
    with pytest.raises(InvalidArgument, match="delay"):
        await layered.enqueue("probe.task", {}, delay=delay, idempotency_key="key")
    assert store.store == {}
    assert driver.sent == []


async def test_an_unknown_task_reserves_nothing(queue) -> None:
    layered, driver, store = queue
    with pytest.raises(InvalidArgument, match="not declared"):
        await layered.enqueue("probe.nobody", {}, idempotency_key="key")
    assert store.store == {}
    assert driver.sent == []


async def test_the_delay_cap_and_zero_are_accepted(queue) -> None:
    layered, driver, _ = queue
    assert enqueue.MAX_DELAY == timedelta(days=30)
    await layered.enqueue("probe.task", {}, delay=timedelta(0), idempotency_key="a")
    await layered.enqueue(
        "probe.task", {}, delay=enqueue.MAX_DELAY, idempotency_key="b"
    )
    assert len(driver.sent) == 2


async def test_a_delay_without_the_feature_reserves_nothing(monkeypatch) -> None:
    store = InMemoryCache()
    monkeypatch.setattr(enqueue, "atomic_store", lambda role: store)
    register_probe_task()
    driver = RecordingProvider(features=frozenset())
    layered = KernelTaskQueue(driver)
    delay = timedelta(seconds=5)
    with pytest.raises(Unsupported, match="delay"):
        await layered.enqueue("probe.task", {}, delay=delay, idempotency_key="k")
    assert store.store == {}
    # Without a key the same refusal holds: nothing is published.
    with pytest.raises(Unsupported, match="delay"):
        await layered.enqueue("probe.task", {}, delay=delay)
    assert driver.sent == []


async def test_a_bad_delay_without_a_key_publishes_nothing(queue) -> None:
    layered, driver, _ = queue
    with pytest.raises(InvalidArgument, match="delay"):
        await layered.enqueue("probe.task", {}, delay=timedelta(days=10**6))
    assert driver.sent == []
