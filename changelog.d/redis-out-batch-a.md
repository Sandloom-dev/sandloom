### Added

- **Three hooks on `HarnessFamily`** (redis-out-harness). A harness family now also says which URLs it owns (`owns_url(url)`), whether two of its URLs name the same database (`same_database(stated, base)`) and which environment a parallel run needs (`parallel_env()`). `sandloom.driverkit.testing_isolation` gains `set_url_mover(mover)`: a family installs one function `(url, worker number) -> url`, and `isolated_url_options` applies it to each URL option of a worker `gw<N>`. With no mover installed the options are unchanged.

### Changed

- **The test harness names no backend** (redis-out-harness). The worker rewrite, the claim of a slot's resources, the same-database check of an override and the xdist environment of `sandloom test` read the installed harness families, not a Redis url form. A `SANDLOOM_TEST_CACHE_URL` or `SANDLOOM_TEST_BROKER_URL` that no installed family owns is refused with `UnmatchedOverrideError`, as before for a URL that matches no INFRA instance. The message now reads "INFRA declares no URL of an installed backend". Upgrade steps: none for an app on the standard stack.
- **The `sandloom_cache` and `real_uow_client` fixtures take the cache driver from the app or from `INFRA`** (`cache.default.driver`), not from the name `redis`. A project that configures no cache driver fails with a `RuntimeError` that says so.

### Breaking changes

- **`sandloom.driverkit.testing_isolation.isolated_redis_options`, `WorkerLayout`, `DEFAULT_STRIDE`, `set_worker_layout` and `worker_layout` are removed** (redis-out-harness). A driver package calls `isolated_url_options(options, worker_id, keys)` instead, and a harness family installs its layout with `set_url_mover`; the layout of a Redis index per worker now lives in `sandloom_redis.testing.isolation`. No alias (ADR-0010).
- **`sandloom.core.testing.workers.REDIS_VARS` is now `URL_VARS`, and `Slot.redis_urls` is now `Slot.family_urls`** (redis-out-harness). Upgrade: rename both in a test helper that reads them. `SANDLOOM_TEST_REDIS_DB_STRIDE` keeps its name: it is the Redis family's own variable.
