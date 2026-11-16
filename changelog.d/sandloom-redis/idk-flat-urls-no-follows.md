### Breaking changes

- **A `redis` broadcast and the synthesized development `atomic_store` no longer follow the url of `cache.default`** (idk-flat-urls-no-follows). Before, a `cache.default` url stated in `INFRA` was also the url of a broadcast that left its own out, and of the development `security` store the kernel synthesizes; now each takes `CACHE_URL` (while it exists) or the url it states itself. Upgrade: when `cache.default` has its url in `INFRA` only (no `CACHE_URL`), set `broadcast.<instance>.url` and `atomic_store.security.url` explicitly, or the instance is refused at startup (`MissingUrlError`).

### Added

- **A startup warning when a persistent instance shares the cache's Redis database** (idk-flat-urls-no-follows). A `redis` `atomic_store`, a `redis` `lease` or a `streams` event bus whose `host:port/db` is the one of `cache.default` logs one WARNING naming both instances and the endpoint (never a password): a cache that evicts can drop the keys they must keep. It refuses nothing, and does not depend on the order the instances start in. The check is `sandloom_redis.shared_cache`.
