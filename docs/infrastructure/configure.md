---
title: Configure backends
description: The default instance of every interface comes from flat settings; INFRA adds named instances, overrides drivers and sets options.
type: concept
---

# Configure backends

The kernel names no driver for the database, cache, event bus, task queue, broadcast, lease <!-- prose-ok: P6 a list of drivers and settings -->
or atomic store. The composition selects them. `sandloom_standard` picks SQLAlchemy, Redis,
Streams and Celery. An app made by `sandloom new` lists its picks in `DEFAULT_INFRA`
(`<app>/__init__.py`), which it passes to `create_app(default_infra=...)`.

The flat settings give those instances their options: `DATABASE_URL`, `CACHE_URL`, <!-- prose-ok: P6 a list of settings -->
`BROKER_URL`, `STORAGE_*` and `NOTIFIER_BACKEND`. `EVENT_BUS_BACKEND` names the bus
(`memory`, `redis` or `streams`); `auto` leaves it to the composition.

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
fails startup. The `sqlalchemy` option `tenant_rls: true` turns on
PostgreSQL row-level security for that instance, with `system_url` /
`require_system_channel` for its system channel (see
[Multi-tenancy](../modules/database/multi-tenancy.md)). PostgreSQL URLs always name their driver:
`postgresql+asyncpg://` for the app; migrations derive
`postgresql+psycopg2://` from it. Never rely on a bare `postgresql://`.

`/diagnostics/infra` reports the configured capabilities, instances,
stability, registered drivers and per-instance health.

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
