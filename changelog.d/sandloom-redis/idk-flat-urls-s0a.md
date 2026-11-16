### Changed

- **A `redis` cache instance with no url is refused at startup, and the message names the INFRA option** (idk-flat-urls-s0a). Opening the instance (`RedisCacheProvider.aopen`, run by the registry at startup) raises `MissingUrlError` (a `ValueError`, `fatal_at_startup`, so startup aborts in every environment) saying `redis cache instance 'default' has no url: set the INFRA option cache.default.url, e.g. INFRA='{...}'`; it no longer says the kernel fills the url from `CACHE_URL`. Building a provider and a provider built by hand (`create_redis_cache`) are unchanged.
