### Added

- **`InfraProvider.backup_hint` and `InfraProvider.aclose_shared`** (redis-out-runtime). A driver package now says what its server needs to be backed up and restored (`backup_hint`, returning a `BackupHint`, exported from `sandloom.driverkit`) and releases the connection pools it shares between instances at shutdown (`aclose_shared`). Both have a neutral default, so a driver that implements neither behaves as before. `InfraRegistry.aclose_shared()` calls every loaded provider's hook on shutdown, logs a failure as a warning and never raises.

### Changed

- **The kernel runtime names no Redis** (redis-out-runtime). `sandloom backup plan`, the production profile, the task relay, the unreachable-service message and the shutdown of the default cache no longer test for `redis`, `kombu` or a `redis://` url: the Redis and Celery packages carry that knowledge. The plan for the standard stack says the same things as before. Wording changes: the plan and the unreachable message name the capability ("the cache backend") where they named "Redis", and the refusals of the production profile say "the same server" for "the same Redis server". Upgrade steps: none for an app on the standard stack.
- **The same-server check of the production profile is stricter for urls without a port.** Two urls of one host, scheme (or its `s` variant) and no port on one side are now the same server, as a missing port is a wildcard. The refusal that follows is the one already documented.
- **The relay classifies an outage by the error the driver mapped, not by client class names or reply words.** A driver maps its transient errors to `Unavailable`; a poison error stays a poison error. An unmapped raw Redis or kombu error is no longer recognised as an outage by the kernel.
- **A driver that is not installed, or an instance not filled, is a critical `backing servers` warning in the backup plan** instead of a listing of its url.
