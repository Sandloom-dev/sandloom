"""Fail-closed enqueue deduplication, independent of the broker's task IDs.

A pending reservation never expires automatically: a publisher crash or lost
broker acknowledgement is ambiguous. Reconcile the broker before deleting it.
Only a send that provably never reached the broker (``send`` raised
:class:`TaskNotSent`: the payload cannot be encoded, the task is unknown) is
released at once, so a corrected retry with the same key is not stuck.
Successful reservations expire after the driver's configured replay window.
This does not deduplicate broker redelivery or make task effects exactly once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

import orjson
from fastapi import HTTPException

from sandloom.core.effects import canonical_digest
from sandloom.core.infra.access import idempotency_cache
from sandloom.core.tenant_context import current_tenant_id_optional


class TaskEnqueueConflict(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            409, "Task idempotency key was reused with different arguments"
        )


class TaskEnqueueUnavailable(HTTPException):
    def __init__(self) -> None:
        super().__init__(503, "Task deduplication store is unavailable")


class TaskNotSent(Exception):  # a signal, not an error kind
    """Raised by an enqueue ``send`` when the message provably never left.

    Only for failures BEFORE any byte reached the broker (serialization,
    unknown task, invalid arguments checked locally): the dedupe reservation
    is then released and the cause re-raised. Anything that may have reached
    the broker (connection drop, timeout, lost acknowledgement) must NOT use
    it — that stays pending + ``task_enqueue_uncertain``.
    """


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
    if not key or len(key) > 200:
        raise ValueError("Task idempotency key must contain 1 to 200 characters")
    tenant = str(current_tenant_id_optional())
    cache_key = "sandloom:task-enqueue:" + canonical_digest([namespace, tenant, key])
    fingerprint = canonical_digest([task, dict(payload), delay_seconds])
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
    except TaskNotSent as exc:
        # Deterministic, pre-broker failure: nothing was published, so the
        # reservation is released (owner-checked) and the cause surfaces.
        try:
            released = await store.compare_and_delete(cache_key, pending)
        except Exception:  # noqa: BLE001 - cannot release: stays pending
            released = False
        if not released:
            raise TaskEnqueueUncertain(task_id) from exc
        cause = exc.__cause__
        if isinstance(cause, Exception):
            raise cause from None
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
