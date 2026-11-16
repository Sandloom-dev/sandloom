"""The Redis half of the Sandloom test harness.

The ``redis`` harness family (:mod:`sandloom_redis.testing.family`, registered in
the entry-point group ``sandloom.testing``): the xdist isolation of Redis
database indexes, ownership of an index, listing and pruning stale ones. A test
that empties a Redis database it owns calls :func:`flush_owned_redis`::

    from sandloom_redis.testing import flush_owned_redis

    await flush_owned_redis(client)  # an asyncio Redis client of the test database

It raises ``TestResourceOwnershipError`` (``sandloom.driverkit.testing_family``)
when another run marked that database. Importing this package imports no Redis
client.
"""

from sandloom_redis.testing.ownership import flush_owned_redis

__all__ = ["flush_owned_redis"]
