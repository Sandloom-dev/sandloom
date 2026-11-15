---
title: Cache
description: The cache contract (major 2), its operations, the redis and memory drivers, and what fails open.
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

`cache(name="default")` returns the operations of the cache contract, major 2:

- `get`, `set`, `delete`, `add`, `incr`, `get_many`, `set_many`;
- `windowed_counter(key, previous_key, ttl_seconds=...)`;
- `compare_and_set` and `compare_and_delete`, behind the feature `cas`.

Values are UTF-8 strings. A key is at most 1024 bytes. A TTL is a whole number of seconds, 1 or
more. `None` means no expiry. A key is not readable after its TTL.

Each operation raises only an `InfraError`:

- `InvalidArgument` for a bad key, a TTL below 1, or an `incr` on a value that is not an integer;
- `TooLarge` for a value over the driver's limit;
- `Unavailable` or `Timeout` when the backend fails.

### Atomic operations

- `add` stores a value only when the key is absent. Of concurrent calls for one key, exactly one
  answers `True`.
- `incr` is atomic. It sets the TTL in the same step when it creates the key, and the result is `1`.
- `get_many` and `set_many` keep the order of their arguments. They are not atomic across keys.

### `windowed_counter`

`windowed_counter` runs `incr(key)`, then reads `previous_key`. It answers the new count and the
previous value, or `None`. It is the per-request step of the sliding-window rate limiter.

A driver can do both in one round trip. It states that with the performance hint
`one_round_trip`, as `redis` does for `windowed_counter`, `get_many` and `set_many`. A driver without
the hint runs the two operations.

### The feature `cas`

`compare_and_set(key, expected, value)` swaps the value only when the key holds `expected`. An absent
key answers `False`. With `ttl_seconds=None` the key loses its earlier TTL.
`compare_and_delete(key, expected)` deletes only on equality.

A driver that does not declare `cas` raises `Unsupported("cas")` from both calls. Ask for the
feature in a module's requirements before you call them.

## What may be lost

The cache may evict a key before its TTL, and a restart may empty it. Nothing a guarantee depends
on lives here.

Two kernel users fail open by contract:

- The HTTP rate limiter (G-HTTP-02) treats an `InfraError` from `windowed_counter` as "no counter".
  It serves the request.
- The cache-backed consumer inbox (G-INBOX-03) treats an `InfraError` as "not seen". It handles the
  event, so the effect stays at-least-once.

A cache outage therefore costs a limit or a dedupe hint, never availability. Revocation, blocks and
idempotency records live on [`atomic_store`](atomic-store.md), not here.

## Drivers

| Driver | Tier | Use it for |
| --- | --- | --- |
| `redis` | `beta` | Production. |
| `memory` | `beta`, `development_only` | Development and tests. |

### `redis`

Set `CACHE_URL`, or the option `url` on a named instance. It declares the feature `cas`. It does
`get_many`, `set_many` and `windowed_counter` in one round trip.

**Limits.** It connects to a single Redis endpoint, with no built-in Sentinel or Cluster client.
Use a managed primary endpoint with failover, or a driver package.

### `memory`

An in-process cache for development and tests. It keeps the whole contract and declares `cas`. Its
guarantees are weaker than those of `redis`:

- **Per process, not shared.** Each process holds its own keys. A worker does not see what the app
  wrote. A rate limit counts per process.
- **Lost on restart.** A stop empties the store.
- **No isolation between processes.** There is no common store that two instances could share.
  `isolation_identity` names the process.

Production refuses it: startup fails with SANDLOOM-B022, and no accepted risk lifts it. Sandloom
never picks it on its own. It is not a fallback for an unreachable Redis. The default cache stays
`redis`, whatever the profile.

The option `max_value_bytes` (default 1 MiB) sets the limit that raises `TooLarge`.

#### Use it in tests

Name the driver in `INFRA`:

```json
{"cache": {"default": {"driver": "memory"}}}
```

Then the default cache is an in-process one. A unit test of module code that only calls `cache()`
can also use `InMemoryCache` from `sandloom.sdk.testing`.

For a second cache, add a named instance in [Configure backends](configure.md).

## Write a driver

A driver package subclasses `CacheProvider` from `sandloom.driverkit` and states
`contract_version = 2`. It runs `CacheConformance` from `sandloom.driverkit.testing`. The kit
checks the round trip, the TTL, concurrent `add` and `incr`, `windowed_counter` and the `cas` group.
See [Write a driver](write-a-driver.md).
