"""The cache guard's store: the idempotency ``atomic_store``'s key operations.

Every operation reaches the ``idempotency`` ``atomic_store`` instance (the
``security`` one when none is configured, see ``infra.access.atomic_store``)
and raises :class:`_CacheUnavailable` on a store error, so the caller decides
whether to fail open or closed. Also the ``pending`` record format a
reservation writes.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import orjson

from sandloom.core.idempotency.fingerprint import _CacheUnavailable
from sandloom.core.infra.access import atomic_store
from sandloom.core.infra.ports.atomic_store import AtomicStore

logger = logging.getLogger("sandloom.core.idempotency")


def _pending_record(fingerprint: str, owner: str | None = None) -> str:
    return orjson.dumps(
        {"state": "pending", "fp": fingerprint, "owner": owner or uuid4().hex}
    ).decode()


def _idempotency_store() -> AtomicStore:
    """The ``idempotency`` ``atomic_store`` instance, resolved through this
    module's ``atomic_store`` name so tests that substitute it keep working."""
    return atomic_store("idempotency")


async def _store_reserve(
    full_key: str, value: str, ttl: int
) -> tuple[bool, str | None]:
    """``add_or_get``: ``(created, existing)`` in one round trip."""
    try:
        created, existing = await _idempotency_store().add_or_get(
            full_key, value, ttl_seconds=ttl
        )
        return created, existing
    except Exception as exc:
        raise _CacheUnavailable(full_key) from exc


async def _store_add(full_key: str, value: str, ttl: int) -> bool:
    """Atomically create the key. True if newly created."""
    try:
        return await _idempotency_store().add(full_key, value, ttl_seconds=ttl)
    except Exception as exc:
        raise _CacheUnavailable(full_key) from exc


async def _store_cas_delete(full_key: str, expected: str) -> bool:
    """Delete ``full_key`` only if it still holds ``expected`` (the store's
    atomic ``compare_and_delete``). Raises :class:`_CacheUnavailable` on a
    store error."""
    try:
        return await _idempotency_store().compare_and_delete(full_key, expected)
    except Exception as exc:
        raise _CacheUnavailable(full_key) from exc


async def _store_cas_set(full_key: str, expected: str, value: str, ttl: int) -> bool:
    """Replace ``full_key`` with ``value`` only if it still holds ``expected``."""
    try:
        return await _idempotency_store().compare_and_set(
            full_key, expected, value, ttl_seconds=ttl
        )
    except Exception as exc:
        raise _CacheUnavailable(full_key) from exc
