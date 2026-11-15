"""The in-memory ``cache`` driver (``memory``): development and tests only.

State lives in the process. Compared with ``redis`` this driver gives less, and
a deployment must not rely on any of it:

* **Per process, not shared.** Two processes (two workers, an app and a worker,
  two services) each hold their own keys. A rate limit counts per process, a
  consumer inbox dedupes per process.
* **Lost on restart.** A stop empties the store.
* **No isolation between processes.** ``isolation_identity`` names the process:
  there is no common store whose failure domain two instances could share.

The driver is ``development_only``: production refuses it with no accepted risk
to lift the refusal, and no resolver picks it on its own. It serves only an
instance whose ``INFRA`` names it (``driver: memory``). It is never an automatic
fallback for an unreachable Redis.

It keeps the whole contract (major 2) and declares ``cas``. Every operation
runs under one lock with no ``await`` between its read and its write, so it is
atomic on one event loop and across threads. A key is dropped when its TTL has
passed, so it is never readable after ``ttl_seconds``.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sandloom.driverkit import (
    CacheProvider,
    InvalidArgument,
    TooLarge,
)
from sandloom.driverkit.atomic_store import (
    DEFAULT_MAX_VALUE_BYTES,
    MAX_KEY_BYTES,
    MAX_TTL_SECONDS,
)

#: Names this process in ``isolation_identity``: two memory caches of one
#: process share a failure domain, two processes never do.
_PROCESS_ID = uuid.uuid4().hex[:12]

#: What ``incr`` accepts as a stored integer (the form Redis ``INCR`` accepts).
_INTEGER = re.compile(r"-?[0-9]+")

#: Every operation is one step in process: the batch and fused ones too.
MEMORY_CACHE_PERFORMANCE_HINTS = MappingProxyType(
    dict.fromkeys(("get_many", "set_many", "windowed_counter"), "one_round_trip")
)


class MemoryCacheOptions(BaseModel):
    """Options of the ``memory`` cache driver."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_value_bytes: int = Field(default=DEFAULT_MAX_VALUE_BYTES, ge=1)


class MemoryCacheProvider(CacheProvider):
    """A process-local key-value cache with TTL. Development and tests only."""

    contract_version = 2
    supported_features = frozenset({"cas"})
    performance_hints = MEMORY_CACHE_PERFORMANCE_HINTS
    development_only = True
    options_schema = MemoryCacheOptions

    def __init__(self, *, name: str, driver: str, options: Mapping[str, Any]) -> None:
        super().__init__(name=name, driver=driver, options=options)
        self._limit = MemoryCacheOptions.model_validate(self.options).max_value_bytes
        #: ``key -> (value, expires_at)``; ``expires_at`` is ``None`` for no expiry.
        self._rows: dict[str, tuple[str, float | None]] = {}
        self._lock = threading.Lock()
        #: Seconds on a monotonic scale; tests substitute a clock they advance.
        self.clock: Callable[[], float] = time.monotonic

    # ------------------------------------------------------------ arguments
    def _invalid(self, message: str) -> InvalidArgument:
        return InvalidArgument(message, capability="cache", instance=self.name)

    def _key(self, key: object) -> str:
        if not isinstance(key, str) or not key:
            raise self._invalid("a cache key is a non-empty string")
        try:
            size = len(key.encode("utf-8"))
        except UnicodeEncodeError:
            raise self._invalid("a cache key must be encodable as UTF-8") from None
        if size > MAX_KEY_BYTES:
            raise self._invalid(f"a cache key is at most {MAX_KEY_BYTES} bytes")
        return key

    def _value(self, value: object) -> str:
        if not isinstance(value, str):
            raise self._invalid("a cache value is a string")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise self._invalid("a cache value must be encodable as UTF-8") from None
        if size > self._limit:
            raise TooLarge(
                f"a cache value is at most {self._limit} bytes, got {size}",
                capability="cache",
                instance=self.name,
            )
        return value

    def _ttl(self, ttl_seconds: int | None) -> int:
        """The TTL in whole seconds, ``0`` for none."""
        if ttl_seconds is None:
            return 0
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or ttl_seconds < 1
            or ttl_seconds > MAX_TTL_SECONDS
        ):
            raise self._invalid(
                f"a cache TTL is a whole number of seconds from 1 to {MAX_TTL_SECONDS}"
            )
        return ttl_seconds

    # ----------------------------------------------------------------- rows
    def _live(self, key: str) -> str | None:
        """The live value of ``key``; an expired row is dropped. Under the lock."""
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

    def _incr(self, key: str, ttl: int) -> int:
        """Increment under the lock; the TTL is set when the key is created."""
        current = self._live(key)
        if current is None:
            self._write(key, "1", ttl)
            return 1
        if not _INTEGER.fullmatch(current):
            raise self._invalid("the value at this key is not an integer")
        count = int(current) + 1
        # An existing key keeps its expiry.
        self._rows[key] = (str(count), self._rows[key][1])
        return count

    # ----------------------------------------------------------- operations
    async def get(self, key: str) -> str | None:
        self._key(key)
        with self._lock:
            return self._live(key)

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        self._key(key)
        self._value(value)
        ttl = self._ttl(ttl_seconds)
        with self._lock:
            self._write(key, value, ttl)

    async def delete(self, key: str) -> None:
        self._key(key)
        with self._lock:
            self._rows.pop(key, None)

    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        self._key(key)
        self._value(value)
        ttl = self._ttl(ttl_seconds)
        with self._lock:
            if self._live(key) is not None:
                return False
            self._write(key, value, ttl)
            return True

    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        self._key(key)
        ttl = self._ttl(ttl_seconds)
        with self._lock:
            return self._incr(key, ttl)

    async def get_many(self, keys: Sequence[str]) -> list[str | None]:
        checked = [self._key(key) for key in keys]
        with self._lock:
            return [self._live(key) for key in checked]

    async def set_many(
        self, items: Mapping[str, str], *, ttl_seconds: int | None = None
    ) -> None:
        for key, value in items.items():
            self._key(key)
            self._value(value)
        ttl = self._ttl(ttl_seconds)
        with self._lock:
            for key, value in items.items():
                self._write(key, value, ttl)

    async def windowed_counter(
        self, key: str, previous_key: str, *, ttl_seconds: int
    ) -> tuple[int, str | None]:
        self._key(key)
        self._key(previous_key)
        ttl = self._ttl(ttl_seconds)
        with self._lock:
            count = self._incr(key, ttl)
            return count, self._live(previous_key)

    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        self._key(key)
        self._value(expected)
        self._value(value)
        ttl = self._ttl(ttl_seconds)
        with self._lock:
            if self._live(key) != expected:
                return False
            # ``ttl`` 0 stores no expiry: the key loses any earlier TTL.
            self._write(key, value, ttl)
            return True

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        self._key(key)
        self._value(expected)
        with self._lock:
            if self._live(key) != expected:
                return False
            del self._rows[key]
            return True

    async def isolation_identity(self) -> str | None:
        return f"memory:{_PROCESS_ID}"


__all__ = [
    "MEMORY_CACHE_PERFORMANCE_HINTS",
    "MemoryCacheOptions",
    "MemoryCacheProvider",
]
