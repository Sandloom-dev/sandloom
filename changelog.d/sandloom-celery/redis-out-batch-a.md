### Changed

- **The Celery task queue isolates `broker` and `backend` through `isolated_url_options`** (redis-out-harness). It replaces `isolated_redis_options`, which the kernel no longer exports, so the package needs the kernel that has `sandloom.driverkit.testing_isolation.isolated_url_options`. Upgrade the package with the kernel. Behaviour is the same: a worker's broker and backend urls move to its own database index when the Redis harness family is installed.
