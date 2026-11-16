### Added

- **A startup warning when the broker is the cache's Redis database** (idk-flat-urls-no-follows). `CeleryTaskQueueProvider.aopen` logs one WARNING (through `sandloom_redis.shared_cache`, when `sandloom-redis` is installed) when the `broker` of an instance is the `host:port/db` of `cache.default`: a cache that evicts can drop queued tasks. The log names both instances and never carries the broker's password; nothing is refused.
