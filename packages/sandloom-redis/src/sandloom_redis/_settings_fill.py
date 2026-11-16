"""What the Redis drivers take from the flat settings (``InfraProvider.settings_fill``).

The kernel applies each declaration without naming the driver: an option an
instance leaves out takes the value of the setting named here, one an instance
states always wins (``INFRA`` over the flat setting).
"""

from __future__ import annotations

from types import MappingProxyType

from sandloom.driverkit import SettingsFill

#: ``option -> setting``: the bounds of a cache-shaped client (the ``CACHE_*``
#: settings): the cache and the atomic store.
CLIENT_BOUNDS = MappingProxyType(
    {
        "socket_timeout_seconds": "cache_socket_timeout_seconds",
        "socket_connect_timeout_seconds": "cache_socket_connect_timeout_seconds",
        "health_check_interval_seconds": "cache_health_check_interval_seconds",
        "max_connections": "cache_max_connections",
        "pool_timeout_seconds": "cache_pool_timeout_seconds",
        "retry_attempts": "cache_retry_attempts",
    }
)

#: ``option -> setting``: the bounds of a transport-shaped client (the
#: broadcast and the ``streams`` bus; its read timeout is its own).
TRANSPORT_BOUNDS = MappingProxyType(
    {
        "socket_connect_timeout_seconds": "cache_socket_connect_timeout_seconds",
        "health_check_interval_seconds": "cache_health_check_interval_seconds",
    }
)

_URL = MappingProxyType({"url": "cache_url"})

#: A ``redis`` cache: the url of ``CACHE_URL`` and the client bounds.
CACHE_FILL = SettingsFill(urls=_URL, bounds=CLIENT_BOUNDS)

#: A ``redis`` lease: the url of ``CACHE_URL`` (the lease shares the cache's
#: Redis, as the beat lease always has).
LEASE_FILL = SettingsFill(urls=_URL)

#: A ``redis`` broadcast: the url follows the resolved ``cache.default`` (an
#: ``INFRA`` override of its url included), else ``CACHE_URL``.
BROADCAST_FILL = SettingsFill(urls=_URL, follows="cache", bounds=TRANSPORT_BOUNDS)

#: A ``redis`` atomic store: the client bounds, and the url only on the
#: development instance the kernel synthesizes (it follows the resolved
#: ``cache.default``): the url of one ``INFRA`` declares is its own, never the
#: cache's.
ATOMIC_STORE_FILL = SettingsFill(
    urls=_URL, urls_for="implicit", follows="cache", bounds=CLIENT_BOUNDS
)

#: A ``streams`` bus: the url of ``CACHE_URL`` on the ``default`` instance (a
#: named bus without one shares the default transport) and the transport bounds.
STREAMS_FILL = SettingsFill(urls=_URL, urls_for="default", bounds=TRANSPORT_BOUNDS)
