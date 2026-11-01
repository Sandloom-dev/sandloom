---
title: Cache
description: The cache interface, its operations and the redis driver.
type: concept
---

# Cache

A cache holds fast, shared data and counters. Never write security state through `cache()`:
the framework keeps revocation marks and idempotency records in its own security cache instance.

## What a module calls

```python
from sandloom.sdk.infra import cache

await cache().set("reports:last", "...", ttl_seconds=60)
```

`cache(name="default")` returns these operations:

- `get`, `set`, `add`, `delete`, `incr`;
- `compare_and_set`, `compare_and_delete`, behind the feature `cas`;
- `incr_window`, `incr_many`, `set_nx_get`, `set_max`: one round trip on Redis and a portable
  fallback elsewhere.

## Drivers

### `redis`

Set `CACHE_URL`, or the option `url` on a named instance. It supports the features `cas`,
`get_many` and `set_many`.

**Limits.** It connects to a single Redis endpoint, with no built-in Sentinel or Cluster client.
Use a managed primary endpoint with failover, or a driver package.

For a second cache, add a named instance in [Configure backends](configure.md).
