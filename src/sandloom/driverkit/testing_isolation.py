"""Parallel test isolation for a driver that talks to Redis.

A driver states what one pytest-xdist worker's instance uses through the
classmethod ``test_isolation(options, worker_id) -> options`` of its provider
(``InfraProvider``; the default returns the options unchanged, right for
``memory``). A driver whose options carry a Redis URL returns them with the
database index moved for the worker::

    from sandloom.driverkit.testing_isolation import isolated_redis_options

    class MyRedisProvider(CacheProvider):
        @classmethod
        def test_isolation(cls, options, worker_id):
            return isolated_redis_options(options, worker_id, keys=("url",))

``keys`` names the options holding a Redis URL (``("broker", "backend")`` for a
task queue). A ``worker_id`` that is not ``gw<N>`` returns the options as given.

The layout (how far each worker's index moves, the shared-Redis ``auto``
allocation) is the test harness's: it computes it from its own configuration
and hands it over with :func:`set_worker_layout` before it calls a hook, so a
driver reads no environment. The harness calls the hook of every configured
instance of a driver; a driver package needs no pytest plugin for it.
"""

from sandloom.core.infra.ports._base import (
    DEFAULT_STRIDE,
    WorkerLayout,
    isolated_redis_options,
    set_worker_layout,
    worker_layout,
)

__all__ = [
    "DEFAULT_STRIDE",
    "WorkerLayout",
    "isolated_redis_options",
    "set_worker_layout",
    "worker_layout",
]
