# Sandloom Redis drivers

Install this package alongside the matching Sandloom release: `pip install
'sandloom[redis]'` brings it in, and a checkout runs `uv pip install -e
packages/sandloom -e packages/sandloom-redis` from the repository root. Sandloom
discovers the drivers through the `sandloom.drivers` entry-point group.

| Entry point | Capability | Provider |
| --- | --- | --- |
| `atomic_store.redis` | `atomic_store` | `sandloom_redis.atomic_store:RedisAtomicStoreProvider` |
| `lease.redis` | `lease` | `sandloom_redis.lease:RedisLeaseProvider` |
| `broadcast.redis` | `broadcast` | `sandloom_redis.broadcast:RedisBroadcastProvider` |
| `cache.redis` | `cache` | `sandloom_redis.cache:RedisCacheProvider` |

Each driver declares the tier `beta`. The `redis-drivers` job of `ci.yml`
(required by the aggregate check "CI required") runs the conformance kits
(`AtomicStoreConformance`, `LeaseConformance`, `BroadcastConformance`,
`CacheConformance`) against
a real Redis, and this package's tests and types (`mypy` strict).

```sh
INFRA='{"lease":{"beat":{"driver":"redis"}},"atomic_store":{"security":{"driver":"redis","url":"redis://cache:6379/0"}}}'
```

The `atomic_store` and `lease` drivers refuse a Redis whose `maxmemory-policy`
is not `noeviction`. Where a managed service refuses `CONFIG GET`, the option
`never_evicts_check` set to `assume` lets the operator vouch for the policy; in
production that needs the accepted risk `unverified_noeviction`.

Run the package tests from the repository root against a Redis with enough
databases for the xdist workers (`redis-server --databases 256`):

```sh
SANDLOOM_TEST_CACHE_URL=redis://127.0.0.1:6380/0 \
SANDLOOM_TEST_BROKER_URL=redis://127.0.0.1:6380/1 \
uv run pytest -n 2 --dist loadfile packages/sandloom-redis/tests
```
