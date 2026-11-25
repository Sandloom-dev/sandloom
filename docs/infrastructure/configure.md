---
title: Configure backends
description: The composition selects the drivers; INFRA gives each instance its own url, adds named instances, overrides drivers and sets options.
type: concept
---

# Configure backends

The kernel names no driver for the database, cache, event bus, task queue, broadcast, lease <!-- prose-ok: P6 a list of drivers and settings -->
or atomic store. The composition selects them. `sandloom_standard` picks SQLAlchemy, Redis,
Streams and Celery. An app made by `sandloom new` lists its picks in `DEFAULT_INFRA`
(`<app>/__init__.py`), which it passes to `create_app(default_infra=...)`.

The flat settings give the database, object store and notifier their options: `DATABASE_URL`, <!-- prose-ok: P6 a list of settings -->
`STORAGE_*` and `NOTIFIER_BACKEND`. `EVENT_BUS_BACKEND` names the bus
(`memory`, `broadcast` or `streams`); unset, it leaves the bus to the composition.
`STORAGE_TYPE` and `NOTIFIER_BACKEND` work the same way. Unset, the driver the composition
or `INFRA` selects stands with its options: an `s3` store keeps its `bucket`, an `smtp`
notifier its `host`. With none selected, the object store is `local` and the notifier is
`log`. Set, the flat setting replaces the composition's choice, and the startup log warns
that the choice's options are dropped. `INFRA` wins over an explicit flat setting and
warns that the flat setting is ignored.

Every Redis instance and the Celery broker takes its own explicit url in `INFRA`: <!-- prose-ok: P6 a list of instances -->
`cache.default.url`, `eventbus.default.url` (streams), `lease.beat.url`,
`atomic_store.security.url`, `broadcast.default.url` and `taskqueue.default.broker`. No
instance inherits another's url, and an instance that needs a url and has none refuses
startup. An entry may omit `driver`: the composition's driver is kept.

```sh
INFRA='{"cache":{"default":{"url":"redis://cache-redis:6379/0"}},
        "atomic_store":{"security":{"url":"redis://security-redis:6379/0"}},
        "taskqueue":{"default":{"broker":"redis://broker-redis:6379/1"}}}'
```

Keep the security instances (`atomic_store.security`, `lease.beat`, the event bus, the task
queue broker) off the cache's Redis: a cache may evict keys under memory pressure. When one
shares `cache.default`'s host, port and database, startup logs a warning that names both
instances.

Nothing falls back to `memory`. Some instances are required: the database, the cache, <!-- prose-ok: P6 a list of required instances -->
the event bus and the `security` atomic store. When neither the composition nor `INFRA`
selects a driver for one, startup refuses with SANDLOOM-B037, in every environment. The
message lists each missing instance, what to select and what to install. A selected driver
whose package is missing is refused the same way, with the install command
(`pip install sandloom-redis`, `pip install sandloom-celery`). Development names `memory`
itself, for example `EVENT_BUS_BACKEND=memory` in `.env`. In production the `security`
atomic store comes from `INFRA` only, never from a default.

The composition root (`create_app`, the worker, `sandloom migrate`, the test
harness) builds the registry. There is no implicit default registry:
`cache()`, `notifier()` or any accessor with none bound raises, in every
environment, with a hint naming those roots.

The `INFRA` setting (JSON) adds named instances
or overrides drivers and options:

```json
{"database": {"analytics": {"driver": "sqlalchemy",
                            "url": "postgresql+asyncpg://.../analytics"}},
 "cache":    {"sessions":  {"driver": "redis", "url": "redis://.../3"}}}
```

`NOTIFIER_BACKEND` accepts only the drivers Sandloom ships; a driver package is
selected through `INFRA`. Keeping the driver merges new options over the old ones,
and naming another driver starts with only the options given here. Driver options are
schema-validated; an unknown driver, instance or option
fails startup. A misspelled option (`urll` for `url`) is refused with
[SANDLOOM-B042](../reference/error-codes.md): the message lists the accepted keys and the
nearest one. The `sqlalchemy` option `tenant_rls: true` turns on
PostgreSQL row-level security for that instance, with `system_url` /
`require_system_channel` for its system channel (see
[Multi-tenancy](../modules/database/multi-tenancy.md)). PostgreSQL URLs always name their driver:
`postgresql+asyncpg://` for the app; migrations derive
`postgresql+psycopg2://` from it. Never rely on a bare `postgresql://`.

`/diagnostics/infra` reports the configured capabilities, instances,
stability, registered drivers and per-instance health.

## Set `ENVIRONMENT` on every deployment

`ENVIRONMENT` takes `dev`, `test` or `prod`. <!-- prose-ok: P6 the three values --> Left unset it
reads `dev`, the setting of a development machine. No production validator applies, and the
interactive docs are exposed. A deployment that forgot the variable would run that way, so an
unset `ENVIRONMENT` is not treated as a choice of `dev`:

- Startup logs a warning. Every process that boots the app logs it, the worker and the scheduler
  included. `sandloom doctor` reports the same thing.
