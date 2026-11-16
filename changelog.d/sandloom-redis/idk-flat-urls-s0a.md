### Changed

- **A `redis` cache instance with no url is refused at startup, and the message names the INFRA option** (idk-flat-urls-s0a). `RedisCacheProvider.from_config` raises `MissingUrlError` (a `ValueError`, `fatal_at_startup`, so startup aborts in every environment) saying `redis cache instance 'default' has no url: set the INFRA option cache.default.url, e.g. INFRA='{...}'`; it no longer says the kernel fills the url from `CACHE_URL`. A provider built by hand (`create_redis_cache`) is unchanged.
