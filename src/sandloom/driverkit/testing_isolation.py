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

The harness applies the same layout to ``CACHE_URL`` / ``BROKER_URL`` before any
setting is read (:mod:`sandloom.core.testing.redis_isolation`, also the layout of
the shared-Redis ``auto`` mode, which only the harness knows).
"""

from sandloom.core.drivers.redis_cache import (
    TEST_WORKER_DB_STRIDE,
    isolated_redis_options,
)

__all__ = ["TEST_WORKER_DB_STRIDE", "isolated_redis_options"]