- A database that is not on this machine refuses startup with
  [SANDLOOM-B043](../reference/error-codes.md). A database is on this machine when it is
  `localhost`, a loopback address (`127.0.0.0/8` or `::1`), a unix socket or a SQLite file. <!-- prose-ok: P6 the local hosts -->
  The check reads `DATABASE_URL` and the `url` of every `INFRA` database instance. A PostgreSQL url
  that names no host connects where `PGHOST` says, so the check reads that setting too. The
  message names the host or the parameter, never the url.
- A token from `sandloom dev token` is refused with `401`, and the command refuses to mint one.
  The same holds for a call a `dev` caller makes over internal RPC for such a token's user.

The database check accepts only what it can show is local. A `dsn` or `service` query parameter
can name any host, so the url is refused. So is a non-SQLite url that names no host. The driver
takes `PGHOST` from the process environment only, while settings also read `.env`: export it, and
do not rely on `.env` for it.

Setting the variable ends all three. `ENVIRONMENT=prod` is a deployment. `ENVIRONMENT=dev`, which
`sandloom new` writes into `.env`, silences the warning and allows a development database on
another host. A value from any place settings are read from counts as set.

## Upgrade: flat settings that became driver options

Five groups of flat settings are removed. Each became an option of the driver
that read it, written on that driver's instance in `INFRA`. Startup refuses a
removed setting that carries a value, and names the option to write. An empty
value only logs a warning. The url and bound settings are the exception (the
`CACHE_URL` row, the `BROKER_URL` row and the `CACHE_*` bounds): any value fails
startup. A value counts from every source of the process load: the environment and `.env`
(an empty `BROKER_URL=` too), and a file of `SECRETS_DIR`. There are no aliases.

| Removed setting | Instance | Option |
| --- | --- | --- |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | `notification` (`smtp`) | `host`, `port`, `username`, `password` |
| `SMTP_FROM`, `SMTP_USE_TLS` | `notification` (`smtp`) | `from_addr`, `use_tls` |
| `S3_BUCKET`, `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | `objectstore` (`s3`) | `bucket`, `region`, `access_key_id`, `secret_access_key` |
| `EVENT_STREAM_RETENTION_SECONDS`, `EVENT_STREAM_MAXLEN`, `EVENT_STREAM_PREFIX` | `eventbus` (`streams`) | `retention_seconds`, `max_length`, `prefix` |
| `CACHE_URL` | every Redis instance: `cache.default`, `atomic_store.security`, `lease.beat`, `broadcast.default`, `eventbus.default` (`streams`) | `url`, stated on each instance |
| `BROKER_URL` | `taskqueue` (`celery`) | `broker` |
| `CACHE_SOCKET_TIMEOUT_SECONDS`, `CACHE_SOCKET_CONNECT_TIMEOUT_SECONDS`, `CACHE_HEALTH_CHECK_INTERVAL_SECONDS`, `CACHE_MAX_CONNECTIONS`, `CACHE_POOL_TIMEOUT_SECONDS`, `CACHE_RETRY_ATTEMPTS` | each Redis instance | `socket_timeout_seconds`, `socket_connect_timeout_seconds`, `health_check_interval_seconds`, `max_connections`, `pool_timeout_seconds`, `retry_attempts` |

For example, move `SMTP_HOST=mail` and `SMTP_USE_TLS=false` to:

```sh
INFRA='{"notification":{"default":{"driver":"smtp","host":"mail","use_tls":false}}}'
```

`CACHE_URL`, `BROKER_URL` and the `CACHE_*` bounds are refused with any value, an empty
one included. No value reaches an instance any more, so each instance of the app states
its own url:

```sh
INFRA='{"cache":{"default":{"driver":"redis","url":"redis://r:6379/1"}},"atomic_store":{"security":{"driver":"redis","url":"redis://r:6379/3"}},"taskqueue":{"default":{"driver":"celery","broker":"redis://r:6379/2"}}}'
```

List only the instances the app selects. The Celery result backend is off unless
`taskqueue.default.backend` is stated. The Redis client bounds keep the defaults the
`CACHE_*` settings had.

Four details to know. STARTTLS stays on unless you write `use_tls: false`, as
it did with `SMTP_USE_TLS`, and production still refuses credentials without it.
`use_tls: false` is still reported as a configuration override (WARNING in
production, `/diagnostics/core`, `sandloom doctor`). The `multi_tenant_strict`
profile refuses it.
Setting `SMTP_USE_TLS=false` fails startup instead of being ignored, so TLS
never turns back on behind your back. The object store driver is still chosen
by `STORAGE_TYPE=s3`.

## Use a named instance

Modules ask for an instance by name, never by engine or connection string:

```python
from typing import Annotated
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.infra import cache
from sandloom.sdk.web import Database

@router.get("/reports/latest")
async def latest_report(
    analytics: Annotated[AsyncSession, Database("analytics")],
) -> ReportOut:
    await cache("sessions").set("reports:last", "...", ttl_seconds=60)
    return await services.load(analytics)
```

Every setting is in the [configuration reference](../reference/configuration.md). See also
[what is included](index.md#what-is-included).
