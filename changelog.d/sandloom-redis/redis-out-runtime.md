### Added

- **The Redis drivers carry their own backup advice and shutdown** (redis-out-runtime). The cache, the atomic store and the Streams bus implement `backup_hint` (AOF, `noeviction`, the loss notes of the cache and the streams) and the cache and the Streams bus implement `aclose_shared`, which closes the shared client pools. This replaces what the kernel did by testing for `redis://` urls.

### Changed

- **An `atomic_store` instance on its own Redis server gets the `noeviction` step in the plan.** It is any `atomic_store` now, where the kernel gave it to the `security` one only.
