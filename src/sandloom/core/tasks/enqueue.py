"""Fail-closed enqueue deduplication, independent of the broker's task IDs.

The kernel layer above the taskqueue driver (G-TASK-03): :func:`enqueue_task`
checks what it can know without the broker, serves ``idempotency_key`` on any
driver (through the ``idempotency`` ``atomic_store``, failing closed without
one) and only then calls the driver's ``send`` under a task id the kernel
chose.

Order, so that a refusal never leaves a dedupe key behind (a pending key has
no TTL): the task name is declared, the payload validates and serialises to
JSON, the ``delay`` is valid and the instance has the feature ``delay``; then
the key is reserved (``enqueue_once``); then ``send``.

A pending reservation never expires automatically: a publisher crash or lost
broker acknowledgement is ambiguous. Reconcile the broker before deleting it.
Only a send that provably never reached the broker is released at once — the
driver raised :class:`~sandloom.core.infra.errors.InvalidArgument` (the payload
cannot be carried, the task is unknown) or
:class:`~sandloom.core.infra.errors.TooLarge` — so a corrected retry with the
same key is not stuck. Anything else the driver raises (``Unavailable``,
``Timeout``, a connection error, a lost acknowledgement) may have published:
the key stays pending and the caller gets ``task_enqueue_uncertain``.
Successful reservations expire after the instance's replay window. This does
not deduplicate broker redelivery or make task effects exactly once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any
from uuid import uuid4

import orjson
from fastapi import HTTPException
from pydantic import BaseModel

from sandloom.core.effects import RECORD_TTL_SECONDS, canonical_digest
from sandloom.core.infra.access import atomic_store
from sandloom.core.infra.contracts import TaskQueueProvider
from sandloom.core.infra.errors import InvalidArgument, TooLarge, Unsupported
from sandloom.core.tasks.registry import declared_task, encode_payload
from sandloom.core.tenant_context import current_tenant_id_optional


class TaskEnqueueConflict(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            409, "Task idempotency key was reused with different arguments"
        )


class TaskEnqueueUnavailable(HTTPException):
    def __init__(self) -> None:
        super().__init__(503, "Task deduplication store is unavailable")


class TaskEnqueueUncertain(HTTPException):
    def __init__(self, task_id: str | None = None) -> None:
        super().__init__(
            503,
            {
                "code": "task_enqueue_uncertain",
                "task_id": task_id,
                "message": (
                    "Task enqueue is pending or uncertain; reconcile before retrying"
                ),
            },
        )


#: Driver errors that mean nothing was published: the reservation is released.
NOT_PUBLISHED: tuple[type[Exception], ...] = (InvalidArgument, TooLarge)

#: The longest ``delay`` an enqueue accepts: 30 days. A broker holds a
#: delayed message in memory or a visibility window; a longer schedule belongs
#: in a periodic task or the module's own table.
MAX_DELAY = timedelta(days=30)


async def enqueue_once(  # noqa: C901
    *,
    namespace: str,
    key: str,
    task: str,
    payload: bytes,
    delay: timedelta | None,
    ttl_seconds: int,
    task_id: str,
    send: Callable[[], Awaitable[str]],
) -> str:
    """Reserve ``key``, call ``send()`` once under ``task_id``, record what it returned.

    ``send`` raising one of :data:`NOT_PUBLISHED` releases the reservation and
    re-raises it; any other error keeps the key pending
    (:class:`TaskEnqueueUncertain`).
    """
    if not key or len(key) > 200:
        raise ValueError("Task idempotency key must contain 1 to 200 characters")
    tenant = str(current_tenant_id_optional())
    cache_key = "sandloom:task-enqueue:" + canonical_digest([namespace, tenant, key])
    fingerprint = canonical_digest(
        [task, payload.decode(), None if delay is None else delay.total_seconds()]
    )
    pending = orjson.dumps(
        {"state": "pending", "fp": fingerprint, "id": task_id}
    ).decode()
    try:
        store = atomic_store("idempotency")
        created = await store.add(cache_key, pending)  # no TTL on ambiguous sends
        if not created:
            record = orjson.loads(await store.get(cache_key) or "null")
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                raise TaskEnqueueUncertain()
            if record.get("fp") != fingerprint:
                raise TaskEnqueueConflict()
            if record.get("state") != "sent":
                raise TaskEnqueueUncertain(record["id"])
            return str(record["id"])
    except HTTPException:
        raise
    except Exception as exc:
        raise TaskEnqueueUnavailable() from exc
    try:
        job_id = await send()
    except NOT_PUBLISHED as exc:
        # Deterministic, pre-broker failure: nothing was published, so the
        # reservation is released (owner-checked) and the error surfaces.
        try:
            released = await store.compare_and_delete(cache_key, pending)
        except Exception:  # noqa: BLE001 - cannot release: stays pending
            released = False
        if not released:
            raise TaskEnqueueUncertain(task_id) from exc
        raise
    except Exception as exc:
        # Never release on a publish failure: the broker may have accepted it.
        raise TaskEnqueueUncertain(task_id) from exc
    try:
        sent = orjson.dumps({"state": "sent", "fp": fingerprint, "id": job_id}).decode()
        if not await store.compare_and_set(
            cache_key, pending, sent, ttl_seconds=ttl_seconds
        ):
            raise TaskEnqueueUncertain()
        return job_id
    except Exception as exc:
        # Never release on a CAS failure: the broker accepted the task.
        raise TaskEnqueueUncertain(task_id) from exc


async def dedupe_namespace(provider: TaskQueueProvider) -> str:
    """The dedupe namespace of a taskqueue instance.

    Its name and the driver's isolation identity (the broker it publishes to,
    never a credential: a rotation must not open a new namespace), so two
    brokers that share an idempotency store never share a namespace. A driver
    without an identity falls back to its name.
    """
    identity = await provider.isolation_identity()
    return repr((provider.name, identity or provider.driver))


def _check_delay(provider: TaskQueueProvider, delay: object) -> timedelta | None:
    """What the layer knows about ``delay`` without the broker (spec 8.1)."""
    if delay is None:
        return None
    if not isinstance(delay, timedelta) or not timedelta(0) <= delay <= MAX_DELAY:
        raise InvalidArgument(
            f"delay must be a timedelta from 0 to {MAX_DELAY}",
            capability="taskqueue",
            instance=provider.name,
        )
    if "delay" not in provider.features():
        raise Unsupported("delay", capability="taskqueue", instance=provider.name)
    return delay


async def enqueue_task(
    provider: TaskQueueProvider,
    task: str,
    payload: BaseModel | Mapping[str, Any] | None,
    *,
    delay: timedelta | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Enqueue ``task`` on ``provider``, deduplicated by ``idempotency_key``.

    Before anything is reserved or sent (spec 8.1): ``task`` must be a name
    declared in this process (:class:`InvalidArgument` otherwise), ``payload``
    must validate against the task's model and encode to JSON
    (:class:`InvalidArgument`), ``delay`` must be ``None`` or a ``timedelta``
    in ``[0, MAX_DELAY]`` (:class:`InvalidArgument`) and needs the instance's
    feature ``delay`` (:class:`Unsupported`): a refusal leaves no pending key
    behind. Then, with a key, :func:`enqueue_once` reserves the task id (the
    ``idempotency`` ``atomic_store``: without one the enqueue is refused,
    :class:`TaskEnqueueUnavailable`); then the driver's ``send``.
    """
    if not isinstance(task, str) or not task:
        raise InvalidArgument(
            "the task name must be a non-empty string",
            capability="taskqueue",
            instance=provider.name,
        )
    declared = declared_task(task)
    if declared is None:
        raise InvalidArgument(
            f"task {task!r} is not declared: its module's tasks.py must be loaded",
            capability="taskqueue",
            instance=provider.name,
        )
    body = encode_payload(declared, payload)
    checked_delay = _check_delay(provider, delay)
    task_id = uuid4().hex

    async def send() -> str:
        return await provider.send(
            task,
            body,
            task_id=task_id,
            delay=checked_delay,
            at_least_once=declared.at_least_once,
            queue=declared.queue,
        )

    if idempotency_key is None:
        return await send()
    ttl = provider.options.get("dedupe_ttl_seconds") or RECORD_TTL_SECONDS
    return await enqueue_once(
        namespace=await dedupe_namespace(provider),
        key=idempotency_key,
        task=task,
        payload=body,
        delay=checked_delay,
        ttl_seconds=int(ttl),
        task_id=task_id,
        send=send,
    )
