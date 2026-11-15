# Changelog — sandloom-redis

## [Unreleased]

- Add the Redis drivers `atomic_store.redis`, `lease.redis` and `broadcast.redis` as an optional package. They left the kernel wheel; `sandloom[redis]` installs this package.
- Add the Streams event bus driver `eventbus.streams` (`sandloom_redis.eventbus.driver`, with its transport in `sandloom_redis.eventbus.streams_*`), moved from the kernel wheel. Its `isolation_identity()` is `streams:<run_id>` of the reached server.
- Add the Redis cache driver `cache.redis` (`sandloom_redis.cache`), moved from the kernel wheel. Its `health()` also runs a no-op script, so a server that refuses scripting reports `fail`.
