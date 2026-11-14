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
import re
from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import ClassVar, Protocol

from sandloom.core.infra.errors import InvalidArgument, TooLarge
from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider

#: A key is a string of at most this many UTF-8 bytes.
MAX_KEY_BYTES = 1024
#: The default bound on a value, in UTF-8 bytes (a driver option moves it).
DEFAULT_MAX_VALUE_BYTES = 1_048_576

#: The value domain of ``set_subject_state``.
_SUBJECT_STATE = re.compile(r"v1:(after|blocked):([0-9]+)(?:\.([0-9]*))?", re.ASCII)
#: Kind ranks of the subject-state order (``blocked`` above every ``after``).
SUBJECT_STATE_RANKS: Mapping[str, int] = MappingProxyType({"after": 0, "blocked": 1})

#: The operations a driver answers in one round trip (the request-path budget;
#: ADR-0016 "What it must not change"). A hint, never read by a decision.
ATOMIC_STORE_PERFORMANCE_HINTS = MappingProxyType(
    dict.fromkeys(
        ("get", "add_or_get", "set_subject_state", "compare_and_set"),
        "one_round_trip",
    )
)


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


def check_key(key: object, *, instance: str | None = None) -> str:
    """``key`` when it is a non-empty string of at most :data:`MAX_KEY_BYTES`
    UTF-8 bytes, else ``InvalidArgument``. Drivers call it first, so the same
    key is refused the same way by every driver."""
    if not isinstance(key, str) or not key:
        raise InvalidArgument(
            "an atomic_store key is a non-empty string",
            capability="atomic_store",
            instance=instance,
        )
    try:
        size = len(key.encode("utf-8"))
    except UnicodeEncodeError:
        raise InvalidArgument(
            "an atomic_store key must be encodable as UTF-8",
            capability="atomic_store",
            instance=instance,
        ) from None
    if size > MAX_KEY_BYTES:
        raise InvalidArgument(
            f"an atomic_store key is at most {MAX_KEY_BYTES} bytes, got {size}",
            capability="atomic_store",
            instance=instance,
        )
    return key


def check_value(
    value: object, *, limit: int = DEFAULT_MAX_VALUE_BYTES, instance: str | None = None
) -> str:
    """``value`` when it is a UTF-8 string within ``limit`` bytes: another type
    is ``InvalidArgument``, a larger one ``TooLarge``."""
    if not isinstance(value, str):
        raise InvalidArgument(
            "an atomic_store value is a string",
            capability="atomic_store",
            instance=instance,
        )
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise InvalidArgument(
            "an atomic_store value must be encodable as UTF-8",
            capability="atomic_store",
            instance=instance,
        ) from None
    if size > limit:
        raise TooLarge(
            f"an atomic_store value is at most {limit} bytes, got {size}",
            capability="atomic_store",
            instance=instance,
        )
    return value


def check_ttl(ttl_seconds: object, *, instance: str | None = None) -> int:
    """The TTL as a whole number of seconds, ``0`` for no expiry (``None``);
    a bool, a non-integer or a TTL below 1 is ``InvalidArgument``."""
    if ttl_seconds is None:
        return 0
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or ttl_seconds < 1
    ):
        raise InvalidArgument(
            "an atomic_store TTL is a whole number of seconds >= 1 or None",
            capability="atomic_store",
            instance=instance,
        )
    return ttl_seconds


def parse_subject_state(raw: str | None) -> tuple[str, Decimal] | None:
    """``(kind, instant)`` of a subject-state value, else ``None`` (absent or
    outside the domain). ``instant`` is the exact decimal the value carries."""
    if raw is None:
        return None
    match = _SUBJECT_STATE.fullmatch(raw)
    if match is None:
        return None
    whole, frac = match.group(2), match.group(3) or "0"
    return match.group(1), Decimal(f"{whole}.{frac}")


def subject_state_rank(raw: str | None) -> tuple[int, Decimal] | None:
    """The rank of a subject-state value: ``blocked`` above every ``after``,
    within a kind the instant as an exact :class:`~decimal.Decimal`. ``None``
    for ``None`` or a value outside the domain (a corrupt stored value)."""
    parsed = parse_subject_state(raw)
    if parsed is None:
        return None
    return SUBJECT_STATE_RANKS[parsed[0]], parsed[1]


def check_subject_state(value: object, *, instance: str | None = None) -> str:
    """``value`` when it is in the domain of ``set_subject_state``
    (``v1:after:<decimal>`` / ``v1:blocked:<decimal>``), else ``InvalidArgument``."""
    if not isinstance(value, str) or parse_subject_state(value) is None:
        raise InvalidArgument(
            "a subject state is v1:after:<decimal> or v1:blocked:<decimal>",
            capability="atomic_store",
            instance=instance,
        )
    return value


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
    # Kernel-facing: a module resolve or requirement is refused (B041).
    kernel_only=True,
)


__all__ = [
    "ATOMIC_STORE_CAPABILITY",
    "ATOMIC_STORE_PERFORMANCE_HINTS",
    "DEFAULT_MAX_VALUE_BYTES",
    "MAX_KEY_BYTES",
    "SUBJECT_STATE_RANKS",
    "AtomicStore",
    "AtomicStoreProvider",
    "check_key",
    "check_subject_state",
    "check_ttl",
    "check_value",
    "parse_subject_state",
    "subject_state_rank",
]
