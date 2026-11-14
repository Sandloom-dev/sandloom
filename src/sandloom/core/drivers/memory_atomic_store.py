"""The in-memory ``atomic_store`` driver (``memory``): development and tests.

State lives in the process, so it is neither shared between processes nor
durable: the driver is ``development_only`` (refused in production with no
accepted risk to lift it) and declares ``never_evicts`` only, since it never
drops a key before its TTL. Every operation runs without an ``await`` between
its read and its write, so on one event loop each is atomic.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sandloom.driverkit import AtomicStoreProvider
from sandloom.driverkit.atomic_store import (
    ATOMIC_STORE_PERFORMANCE_HINTS,
    DEFAULT_MAX_VALUE_BYTES,
    check_key,
    check_subject_state,
    check_ttl,
    check_value,
    subject_state_rank,
)

#: Names this process in ``isolation_identity``: two memory stores of one
#: process share a failure domain, two processes never do.
_PROCESS_ID = uuid.uuid4().hex[:12]


class MemoryAtomicStoreOptions(BaseModel):
    """Options of the ``memory`` atomic_store driver."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_value_bytes: int = Field(default=DEFAULT_MAX_VALUE_BYTES, ge=1)


class MemoryAtomicStoreProvider(AtomicStoreProvider):
    """A process-local key-value store with TTL."""

    contract_version = 1
    supported_features = frozenset({"never_evicts"})
    performance_hints = ATOMIC_STORE_PERFORMANCE_HINTS
    development_only = True
    options_schema = MemoryAtomicStoreOptions

    def __init__(self, *, name: str, driver: str, options: Mapping[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._limit = MemoryAtomicStoreOptions.model_validate(
            self.options
        ).max_value_bytes
        #: ``key -> (value, expires_at)``; ``expires_at`` is ``None`` for no expiry.
        self._rows: dict[str, tuple[str, float | None]] = {}
        #: Seconds on a monotonic scale; tests substitute a clock they advance.
        self.clock: Callable[[], float] = time.monotonic

    # ------------------------------------------------------------------ rows
    def _live(self, key: str) -> str | None:
        """The live value of ``key``; an expired row is dropped, so it is absent."""
        row = self._rows.get(key)
        if row is None:
            return None
        value, expires_at = row
        if expires_at is not None and self.clock() >= expires_at:
            del self._rows[key]
            return None
        return value

    def _write(self, key: str, value: str, ttl: int) -> None:
        self._rows[key] = (value, self.clock() + ttl if ttl else None)

    # ------------------------------------------------------------ operations
    async def get(self, key: str) -> str | None:
        return self._live(check_key(key, instance=self.name))

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        check_key(key, instance=self.name)
        check_value(value, limit=self._limit, instance=self.name)
        self._write(key, value, check_ttl(ttl_seconds, instance=self.name))

    async def delete(self, key: str) -> None:
        self._rows.pop(check_key(key, instance=self.name), None)

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        created, _ = await self.add_or_get(key, value, ttl_seconds=ttl_seconds)
        return created

    async def add_or_get(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> tuple[bool, str | None]:
        check_key(key, instance=self.name)
        check_value(value, limit=self._limit, instance=self.name)
        ttl = check_ttl(ttl_seconds, instance=self.name)
        current = self._live(key)
        if current is not None:
            return False, current
        self._write(key, value, ttl)
        return True, None

    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        check_key(key, instance=self.name)
        check_value(expected, limit=self._limit, instance=self.name)
        check_value(value, limit=self._limit, instance=self.name)
        ttl = check_ttl(ttl_seconds, instance=self.name)
        if self._live(key) != expected:
            return False
        self._write(key, value, ttl)
        return True

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        check_key(key, instance=self.name)
        check_value(expected, limit=self._limit, instance=self.name)
        if self._live(key) != expected:
            return False
        del self._rows[key]
        return True

    async def set_subject_state(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        check_key(key, instance=self.name)
        check_subject_state(value, instance=self.name)
        ttl = check_ttl(ttl_seconds, instance=self.name)
        current = self._live(key)
        stored = subject_state_rank(current)
        ours = subject_state_rank(value)
        # ``ours`` is in the domain (checked above); a stored value outside it
        # is corrupt and replaced.
        if (
            current is not None
            and stored is not None
            and ours is not None
            and stored >= ours
        ):
            return current
        self._write(key, value, ttl)
        return value

    async def isolation_identity(self) -> str | None:
        return f"memory:{_PROCESS_ID}"


__all__ = [
    "MemoryAtomicStoreOptions",
    "MemoryAtomicStoreProvider",
]
