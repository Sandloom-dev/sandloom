### Added

- **A startup warning when the broker is the cache's Redis database** (idk-flat-urls-no-follows). `CeleryTaskQueueProvider.aopen` registers the broker's `host:port/db` in `sandloom.driverkit.shared_endpoints` and logs one WARNING when it is the endpoint of `cache.default`: a cache that evicts can drop queued tasks. The log names both instances and never carries the broker's password; nothing is refused, and the package imports no other driver package. A queue the kernel synthesized is logged at INFO. `aclose` unregisters the queue.
