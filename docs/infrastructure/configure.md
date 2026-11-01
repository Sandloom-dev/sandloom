---
title: Configure backends
description: The default instance of every interface comes from flat settings; INFRA adds named instances, overrides drivers and sets options.
type: concept
---

# Configure backends

A `default` instance of every interface is built from the flat settings: `DATABASE_URL`,
`CACHE_URL`, `BROKER_URL`, `STORAGE_*`, `EVENT_BUS_BACKEND` and `NOTIFIER_BACKEND`. For most
applications that is all the configuration there is.

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
[Multi-tenancy](../concepts/multi-tenancy.md)). PostgreSQL URLs always name their driver:
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
[what is included](../concepts/capabilities-and-infrastructure.md#what-is-included).
