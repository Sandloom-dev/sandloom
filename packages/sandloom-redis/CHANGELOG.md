# Changelog — sandloom-redis

## [Unreleased]

- **An unknown Redis reply is no longer an outage: the five drivers now share one classifier (`sandloom_redis._errors`).** A connection error, a socket `OSError` and the transient replies `LOADING`, `READONLY`, `MASTERDOWN`, `TRYAGAIN`, `CLUSTERDOWN`, `BUSY` and `OOM` map to `Unavailable`; a timeout to `Timeout`; `AuthenticationError`, `NoPermissionError` and `NOAUTH` / `WRONGPASS` / `NOPERM` to `PermissionDenied`; any other `ResponseError` (an unknown `ERR ...`: an oversized value, a script error) and a client `DataError` to `InvalidArgument`, which is not retryable. Before, the cache, atomic store, lease and broadcast drivers mapped every such reply to `Unavailable`, so the outbox relay (which trusts the mapped class) parked the row for ever as an outage instead of spending an attempt and dead-lettering it. The lease no longer reads a transient reply to `CONFIG GET` as the server refusing the read. The message names the error type only.
- The Streams batch publish chains its `PartialPublish` from the MAPPED error, with the raw Redis error as that error's `__cause__` (it chained the raw error), so the relay judges the batch path like the per-row path: a `PermissionDenied` batch is an outage, an unknown `ERR` reply is not.

- Add the Redis drivers `atomic_store.redis`, `lease.redis` and `broadcast.redis` as an optional package. They left the kernel wheel; `sandloom[redis]` installs this package.
- Add the Streams event bus driver `eventbus.streams` (`sandloom_redis.eventbus.driver`, with its transport in `sandloom_redis.eventbus.streams_*`), moved from the kernel wheel. Its `isolation_identity()` is `streams:<run_id>` of the reached server.
- Add the Redis cache driver `cache.redis` (`sandloom_redis.cache`), moved from the kernel wheel. Its `health()` also runs a no-op script, so a server that refuses scripting reports `fail`.
