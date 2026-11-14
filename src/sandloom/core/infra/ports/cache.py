"""The ``cache`` capability (module-facing; spec section 3).

Until ``idk-cache-contract`` replaces it, this is the cache contract the kernel
has used so far, moved here unchanged from :mod:`sandloom.core.infra.contracts`
(which re-exports it), with the capability's spec.
"""

from __future__ import annotations

import abc
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, ClassVar, Protocol

from sandloom.core.infra.ports._base import CapabilitySpec, InfraProvider


class Cache(Protocol):
    """What a module calls on ``cache()``: the operations, not the lifecycle."""

    async def get(self, key: str) -> str | None: ...
    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None: ...
    async def add(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool: ...
    async def delete(self, key: str) -> None: ...
    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int: ...
    async def get_many(self, keys: Sequence[str]) -> list[str | None]: ...
    async def set_many(
        self, items: Mapping[str, str], *, ttl_seconds: int | None = None
    ) -> None: ...
    async def compare_and_set(
        self, key: str, expected: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool: ...
    async def compare_and_delete(self, key: str, expected: str) -> bool: ...
    async def incr_window(
        self, key: str, previous_key: str, *, ttl_seconds: int | None = None
    ) -> tuple[int, str | None]: ...
    async def incr_many(
        self, keys: Sequence[str], *, ttl_seconds: int | None = None
    ) -> list[int]: ...
    async def set_nx_get(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> tuple[bool, str | None]: ...
    async def set_max(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str: ...


class CacheProvider(InfraProvider, Cache):
    """Ephemeral key/value store capability (e.g. Redis, Memcached, in-memory).

    The contract is a small set of backend-agnostic *operations* — never the
    raw client. A new backend is plug-compatible the moment it implements these
    five methods; callers cannot reach for engine-specific surface (``eval``,
    ``pipeline``, ``pubsub``, …) and quietly couple to one engine.

    Values are opaque UTF-8 strings; callers serialize structured data
    themselves. TTLs are whole seconds (``None`` = no expiry).
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

        Returns ``True`` when this call created the key, ``False`` when it
        already existed. The atomic primitive behind reserve-or-conflict
        guards (idempotency keys).
        """

    @abc.abstractmethod
    async def delete(self, key: str) -> None:
        """Remove ``key`` (no error if it does not exist)."""

    @abc.abstractmethod
    async def incr(self, key: str, *, ttl_seconds: int | None = None) -> int:
        """Atomically increment the integer at ``key`` and return the new value.

        When the key is first created (result ``1``) and ``ttl_seconds`` is
        given, the expiry is set in the same atomic step — the primitive behind
        fixed-window rate limiting (no counter can leak without a TTL).
        """

    async def get_many(self, keys: Sequence[str]) -> list[str | None]:
        """Values of ``keys`` in order (``None`` for absent/expired ones).

        Batch op (performance hint ``get_many``): drivers serve it in one round
        trip (Redis ``MGET``); the default falls back to one :meth:`get` each.
        """
        return [await self.get(key) for key in keys]

    async def set_many(
        self, items: Mapping[str, str], *, ttl_seconds: int | None = None
    ) -> None:
        """Store every ``key -> value`` of ``items`` (overwriting), same TTL.

        Batch op (performance hint ``set_many``): drivers serve it in one round
        trip (a Redis pipeline); the default falls back to one :meth:`set` each.
        Not atomic across keys.
        """
        for key, value in items.items():
            await self.set(key, value, ttl_seconds=ttl_seconds)

    async def compare_and_set(
        self,
        key: str,
        expected: str,
        value: str,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        """Atomically replace ``key`` with ``value`` ONLY if it equals ``expected``.

        Returns ``True`` when swapped, ``False`` when the key is absent or holds
        something else. The primitive behind owner-checked state transitions
        (an idempotency reservation must only be finalised by its owner).
        Optional: drivers advertise it via the ``cas`` feature; the default
        raises ``NotImplementedError`` so callers can detect its absence.
        """
        raise NotImplementedError(
            f"cache driver {self.driver!r} does not support compare_and_set"
        )

    async def compare_and_delete(self, key: str, expected: str) -> bool:
        """Atomically delete ``key`` ONLY if it equals ``expected`` (``cas``)."""
        raise NotImplementedError(
            f"cache driver {self.driver!r} does not support compare_and_delete"
        )

    # ---- optional fused operations (one round trip where the driver can) ----
    # Each has a portable default composed from the core operations above, so
    # every driver supports them; a driver that can do better (one server round
    # trip) overrides them and names them in ``performance_hints``.

    async def incr_window(
        self, key: str, previous_key: str, *, ttl_seconds: int | None = None
    ) -> tuple[int, str | None]:
        """:meth:`incr` ``key`` and read ``previous_key`` together.

        Returns ``(new count of key, value of previous_key or None)`` — the
        sliding-window-counter rate limiter's per-request step. Hint
        ``incr_window`` = one round trip; the default is two operations.
        """
        count = await self.incr(key, ttl_seconds=ttl_seconds)
        return count, await self.get(previous_key)

    async def incr_many(
        self, keys: Sequence[str], *, ttl_seconds: int | None = None
    ) -> list[int]:
        """:meth:`incr` every key of ``keys`` (TTL set when a key is created).

        Returns the new counts in order. Hint ``incr_many`` = one round trip
        (atomic per key); the default increments the keys one by one.
        """
        return [await self.incr(key, ttl_seconds=ttl_seconds) for key in keys]

    async def set_nx_get(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> tuple[bool, str | None]:
        """Store ``value`` ONLY if ``key`` is absent, else return what is there.

        Returns ``(True, None)`` when this call created the key, ``(False,
        existing)`` when it already existed. ``existing`` may be ``None`` when
        the key vanished between the two steps of the portable default (expired
        / deleted concurrently) — callers simply retry. Hint ``set_nx_get``
        = one atomic round trip (Redis ``SET … NX GET``).
        """
        if await self.add(key, value, ttl_seconds=ttl_seconds):
            return True, None
        return False, await self.get(key)

    async def set_max(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        """Store ``value`` ONLY if it is greater than what ``key`` holds.

        A monotonic "set if greater": ``value`` is written when the key is
        absent, holds something that is not a plain decimal (a corrupt value
        is replaced), or holds a plain decimal strictly LOWER than ``value``.
        Values are plain non-negative decimals (``"17"``, ``"1700000000.25"``;
        :func:`parse_plain_decimal`) compared EXACTLY, never as floats — any
        other ``value`` raises ``ValueError``. Returns the value the key holds
        after the call: ``value`` when it was written, else the equal/greater
        one already there. ``ttl_seconds`` applies only when ``value`` is
        written; a winning existing value keeps its own TTL (never shortened,
        never extended).

        Never fails because of concurrent writers: a lost race means another
        writer changed the key, so the decision is simply re-taken. Hint
        ``set_max`` = one atomic round trip (a Lua script on Redis); the
        portable default is a read / ``add`` / ``compare_and_set`` loop
        (:func:`portable_set_max`). **Without the ``cas`` feature that default
        degrades to a plain ``set`` after the read — NOT monotonic**: a value
        a concurrent writer raised in between can be lowered. Callers for whom
        that is unacceptable (the token revocation mark) call
        ``portable_set_max(..., overwrite_without_cas=False)`` on such a
        driver and get ``NotImplementedError`` instead.
        """
        return await portable_set_max(self, key, value, ttl_seconds=ttl_seconds)

    async def set_subject_state(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> str:
        """Store a subject-state ``value`` ONLY if it ranks above what ``key``
        holds (ADR-0009 item 13: the one subject-state record).

        The value domain is :func:`parse_subject_state`: ``v1:after:<decimal>``
        or ``v1:blocked:<decimal>``. The order is total: every ``blocked``
        value ranks above every ``after`` value (a revocation never unblocks);
        within a kind the decimal instant decides, compared EXACTLY. A stored
        value outside the domain is corrupt and always replaced. Any other
        ``value`` raises ``ValueError``. Returns the value the key holds after
        the call (``value`` when written, else the equal/higher one there);
        ``ttl_seconds`` applies only when ``value`` is written.

        Hint ``set_subject_state`` = one atomic round trip (a Lua script on
        Redis); the portable default is a read / ``add`` / ``compare_and_set``
        loop (:func:`portable_set_subject_state`) and REFUSES
        (``NotImplementedError``) on a cache without ``compare_and_set``: a
        plain overwrite could unblock a subject or lower a revocation.
        """
        return await portable_set_subject_state(
            self, key, value, ttl_seconds=ttl_seconds
        )

    async def raw_client(self) -> Any:
        """Escape hatch: return the underlying client, if the driver has one.

        NOT part of the portable contract — only backends with a meaningful
        client object (e.g. Redis, for transport-level needs like pub/sub)
        override this. The default raises so portable code never depends on it.
        """
        raise NotImplementedError(
            f"cache driver {self.driver!r} does not expose a raw client"
        )


#: A plain non-negative decimal — the value domain of :meth:`CacheProvider.set_max`
#: (the Redis Lua script matches exactly the same shape).
_PLAIN_DECIMAL = re.compile(r"\s*([0-9]+)(?:\.([0-9]*))?\s*", re.ASCII)

#: Safety bound of :func:`portable_set_max`. Each lost round means ANOTHER
#: writer changed the key in between (monotonic writers only ever raise it),
#: so realistic contention ends in a few rounds; the bound only stops a
#: livelock against a non-monotonic writer (plain ``set`` of lower values).
SET_MAX_MAX_ROUNDS = 1000


class CacheContentionError(RuntimeError):
    """:func:`portable_set_max` lost :data:`SET_MAX_MAX_ROUNDS` races in a row."""


def parse_plain_decimal(raw: str | bytes | None) -> Decimal | None:
    """``raw`` as an exact :class:`~decimal.Decimal` when it is a plain
    non-negative decimal (``[0-9]+(.[0-9]*)?``, surrounding whitespace
    allowed), else ``None`` (absent / corrupt / signed / exponent form)."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "replace")
    match = _PLAIN_DECIMAL.fullmatch(raw)
    if match is None:
        return None
    whole, frac = match.group(1), match.group(2) or "0"
    return Decimal(f"{whole}.{frac}")


async def portable_set_max(
    provider: Any,
    key: str,
    value: str,
    *,
    ttl_seconds: int | None = None,
    overwrite_without_cas: bool = True,
) -> str:
    """Portable :meth:`CacheProvider.set_max` over ``get`` / ``add`` /
    ``compare_and_set`` (``set`` without ``cas``) — also usable on duck-typed
    cache doubles that implement only those operations.

    Retries for as long as the stored value is below ``value``: a failed
    ``add`` / ``compare_and_set`` means another writer changed the key, so the
    loop re-reads and either finds an equal/greater value (done) or tries
    again. Raises :class:`CacheContentionError` only after
    :data:`SET_MAX_MAX_ROUNDS` lost races in a row.

    On a cache without ``compare_and_set`` (``NotImplementedError``) it
    overwrites with a plain ``set`` — best effort, it can LOWER a value a
    concurrent writer raised in between — unless ``overwrite_without_cas`` is
    false, in which case the ``NotImplementedError`` propagates.
    """
    ours = parse_plain_decimal(value)
    if ours is None:
        raise ValueError(
            f"set_max value must be a plain non-negative decimal: {value!r}"
        )
    for _ in range(SET_MAX_MAX_ROUNDS):
        current = await provider.get(key)
        if current is None:
            if await provider.add(key, value, ttl_seconds=ttl_seconds):
                return value
            continue  # created concurrently: re-read and re-decide
        existing = parse_plain_decimal(current)
        if existing is not None and existing >= ours:
            return current if isinstance(current, str) else current.decode()
        try:
            swapped = await provider.compare_and_set(
                key, current, value, ttl_seconds=ttl_seconds
            )
        except NotImplementedError:
            if not overwrite_without_cas:
                raise
            # Driver without the ``cas`` feature: best-effort (non-monotonic)
            # overwrite.
            await provider.set(key, value, ttl_seconds=ttl_seconds)
            return value
        if swapped:
            return value
        # Lost the race: the key changed under us — re-read and re-decide.
    raise CacheContentionError(
        f"set_max on {key!r} lost {SET_MAX_MAX_ROUNDS} races in a row"
    )


#: The value domain of :meth:`CacheProvider.set_subject_state`.
_SUBJECT_STATE = re.compile(r"v1:(after|blocked):([0-9]+)(?:\.([0-9]*))?", re.ASCII)
#: Kind ranks of the subject-state order (``blocked`` above every ``after``).
SUBJECT_STATE_RANKS: Mapping[str, int] = {"after": 0, "blocked": 1}


def parse_subject_state(raw: str | bytes | None) -> tuple[str, Decimal] | None:
    """``(kind, instant)`` of a subject-state value, else ``None`` (absent or
    corrupt). ``kind`` is ``"after"`` or ``"blocked"``; ``instant`` is the
    exact decimal (unix seconds) the value carries."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "replace")
    match = _SUBJECT_STATE.fullmatch(raw)
    if match is None:
        return None
    kind, whole, frac = match.group(1), match.group(2), match.group(3) or "0"
    return kind, Decimal(f"{whole}.{frac}")


def _subject_rank(raw: str | bytes | None) -> tuple[int, Decimal] | None:
    parsed = parse_subject_state(raw)
    if parsed is None:
        return None
    return SUBJECT_STATE_RANKS[parsed[0]], parsed[1]


async def portable_set_subject_state(
    provider: Any, key: str, value: str, *, ttl_seconds: int | None = None
) -> str:
    """Portable :meth:`CacheProvider.set_subject_state` over ``get`` / ``add``
    / ``compare_and_set`` (also usable on duck-typed cache doubles).

    Like :func:`portable_set_max` with the subject-state order; a cache
    without ``compare_and_set`` raises ``NotImplementedError`` (never a plain
    overwrite: it could lift a block).
    """
    ours = _subject_rank(value)
    if ours is None:
        raise ValueError(f"not a subject-state value: {value!r}")
    for _ in range(SET_MAX_MAX_ROUNDS):
        current = await provider.get(key)
        if current is None:
            if await provider.add(key, value, ttl_seconds=ttl_seconds):
                return value
            continue
        existing = _subject_rank(current)
        if existing is not None and existing >= ours:
            return current if isinstance(current, str) else current.decode()
        if await provider.compare_and_set(key, current, value, ttl_seconds=ttl_seconds):
            return value
    raise CacheContentionError(
        f"set_subject_state on {key!r} lost {SET_MAX_MAX_ROUNDS} races in a row"
    )


CACHE_CAPABILITY = CapabilitySpec(
    name="cache",
    version="v1",
    provider_contract=CacheProvider,
    # ``get_many``, ``set_many`` and the fused operations (``incr_window``,
    # ``incr_many``, ``set_nx_get``, ``set_max``, ``set_subject_state``) are
    # required operations with portable defaults, not features: a driver that
    # does one in a single round trip says so in ``performance_hints``.
    feature_kinds={"cas": "behaviour"},
    description="Ephemeral key/value store (operation-based).",
)


__all__ = [
    "CACHE_CAPABILITY",
    "SET_MAX_MAX_ROUNDS",
    "SUBJECT_STATE_RANKS",
    "Cache",
    "CacheContentionError",
    "CacheProvider",
    "parse_plain_decimal",
    "parse_subject_state",
    "portable_set_max",
    "portable_set_subject_state",
]
