"""Fail-closed enqueue deduplication, independent of the broker's task IDs.

The kernel layer above the taskqueue driver (G-TASK-03): :func:`enqueue_task`
serves ``idempotency_key`` on any driver that declares the feature, and the
driver only publishes under the job id reserved here.

A pending reservation never expires automatically: a publisher crash or lost
broker acknowledgement is ambiguous. Reconcile the broker before deleting it.
Only a send that provably never reached the broker is released at once — the
driver raised :class:`~sandloom.core.infra.errors.InvalidArgument` (the payload
cannot be encoded, the task is unknown) or
:class:`~sandloom.core.infra.errors.TooLarge` — so a corrected retry with the
same key is not stuck. Anything else the driver raises (``Unavailable``,
``Timeout``, a connection error, a lost acknowledgement) may have published:
the key stays pending and the caller gets ``task_enqueue_uncertain``.
Successful reservations expire after the instance's replay window. This does
not deduplicate broker redelivery or make task effects exactly once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from typing import Any, Protocol, cast
from urllib.parse import urlsplit
from uuid import uuid4

import orjson
from fastapi import HTTPException

from sandloom.core.effects import RECORD_TTL_SECONDS, canonical_digest
from sandloom.core.infra.access import idempotency_cache
from sandloom.core.infra.contracts import TaskQueueProvider
from sandloom.core.infra.errors import InvalidArgument, TooLarge, Unsupported
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


async def enqueue_once(  # noqa: C901
    *,
    namespace: str,
    key: str,
    task: str,
    payload: Mapping[str, Any],
    delay_seconds: int | None,
    ttl_seconds: int,
    send: Callable[[str], Awaitable[str]],
) -> str:
    """Reserve ``key``, call ``send(job_id)`` once, record what it returned.

    ``send`` raising one of :data:`NOT_PUBLISHED` releases the reservation and
    re-raises it; any other error keeps the key pending
    (:class:`TaskEnqueueUncertain`).
    """
    if not key or len(key) > 200:
        raise ValueError("Task idempotency key must contain 1 to 200 characters")
    tenant = str(current_tenant_id_optional())
    cache_key = "sandloom:task-enqueue:" + canonical_digest([namespace, tenant, key])
    try:
        fingerprint = canonical_digest([task, dict(payload), delay_seconds])
    except Exception as exc:
        # Checked before reserving (a pending key has no TTL).
        raise InvalidArgument(
            f"task payload has no canonical form ({type(exc).__name__})",
            capability="taskqueue",
        ) from exc
    task_id = uuid4().hex
    pending = orjson.dumps(
        {"state": "pending", "fp": fingerprint, "id": task_id}
    ).decode()
    try:
        store = idempotency_cache()
        if "cas" not in store.features():
            raise TaskEnqueueUnavailable()
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
        job_id = await send(task_id)
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


class _PublishesUnderJobId(Protocol):
    """A taskqueue driver that declares ``idempotency_key``: its ``enqueue``
    publishes under the job id the kernel reserved (``task_id``)."""

    async def enqueue(
        self,
        task: str,
        payload: Mapping[str, Any],
        *,
        delay_seconds: int | None = None,
        task_id: str | None = None,
    ) -> str: ...


def dedupe_namespace(provider: TaskQueueProvider) -> str:
    """The dedupe namespace of a taskqueue instance.

    Its name and its broker's location (scheme, host, port, path of the
    ``broker`` option, never the credentials: a rotation must not open a new
    namespace); its name and driver for a driver without a ``broker``.
    """
    broker = provider.options.get("broker")
    if isinstance(broker, str) and broker:
        where = urlsplit(broker)
        return repr(
            (provider.name, where.scheme, where.hostname, where.port, where.path)
        )
    return repr((provider.name, provider.driver))


async def enqueue_task(
    provider: TaskQueueProvider,
    task: str,
    payload: Mapping[str, Any],
    *,
    delay_seconds: int | None = None,
    idempotency_key: str | None = None,
) -> str:
    """Enqueue ``task`` on ``provider``, deduplicated by ``idempotency_key``.

    Without a key the driver publishes once. With one, the instance must
    declare the feature ``idempotency_key`` (refused with
    :class:`Unsupported` before anything is reserved otherwise), the payload
    is snapshotted, and :func:`enqueue_once` reserves a job id the driver
    publishes under (its ``enqueue(..., task_id=)``).
    """
    if idempotency_key is None:
        return await provider.enqueue(task, payload, delay_seconds=delay_seconds)
    if "idempotency_key" not in provider.features():
        raise Unsupported(
            "idempotency_key", capability="taskqueue", instance=provider.name
        )
    snapshot = deepcopy(dict(payload))
    ttl = provider.options.get("dedupe_ttl_seconds") or RECORD_TTL_SECONDS
    publisher = cast("_PublishesUnderJobId", provider)
    return await enqueue_once(
        namespace=dedupe_namespace(provider),
        key=idempotency_key,
        task=task,
        payload=snapshot,
        delay_seconds=delay_seconds,
        ttl_seconds=int(ttl),
        send=lambda task_id: publisher.enqueue(
            task, snapshot, delay_seconds=delay_seconds, task_id=task_id
        ),
    )
