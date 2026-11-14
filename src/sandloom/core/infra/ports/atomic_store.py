"""The ``atomic_store`` capability (kernel-facing; spec section 4).

A durable key-value store whose every operation is atomic: the kernel keeps
revocation marks, subject blocks, RPC nonces and idempotency reservations
there. Unlike ``cache`` it may not evict or lose an acknowledged write within
the driver's durability class.

- **Every operation is required and atomic.** No portable default composes one
  from others; a driver that needs a compare-and-set loop writes it inside.
- **Expired is absent** for every operation, also while the backend still holds
  the row: ``add`` / ``add_or_get`` create over it, ``compare_and_*`` answer
  ``False`` on it and ``set_subject_state`` writes over it.
- TTLs are whole seconds, ``>= 1``; ``None`` means no expiry, also on
  ``compare_and_set`` (the key loses any earlier TTL). A key is not readable
  after ``ttl_seconds + 1`` s.
- ``set_subject_state`` ranks values of the domain of
  :func:`sandloom.core.infra.contracts.parse_subject_state`
  (``v1:after:<decimal>`` / ``v1:blocked:<decimal>``): every ``blocked`` above
  every ``after``, within a kind the instant compared exactly as a decimal
  (never a float, never text). It writes only a higher value, replaces a stored
  value outside the domain, returns the value held after the call, and applies
  ``ttl_seconds`` only when it writes. A value outside the domain raises
  ``InvalidArgument``.

Errors: every operation raises ``InvalidArgument`` (key, TTL, subject-state
domain), ``TooLarge``, ``Unavailable`` (also a refused write on a full
non-evicting store, and contention past a driver's CAS bound) or ``Timeout``.
"""

from __future__ import annotations

import abc
from typing import ClassVar, Protocol

from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider


class AtomicStore(Protocol):
    """The operations the kernel calls on an ``atomic_store`` instance."""

    async def get(self, key: str) -> str | None: ...
    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool: ...
    async def add_or_get(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> tuple[bool, str | None]: ...
    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool: ...
    async def compare_and_delete(self, key: str, expected: str) -> bool: ...
    async def set_subject_state(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str: ...


class AtomicStoreProvider(InfraProvider, AtomicStore):
    """Base class of an ``atomic_store`` driver's provider."""

    capability: ClassVar[str] = "atomic_store"

    @abc.abstractmethod
    async def get(self, key: str) -> str | None:
        """The live value of ``key``; ``None`` only when it is known absent."""

    @abc.abstractmethod
    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        """Write ``value`` unconditionally."""

    @abc.abstractmethod
    async def delete(self, key: str) -> None:
        """Remove ``key``; an absent key succeeds."""

    @abc.abstractmethod
    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        """Create ``key`` only when absent: of concurrent adds exactly one wins."""

    @abc.abstractmethod
    async def add_or_get(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> tuple[bool, str | None]:
        """``(True, None)`` when this call created ``key``, else ``(False,
        value)`` with the value that blocked it (never ``None``)."""

    @abc.abstractmethod
    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        """Swap to ``value`` only while ``key`` holds ``expected`` (absent:
        ``False``)."""

    @abc.abstractmethod
    async def compare_and_delete(self, key: str, expected: str) -> bool:
        """Delete ``key`` only while it holds ``expected``."""

    @abc.abstractmethod
    async def set_subject_state(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        """Write ``value`` only when it ranks above the stored one; return the
        value held after the call (see the module docstring)."""


ATOMIC_STORE_CAPABILITY = CapabilitySpec(
    name="atomic_store",
    version="v1",
    provider_contract=AtomicStoreProvider,
    feature_kinds={
        # Every process of the deployment reads the same state (memory lacks it).
        "shared": "guarantee",
        # The store never evicts a key before its TTL; the driver verifies it
        # on aopen and in health.
        "never_evicts": "guarantee",
    },
    description="Durable atomic key-value store (security marks, idempotency).",
)


__all__ = ["ATOMIC_STORE_CAPABILITY", "AtomicStore", "AtomicStoreProvider"]
