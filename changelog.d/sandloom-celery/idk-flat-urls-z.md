### Breaking changes

- **The `celery` task queue driver fills only `health_timeout_seconds` from a setting** (idk-flat-urls-z). `BROKER_URL` and `CACHE_URL` are removed settings: the broker is the `broker` option of the `taskqueue` instance and the result backend is the `backend` option, off unless stated. The driver's `settings_fill` keeps the readiness probe's bound (`HEALTH_CHECK_TIMEOUT_SECONDS` into `health_timeout_seconds`, as cap and bound). Upgrade: [Upgrading to 0.7.0a14](docs/operate/upgrade.md).
