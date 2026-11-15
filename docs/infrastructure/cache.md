---
title: Cache
description: The cache interface, its operations and the redis driver.
type: concept
---

# Cache

A cache holds fast, shared data and counters. Never write security state through `cache()`:
the framework keeps revocation marks and idempotency records in its own `atomic_store` instances (`INFRA.atomic_store.security`), not in the cache.

## What a module calls

```python
from sandloom.sdk.infra import cache

await cache().set("reports:last", "...", ttl_seconds=60)
```

`cache(name="default")` returns these operations:

- `get`, `set`, `add`, `delete`, `incr`, `get_many`, `set_many`;
- `compare_and_set`, `compare_and_delete`, behind the feature `cas`;
- `windowed_counter(key, previous_key, ttl_seconds=...)`: increments `key` and reads
  `previous_key`, the step of the sliding-window rate limiter. It is one round trip on Redis and
  two operations elsewhere.

The cache may evict a key before its TTL. The rate limiter and the consumer inbox fail open when
it raises an `InfraError`. Revocation, blocks and idempotency records live on
[`atomic_store`](atomic-store.md), not here.

## Drivers

### `redis`

Set `CACHE_URL`, or the option `url` on a named instance. It supports the features `cas`,
`get_many` and `set_many`.

**Limits.** It connects to a single Redis endpoint, with no built-in Sentinel or Cluster client.
Use a managed primary endpoint with failover, or a driver package.

For a second cache, add a named instance in [Configure backends](configure.md).
