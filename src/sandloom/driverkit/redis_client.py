"""The process-wide Redis clients a built-in Redis driver shares with the cache.

The ``default`` ``cache`` instance and the ``default`` ``streams`` event bus
share one process-wide Redis client: the cache owns its lifecycle, and the bus
takes a bounded sibling of it (a blocking ``XREADGROUP`` must not trip the
cache's short socket timeout). The code stays in
``sandloom.core.drivers.redis_cache``; these functions call it by attribute at
each call, so a test that patches that module is honoured, and a driver imports
only the driver kit.

:func:`build_transport_client` builds a client of its own (a named instance);
:func:`configure_default_client` sets the options the default client is built
from (``replace=False``: never over what the default cache instance set);
:func:`get_default_transport_client` is the shared sibling.
"""

from __future__ import annotations

from typing import Any

import sandloom.core.drivers.redis_cache as _redis_cache
from sandloom.core.drivers.redis_cache import RedisCacheOptions


def build_transport_client(
    url: str,
    *,
    read_timeout: float | None = None,
    socket_connect_timeout_seconds: float = 0.5,
    health_check_interval_seconds: int = 30,
) -> Any:
    """A bounded client for event-bus transport reads, on its own pool."""
    return _redis_cache.build_transport_client(
        url,
        read_timeout=read_timeout,
        socket_connect_timeout_seconds=socket_connect_timeout_seconds,
        health_check_interval_seconds=health_check_interval_seconds,
    )


def configure_default_client(
    options: RedisCacheOptions, *, replace: bool = True
) -> None:
    """Set the options the process-wide ``default`` client is built from."""
    _redis_cache.configure_default_client(options, replace=replace)


async def get_default_transport_client(
    read_timeout: float | None = None, options: RedisCacheOptions | None = None
) -> Any:
    """The shared client of the default event-bus transport (blocking reads)."""
    return await _redis_cache.get_default_transport_client(read_timeout, options)


__all__ = [
    "RedisCacheOptions",
    "build_transport_client",
    "configure_default_client",
    "get_default_transport_client",
]
