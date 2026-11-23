### Fixed

- **The test-harness family reports a Redis that does not answer as `Unavailable`, naming the server** (dx-fresh-app-tests). `allocate_workers` let redis-py's `ConnectionError` escape, which ended `sandloom test` on a machine with no Redis in an INTERNALERROR. A refused or lost connection, a timeout or a socket error now raises `Unavailable` (capability and instance of the `INFRA` entry that names the server, the message with `scheme://host:port` and no credential). Any other error, a `ResponseError` included, keeps its type.
