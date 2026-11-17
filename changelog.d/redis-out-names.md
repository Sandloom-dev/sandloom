### Breaking changes

- **The volatile event bus names say `broadcast`, not `redis`** (redis-out-names). `EVENT_BUS_REDIS_SEMAPHORE_LIMIT` is now `EVENT_BUS_BROADCAST_SEMAPHORE_LIMIT` (same meaning, default `100`); startup fails while the old name is set, and the error names the new one. The new setting refuses a value below 1 at startup (`0` used to block every callback). The event bus counter `redis_reconnect_attempts` in `/diagnostics` is `broadcast_reconnect_attempts`, and the Prometheus counter `sandloom_redis_reconnect_attempts_total` is `sandloom_broadcast_reconnect_attempts_total`; the bundled Grafana dashboard queries the new name, and the counter now moves with every lost subscription (the old one was never incremented). The old names have no alias. The text of `SANDLOOM-R014` names a backing service instead of PostgreSQL and Redis; its fix is unchanged. See [Upgrading to 0.7.0a15](https://sandloom.dev/docs/operate/upgrade/#upgrading-to-070a15-the-volatile-event-bus-names-say-broadcast).

### Fixed

- **A removed setting in a lower-case environment variable is refused too** (redis-out-names). Setting names are case-insensitive, but the removed-setting check read the process environment by exact upper-case name only, so `cache_url=...` or `event_bus_redis_semaphore_limit=5` started the app without the refusal. Every removed setting is now found under either spelling.
