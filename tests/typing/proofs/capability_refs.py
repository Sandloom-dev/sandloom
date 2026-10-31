"""Accessors give a module the caller's Protocol, not a driver's lifecycle."""

from __future__ import annotations

from typing import Protocol, assert_type

from sandloom.sdk.infra import (
    Cache,
    CapabilityRef,
    Notifier,
    ObjectStore,
    TaskQueue,
    cache,
    capability,
    notifier,
    objectstore,
    taskqueue,
)


class VectorIndex(Protocol):
    async def nearest(self, vector: list[float], *, limit: int) -> list[str]: ...


VECTOR_INDEX: CapabilityRef[VectorIndex] = CapabilityRef("vector_index.v1")


async def accessors_return_protocols() -> None:
    assert_type(cache(), Cache)
    assert_type(notifier(), Notifier)
    assert_type(objectstore(), ObjectStore)
    assert_type(taskqueue(), TaskQueue)
    assert_type(capability(VECTOR_INDEX), VectorIndex)
    assert_type(await capability(VECTOR_INDEX).nearest([1.0], limit=1), list[str])


async def the_lifecycle_is_not_a_callers() -> None:
    await notifier().aclose()  # type: ignore[attr-defined]
    await cache().raw_client()  # type: ignore[attr-defined]
