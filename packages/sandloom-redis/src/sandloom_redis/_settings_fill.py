"""What the Redis drivers take from the flat settings (``InfraProvider.settings_fill``).

The kernel applies each declaration without naming the driver: an option an
instance leaves out takes the value of the setting named here, one an instance
states always wins (``INFRA`` over the flat setting).

``CACHE_URL`` is an optional setting: unset, nothing is filled, and an instance
that still has no url refuses startup naming its own INFRA option
(``cache.<instance>.url``). The url options are configured through ``INFRA``.
"""

from __future__ import annotations

import json
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


class MissingUrlError(ValueError):
    """A ``redis`` instance has no ``url``: a configuration mistake that aborts
    startup in every environment (``fatal_at_startup``), never a transient fault.

    Fatal everywhere because a non-strict (development) startup only logs any
    other failure and goes on, and the auth revocation and block checks then
    read the unreachable store as "not revoked": a missing url must never
    reach that path.
    """

    fatal_at_startup = True


def require_url(
    capability: str,
    instance: str,
    url: str | None,
    *,
    fallback: str | None = None,
) -> str:
    """The ``url``, or refuse startup (:class:`MissingUrlError`) when it is empty.

    The message names the INFRA option to set (``<capability>.<instance>.url``)
    with an example. ``fallback`` says what else the instance would have used
    and found empty too (``None``: it has no fallback). Every Redis driver calls
    this from its ``aopen``, so ``InfraRegistry.aopen_all`` reports the mistake
    before the first connection.
    """
    if url:
        return url
    example = {capability: {instance: {"url": "redis://localhost:6379/0"}}}
    tail = f" ({fallback} has none either)" if fallback else ""
    raise MissingUrlError(
        f"redis {capability} instance {instance!r} has no url{tail}: set the "
        f"INFRA option {capability}.{instance}.url, e.g. "
        f"INFRA='{json.dumps(example)}'"
    )
