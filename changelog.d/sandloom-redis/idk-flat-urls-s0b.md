### Changed

- **The Redis test family isolates `INFRA` urls per worker** (idk-flat-urls-s0b). It moves the `url`, `broker` and `backend` of every `INFRA` instance per xdist worker (the Celery task queue's `broker` included), with or without `CACHE_URL` / `BROKER_URL`, and claims and allocates the indexes of those urls.
