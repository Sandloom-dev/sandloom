"""The ``lease`` capability (kernel-facing; spec section 5).

A named, fenced, time-bound lease: one holder at a time, each grant carrying a
fence strictly greater than every earlier grant's for that name.

- ``acquire`` grants the lease when nobody holds it (or the holder's lease
  expired), else answers ``None``; it never waits. ``ttl_seconds >= 3``;
  ``owner`` is a diagnostic label (``<host>:<pid>``), not an identity check.
  A name is 1 to ``MAX_LEASE_NAME_LENGTH`` characters, an owner at most as long.
- The fence grows across expiry, release and a backend restart within its
  durability class; ``renew`` keeps it.
- ``renew`` extends to a fresh ``ttl_seconds`` only while ``lease.token`` still
  holds it (``True``), else ``False``. ``release`` ends it only when
  ``lease.token`` holds it (``True``), else ``False``; the name keeps its fence
  counter.
- ``holder`` reads the current holder without changing anything.
- Expiry is measured by the backend's clock; the holder treats the lease as
  lost at a local deadline measured from before the call that set it.

Errors: ``InvalidArgument`` (TTL ``< 3``, empty or too long name), ``Unavailable``,
``Timeout``. A lost lease is a result (``False``), never an exception.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import ClassVar, Protocol

from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider

#: The shortest lease ``acquire`` accepts, in seconds.
MIN_LEASE_TTL_SECONDS = 3
#: The longest lease name (and owner label) ``acquire`` accepts.
MAX_LEASE_NAME_LENGTH = 255


@dataclass(frozen=True, slots=True)
class Lease:
    """One grant of a lease."""

    name: str
    owner: str
    #: Opaque, unique per grant.
    token: str
    #: Strictly greater than every fence granted before for this name.
    fence: int
    ttl_seconds: int


@dataclass(frozen=True, slots=True)
class LeaseHolder:
    """Who holds a lease now (read by ``sandloom doctor`` and the ops endpoint)."""

    owner: str
    fence: int


class LeaseStore(Protocol):
    """The operations the kernel calls on a ``lease`` instance."""

    async def acquire(
        self, name: str, *, ttl_seconds: int, owner: str
    ) -> Lease | None: ...
    async def renew(self, lease: Lease) -> bool: ...
    async def release(self, lease: Lease) -> bool: ...
    async def holder(self, name: str) -> LeaseHolder | None: ...


class LeaseProvider(InfraProvider, LeaseStore):
    """Base class of a ``lease`` driver's provider."""

    capability: ClassVar[str] = "lease"

    @abc.abstractmethod
    async def acquire(self, name: str, *, ttl_seconds: int, owner: str) -> Lease | None:
        """Grant ``name`` when free or expired, else ``None`` (never waits)."""

    @abc.abstractmethod
    async def renew(self, lease: Lease) -> bool:
        """Extend ``lease`` while its token holds it; ``False`` when lost."""

    @abc.abstractmethod
    async def release(self, lease: Lease) -> bool:
        """End ``lease`` when its token holds it; the fence counter stays."""

    @abc.abstractmethod
    async def holder(self, name: str) -> LeaseHolder | None:
        """The current holder of ``name``, or ``None`` when it is free."""


LEASE_CAPABILITY = CapabilitySpec(
    name="lease",
    version="v1",
    provider_contract=LeaseProvider,
    feature_kinds={
        # Every process of the deployment contends for the same lease (memory
        # lacks it).
        "shared": "guarantee",
    },
    description="Named fenced leases (the beat scheduler, the saga sweep).",
    # Kernel-facing: a module resolve or requirement is refused (B041).
    kernel_only=True,
)


__all__ = [
    "LEASE_CAPABILITY",
    "MAX_LEASE_NAME_LENGTH",
    "MIN_LEASE_TTL_SECONDS",
    "Lease",
    "LeaseHolder",
    "LeaseProvider",
    "LeaseStore",
]
