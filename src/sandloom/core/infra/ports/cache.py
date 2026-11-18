"""The ``cache`` capability (module-facing, contract major 2; spec section 3).

The cache is the ephemeral store a module calls through ``cache()``: operations on
UTF-8 string values with optional whole-second TTLs, never a raw client.

* **It may evict and may lose data.** Any key can vanish before its TTL and a
  restart may empty the store. Nothing a kernel guarantee depends on lives here
  (revocation, blocks and idempotency records are on ``atomic_store``).
* **The kernel's users fail open by contract.** The HTTP rate limiter
  (``ingress/rate_limit.py``) and the consumer inbox (``eventing/consumer_inbox.py``)
  treat an :class:`~sandloom.core.infra.errors.InfraError` from the cache as "no
  counter" / "not seen": the request is served, the event is handled. A cache
  outage costs a limit or a dedupe hint, never availability.
* ``windowed_counter`` is required: a driver serves it in one round trip
  (performance hint ``one_round_trip``) or inherits the default of two operations.
* ``compare_and_set`` and ``compare_and_delete`` are behind the behaviour feature
  ``cas``; without it both raise ``Unsupported("cas")``.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from typing import ClassVar, Protocol

from sandloom.core.infra.errors import Unsupported
from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider


class Cache(Protocol):
    """What a module calls on ``cache()``: the operations, not the lifecycle."""

    async def get(self, key: str) -> str | None: ...
    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool: ...
    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int: ...
    async def get_many(self, keys: Sequence[str]) -> list[str | None]: ...
    async def set_many(
        self, items: Mapping[str, str], *, ttl_seconds: int | None = None
    ) -> None: ...
    async def windowed_counter(
        self, key: str, previous_key: str, *, ttl_seconds: int
    ) -> tuple[int, str | None]: ...
    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool: ...  # feature cas
    async def compare_and_delete(self, key: str, expected: str) -> bool: ...  # cas


class CacheProvider(InfraProvider, Cache):
    """Ephemeral key/value store capability (a networked store, in-memory, ...).

    Values are UTF-8 strings; keys are strings of at most 1024 bytes. TTLs are
    whole seconds ``>= 1``; ``None`` means no expiry. A key is not readable after
    ``ttl_seconds + 1`` s. Each driver class states ``contract_version = 2``.

    Errors: ``InvalidArgument`` (key length, TTL below 1, a non-integer value for
    ``incr``), ``TooLarge``, ``Unavailable``, ``Timeout``; ``compare_and_*`` also
    ``Unsupported("cas")``. A miss is a result: an absent key answers ``None``.
    """

    capability: ClassVar[str] = "cache"

    @abc.abstractmethod
    async def get(self, key: str) -> str | None:
        """Return the value for ``key``, or ``None`` if absent/expired."""

    @abc.abstractmethod
    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        """Store ``value`` under ``key`` (overwriting), optionally with a TTL."""

    @abc.abstractmethod
    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        """Atomically store ``value`` ONLY if ``key`` is absent (SET NX).

        Of concurrent calls for one key exactly one answers ``True``.
        """

    @abc.abstractmethod
    async def delete(self, key: str) -> None:
        """Remove ``key`` (no error if it does not exist)."""

    @abc.abstractmethod
    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        """Atomically increment the integer at ``key`` and return the new value.

        When the key is created (result ``1``) and ``ttl_seconds`` is given, the
        expiry is set in the same atomic step, so no counter leaks without a TTL.
        A stored value that is not an integer raises ``InvalidArgument``.
        """

    async def get_many(self, keys: Sequence[str]) -> list[str | None]:
        """Values of ``keys`` in order (``None`` for absent ones).

        Performance hint ``one_round_trip`` where the driver batches; the default
        calls :meth:`get` for each key.
        """
        return [await self.get(key) for key in keys]

    async def set_many(
        self, items: Mapping[str, str], *, ttl_seconds: int | None = None
    ) -> None:
        """Store every ``key -> value`` of ``items`` (overwriting), same TTL.

        Not atomic across keys. The default calls :meth:`set` for each item.
        """
        for key, value in items.items():
            await self.set(key, value, ttl_seconds=ttl_seconds)

    async def windowed_counter(
        self, key: str, previous_key: str, *, ttl_seconds: int
    ) -> tuple[int, str | None]:
        """:meth:`incr` ``key`` (TTL on creation), then read ``previous_key``.

        Returns ``(new count of key, value of previous_key or None)``: the
        sliding-window rate limiter's per-request step. Required. Performance
        hint ``one_round_trip``; the default is the two operations.
        """
        count = await self.incr(key, ttl_seconds=ttl_seconds)
        return count, await self.get(previous_key)

    async def compare_and_set(
        self,
        key: str,
        expected: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        """Replace ``key`` with ``value`` ONLY if it holds ``expected`` (``cas``).

        ``False`` when the key is absent or holds something else. ``ttl_seconds``
        ``None`` removes any earlier TTL. Without ``cas`` this raises
        ``Unsupported("cas")``; a driver that declares ``cas`` overrides it.
        """
        raise Unsupported("cas", capability="cache", instance=self.name)

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        """Delete ``key`` ONLY if it holds ``expected`` (``cas``)."""
        raise Unsupported("cas", capability="cache", instance=self.name)


CACHE_CAPABILITY = CapabilitySpec(
    name="cache",
    version="v1",
    major=2,
    provider_contract=CacheProvider,
    feature_kinds={"cas": "behaviour"},
    description="Ephemeral key/value store (operation-based).",
)


__all__ = [
    "CACHE_CAPABILITY",
    "Cache",
    "CacheProvider",
]
