"""The cache guard's store: the idempotency cache and its atomic key operations.

Every operation reaches the cache through the operation-based
``CacheProvider`` (never a raw client) and raises :class:`_CacheUnavailable`
on a cache error, so the caller decides whether to fail open or closed. Also
the ``pending`` record format a reservation writes.
"""

from __future__ import annotations

import logging
from typing import cast
from uuid import uuid4

import orjson

from sandloom.core.idempotency.fingerprint import _CacheUnavailable
from sandloom.core.infra.access import cache, idempotency_cache_name
from sandloom.core.infra.contracts import CacheProvider

logger = logging.getLogger("sandloom.core.idempotency")


def _pending_record(fingerprint: str, owner: str | None = None) -> str:
    return orjson.dumps(
        {"state": "pending", "fp": fingerprint, "owner": owner or uuid4().hex}
    ).decode()


def _idempotency_cache() -> CacheProvider:
    """The idempotency cache instance (``IDEMPOTENCY_CACHE_INSTANCE``, else
    ``SECURITY_CACHE_INSTANCE``).

    Resolved through this module's ``cache`` name so tests that substitute it
    keep working; the ``default`` instance is ``cache()`` exactly as before.
    """
    name = idempotency_cache_name()
    return cast("CacheProvider", cache() if name == "default" else cache(name))


async def _store_reserve(
    full_key: str, value: str, ttl: int
) -> tuple[bool, str | None]:
    """SET NX that also returns the existing value: ``(created, existing)``.

    One round trip on drivers with the ``set_nx_get`` op; duck-typed
    providers without it use SET NX then GET.
    """
    try:
        provider = _idempotency_cache()
        set_nx_get = getattr(provider, "set_nx_get", None)
        if set_nx_get is not None:
            created, existing = await set_nx_get(full_key, value, ttl_seconds=ttl)
            return bool(created), existing
        if await provider.add(full_key, value, ttl_seconds=ttl):
            return True, None
        return False, await provider.get(full_key)
    except Exception as exc:
        raise _CacheUnavailable(full_key) from exc


async def _store_add(full_key: str, value: str, ttl: int) -> bool:
    """Atomically create the key (SET NX). True if newly created."""
    try:
        return await _idempotency_cache().add(full_key, value, ttl_seconds=ttl)
    except Exception as exc:
        raise _CacheUnavailable(full_key) from exc


async def _store_cas_delete(full_key: str, expected: str) -> bool:
    """Delete ``full_key`` only if it still holds ``expected``.

    Uses the cache's atomic ``compare_and_delete``; a driver without the
    ``cas`` feature degrades to a (racy) read-compare-delete, logged once.
    Raises :class:`_CacheUnavailable` on a cache error.
    """
    provider = _idempotency_cache()
    try:
        try:
            return await provider.compare_and_delete(full_key, expected)
        except NotImplementedError:
            _warn_no_cas()
            if await provider.get(full_key) != expected:
                return False
            await provider.delete(full_key)
            return True
    except Exception as exc:
        raise _CacheUnavailable(full_key) from exc


async def _store_cas_set(full_key: str, expected: str, value: str, ttl: int) -> bool:
    """Replace ``full_key`` with ``value`` only if it still holds ``expected``."""
    provider = _idempotency_cache()
    try:
        try:
            return await provider.compare_and_set(
                full_key, expected, value, ttl_seconds=ttl
            )
        except NotImplementedError:
            _warn_no_cas()
            if await provider.get(full_key) != expected:
                return False
            await provider.set(full_key, value, ttl_seconds=ttl)
            return True
    except Exception as exc:
        raise _CacheUnavailable(full_key) from exc


_no_cas_warned = False


def _warn_no_cas() -> None:
    global _no_cas_warned
    if not _no_cas_warned:
        _no_cas_warned = True
        logger.warning(
            "Cache driver has no compare-and-set ('cas' feature); idempotency "
            "key transitions fall back to non-atomic read-compare-write"
        )
