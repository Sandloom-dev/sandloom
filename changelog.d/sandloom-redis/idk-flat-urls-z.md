### Breaking changes

- **The Redis drivers take no url and no client bound from a setting** (idk-flat-urls-z). The `redis` cache, `atomic_store`, `lease` and `broadcast` drivers and the `streams` event bus read their `url` and their bounds from the options of their instance, and an instance with no `url` is refused (`MissingUrlError`). `CACHE_URL` and the `CACHE_*` bounds no longer exist as settings (the kernel refuses them), and the drivers' `settings_fill` is empty. The bounds keep the defaults the settings had. The `redis` harness family no longer rewrites a flat variable: it moves the database index of each instance `url`, and of the Celery `broker` through the kernel layout. Upgrade: [Upgrading to 0.7.0a14](docs/operate/upgrade.md).

### Added

- **The `redis` atomic store moves per xdist worker** (idk-flat-urls-z). `RedisAtomicStoreProvider.test_isolation` rewrites the store `url` to the worker's own Redis database through the same driverkit helper the cache uses, so the security store of a worker stays on the worker's cache database (the testing fixtures write revocation and block marks through the cache, the app reads them from the store). A test app whose security store shares its cache url keeps them equal under `-n`.
