"""The ``memory`` lease driver: one process, for development and tests.

Leases live in a dict of this provider, so only the holders in this process
contend (the ``shared`` guarantee is not declared) and nothing survives a
restart. It is ``development_only``: refused in production with no accepted
risk. Expiry is read from the monotonic clock, the fence counter of a name is
kept for the provider's lifetime (released and expired names keep it).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from sandloom.driverkit import (
    InvalidArgument,
    Lease,
    LeaseHolder,
    LeaseProvider,
)

#: The shortest lease ``acquire`` accepts (the contract's ``MIN_LEASE_TTL_SECONDS``).
_MIN_TTL_SECONDS = 3
_MAX_NAME_LENGTH = 255
#: One id per process: two instances of this driver name the same domain.
_PROCESS_ID = uuid.uuid4().hex[:16]


@dataclass(slots=True)
class _Held:
    owner: str
    token: str
    fence: int
    expires_at: float


def _check_acquire_arguments(
    name: str, ttl_seconds: int, owner: str, *, capability: str
) -> None:
    """The argument errors a lease driver raises (spec 5); each driver keeps its
    own copy, since a driver imports only ``sandloom.driverkit``."""
    if not isinstance(name, str) or not name or len(name) > _MAX_NAME_LENGTH:
        raise InvalidArgument(
            f"a lease name is 1..{_MAX_NAME_LENGTH} characters", capability=capability
        )
    if not isinstance(owner, str) or len(owner) > _MAX_NAME_LENGTH:
        raise InvalidArgument(
            f"a lease owner is a label of up to {_MAX_NAME_LENGTH} characters",
            capability=capability,
        )
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or ttl_seconds < _MIN_TTL_SECONDS
    ):
        raise InvalidArgument(
            f"a lease TTL is a whole number of seconds >= {_MIN_TTL_SECONDS}",
            capability=capability,
        )


class MemoryLeaseProvider(LeaseProvider):
    """In-process fenced leases (development and tests only)."""

    contract_version = 1
    supported_features: ClassVar[frozenset[str]] = frozenset()
    development_only = True

    def __init__(
        self,
        *,
        name: str,
        driver: str,
        options: dict[str, object],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._clock = clock
        self._held: dict[str, _Held] = {}
        self._fences: dict[str, int] = {}
        self._identity = f"{driver}:{_PROCESS_ID}"

    async def acquire(self, name: str, *, ttl_seconds: int, owner: str) -> Lease | None:
        _check_acquire_arguments(name, ttl_seconds, owner, capability=self.capability)
        now = self._clock()
        current = self._held.get(name)
        if current is not None and current.expires_at > now:
            return None
        fence = self._fences.get(name, 0) + 1
        self._fences[name] = fence
        token = uuid.uuid4().hex
        self._held[name] = _Held(owner, token, fence, now + ttl_seconds)
        return Lease(
            name=name, owner=owner, token=token, fence=fence, ttl_seconds=ttl_seconds
        )

    async def renew(self, lease: Lease) -> bool:
        current = self._held.get(lease.name)
        now = self._clock()
        if current is None or current.token != lease.token or current.expires_at <= now:
            return False
        current.expires_at = now + lease.ttl_seconds
        return True

    async def release(self, lease: Lease) -> bool:
        current = self._held.get(lease.name)
        if (
            current is None
            or current.token != lease.token
            or current.expires_at <= self._clock()
        ):
            return False
        current.expires_at = self._clock()
        return True

    async def holder(self, name: str) -> LeaseHolder | None:
        current = self._held.get(name)
        if current is None or current.expires_at <= self._clock():
            return None
        return LeaseHolder(owner=current.owner, fence=current.fence)

    async def isolation_identity(self) -> str | None:
        return self._identity


__all__ = ["MemoryLeaseProvider"]
