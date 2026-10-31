---
title: Capabilities and infrastructure
description: The pluggable infrastructure layer - capabilities, drivers and named instances, what modules may touch, and what ships today.
type: concept
---

# Capabilities and infrastructure

All infrastructure (databases, caches, object stores, task queues, the event
bus, notifications) is reached through one plugin layer. A **capability**
(`database`, `cache`, `objectstore`, `taskqueue`, `eventbus`, `notification`,
...) is a versioned contract of operations; a **driver** is a concrete backend
that implements it, chosen by configuration; an **instance** is a named,
configured driver, such as the `default` database and a second `analytics`
database. A module knows infrastructure only by capability and instance
name, never by engine, driver or connection string, so the same module code
runs against any configured backend and in any deployment profile.

## What you get by default / what you declare

| You get by default | You declare |
| --- | --- |
| A `default` instance of every capability, built from the flat settings (`DATABASE_URL`, `CACHE_URL`, `BROKER_URL`, `STORAGE_*`, `EVENT_BUS_BACKEND`) | extra instances or driver overrides in the `INFRA` setting |
| `db()` / `DbSession`: the session of the running unit on the module's database | `Annotated[AsyncSession, Database("analytics")]` for a named database |
| Operation-based accessors: `cache()`, `notifier()`, `objectstore()`, `taskqueue()`, `capability(...)` | the instance name, when not `default` |
| A startup preflight of the module's needs, without I/O | `define_module(..., capabilities=[CapabilityRequirement(...)])` for anything beyond the defaults |
| Refusal of backend SDKs, raw clients and backend constructors in modules | nothing |

## How to use it

### Resolve by logical name

```python
from typing import Annotated
from sqlalchemy.ext.asyncio import AsyncSession

from sandloom.sdk.infra import Notification, cache, notifier
from sandloom.sdk.web import Capability, Database

@router.get("/reports/latest")
async def latest_report(
    analytics: Annotated[AsyncSession, Database("analytics")],   # a named database
) -> ReportOut:
    rows = await services.load(analytics)
    await cache().set("reports:last", "...", ttl_seconds=60)
    await notifier().send(Notification(channel="email", recipient=..., subject=..., body=...))
    ...
```

- **Databases.** `db()` is the session of the running unit on the module's
  database (see [Units of work](units.md)). A named database is
  `Annotated[AsyncSession, Database("x")]` in a route, or `open_uow("x")` in a
  block. Each database has its own unit and its own outbox; there is no
  transaction across databases. A route that writes to several needs
  `writes(...)` or `allow_multi_db_write(...)` (see
  [Write admission](write-admission.md)).
- **Cache.** `cache(name="default")` returns the operation-based cache:
  `get` / `set` / `add` / `delete` / `incr`, plus `compare_and_set` /
  `compare_and_delete` behind the feature `cas`, and the fused `incr_window` /
  `incr_many` / `set_nx_get` / `set_max` (one round trip on Redis, a portable
  fallback elsewhere). Never write security state through `cache()`: the
  framework keeps revocation marks and idempotency records in its own
  security cache instance.
- **Notifications.** `await notifier().send(Notification(channel="email",
  recipient=..., subject=..., body=...))`. The default driver `log` only
  records (never the raw recipient); `NOTIFIER_BACKEND=smtp` plus `SMTP_*`
  sends email. In production `log` advertises no channel features, so a
  module requiring `features={"email"}` fails at startup unless
  `SANDLOOM_ACCEPTED_RISKS` has `log_notifier_in_prod`. Pair it with an `@on`
  consumer to notify on events.
- **Object store, task queue, any capability.** `objectstore(name)`,
  `taskqueue(name)` and `capability(cap, name, contract=None)` from
  `sandloom.sdk.infra`. In a route, the universal dependency is
  `Annotated[GraphStoreProvider, Capability("graph_store", GraphStoreProvider, "recommendations", features={...})]`
  from `sandloom.sdk.web`, which enforces the contract and the features.
- **The runtime view.** `core = Depends(get_core)` (or
  `await get_core(request)`) gives the read-only `CoreRuntime`, with
  `core.capability(cap, name)`, `core.require_capability(cap, name,
  features={...})` and the typed sugar `core.cache()`, `core.database()`,
  `core.objectstore()`, `core.taskqueue()`, `core.notifier()`.

The `ModuleAPI` handed to `setup(core)` declares needs; it does not resolve
infrastructure. Resolution happens at runtime.

### Operations, not handles

| Capability | Operations |
| --- | --- |
| `database` | a unit-of-work factory, plus `outbox()` / `audit()` writers behind the features `transactional_outbox` / `audit` |
| `cache` | `get`, `set`, `add`, `delete`, `incr`; `compare_and_set` / `compare_and_delete` (feature `cas`); fused `incr_window`, `incr_many`, `set_nx_get`, `set_max` |
| `objectstore` | `put(key, data, content_type=...)`, `get`, `delete`, `url` (key-addressed: a logical key and bytes, never a host path) |
| `taskqueue` | `enqueue(task, payload, *, delay_seconds=None, idempotency_key=None)` (see [Background tasks](background-tasks.md)) |
| `notification` | `send(Notification(...))` |

Engine-specific surface (Redis `eval` / `pubsub`, Celery `send_task`, a
filesystem path) stays behind a driver-only `raw_client()` / `raw_backend()`
escape hatch that business modules may not call.

### Declare what you need

```python
from sandloom.sdk import define_module
from sandloom.sdk.module import CapabilityRequirement

register = define_module(
    __name__,
    capabilities=[
        CapabilityRequirement("cache", features=frozenset({"incr"})),
        CapabilityRequirement("database", instance="analytics"),
    ],
)
```

The loader preflights requirements at startup, without I/O: a required one
that is unknown, unconfigured or missing a feature fails the module load (and
startup, if the module is required); an optional one (`required=False`) only
warns. A module whose tables live in a non-default database declares it as
`CapabilityRequirement("database", instance="analytics")`; migrations read
this to choose the database that hosts the module's chain (see
[Migrations](migrations.md)).

### Configuration, not code, picks backends

The composition root (`create_app`, the worker, `sandloom migrate`, the test
harness) builds the registry. There is no implicit default registry:
`cache()`, `notifier()` or any accessor with none bound raises, in every
environment, with a hint naming those roots.

The flat settings select only the drivers Sandloom ships; a plugin driver is
always selected through `INFRA`. The `INFRA` setting (JSON) adds named instances
or overrides drivers and options:

```json
{"database": {"analytics": {"driver": "sqlalchemy",
                            "url": "postgresql+asyncpg://.../analytics"}},
 "cache":    {"sessions":  {"driver": "redis", "url": "redis://.../3"}}}
```

Driver options are schema-validated; an unknown driver, instance or option
fails startup. The `sqlalchemy` option `tenant_rls: true` turns on
PostgreSQL row-level security for that instance, with `system_url` /
`require_system_channel` for its system channel (see
[Multi-tenancy](multi-tenancy.md)). PostgreSQL URLs always name their driver:
`postgresql+asyncpg://` for the app; migrations derive
`postgresql+psycopg2://` from it. Never rely on a bare `postgresql://`.

`/diagnostics/infra` reports the configured capabilities, instances,
stability, registered drivers and per-instance health.

### Adding a backend is a driver, not a kernel edit

A driver subclasses the capability's provider contract from
`sandloom.sdk.drivers` and registers itself with
`registry.register_driver(capability, driver, factory)`, published as a package
under the `sandloom.infra` entry-point group (a callable `register(registry)`; a
broken entry point is logged and does not abort startup).
[Write a driver](../guides/write-a-driver.md) has a complete example, how to select
it with `INFRA` and how to prove it is used.

Drivers are the only code that constructs a backend, including for the `default`
instance. Inside the framework, a backend constructor (`create_async_engine`,
`redis.from_url`, `Celery(...)`) appears only in the kernel's driver tree, built
lazily so nothing opens at import. A third-party driver follows the same rule in
its own package, and business modules never construct one.

A capability is a `CapabilitySpec`: the provider contract every driver must
implement, the optional features a driver may advertise, an optional config
schema, a version and a stability (`stable` or `experimental`). The registry
enforces the contract (`ProviderContractError`) and the features
(`FeatureUnsupportedError`); errors carry a fix.

### The relational paradigm

`database` is the **relational** capability: the SQL-shaped system of record
that backs the unit of work, the outbox and audit. The framework abstracts
engine construction and pooling; the query dialect is SQLAlchemy, used only
in `models/` and `services/`. Sibling paradigms (`document_store`,
`graph_store`, ...) have their own contracts and weaker guarantees, so no
universal "database" abstraction has to span them. The relational guarantees
are features (`transactional_outbox`, `audit`), not assumptions: a driver
that cannot provide them does not advertise them. No distributed transaction
spans backends.

The relational backend is not freely swappable today: the only driver is
`sqlalchemy`, the unit of work, outbox, audit, tenancy and migrations are
SQLAlchemy code in the kernel, and the settings require a
`postgresql+asyncpg://` URL. In practice it is PostgreSQL.

Sandloom isolates business code from connecting to and operating infrastructure;
the full set of transactional guarantees is implemented and verified on
PostgreSQL through the SQLAlchemy driver. The unit guarantees (one writer
per request, SANDLOOM-R007; conservative write detection at the statement
boundary; refusal of an exited unit, SANDLOOM-R002; the lazy request unit;
unflushed changes counted as writes) are driver obligations, not kernel
code. A database driver advertises them as the `write_admission` feature
(with `transactions`), implements `LazyUnitOfWorkContract` (`open_lazily` /
`close`) and `DatabaseProvider.has_pending_changes`, and `create_app`
refuses (SANDLOOM-B010) a database that hosts request units without
`write_admission`. Another relational driver must implement these features
to host request units; a driver conformance suite for them is planned.

### What ships today

| Capability | Driver | Status |
| --- | --- | --- |
| `database` | `sqlalchemy` (PostgreSQL via asyncpg) | production-ready; the only relational backend |
| `cache` | `redis` | production-ready for a single Redis endpoint; no built-in Sentinel or Cluster client (use a managed primary endpoint with failover, or a driver plugin) |
| `eventbus` | `streams` (Redis Streams, durable) | production-ready (the `auto` default in production) |
| `eventbus` | `redis` (Pub/Sub), `memory` | development and tests only; refused in production unless `SANDLOOM_ACCEPTED_RISKS` lists `volatile_event_bus` |
| `taskqueue` | `celery` (Redis broker) | production-ready; replacing the task runtime is not a configuration change |
| `notification` | `log`, `smtp` | production-ready (`log` only records; `smtp` sends) |
| `objectstore` | `local` (filesystem) | single node or a shared persistent volume only, with the accepted risk `local_storage_in_prod`; `put` returns a key; no public `url`, CDN or presigned URLs |
| `objectstore` | `s3`, `gcs`, `yc` | not implemented; startup rejects the placeholder unless a plugin supplies a driver |
| `document_store`, `graph_store`, `workflow`, `search`, `lock`, `secrets` | none | contract only, experimental; resolving one without a plugin driver raises `ExperimentalCapabilityError` |

A production deployment today is **PostgreSQL + Redis** (cache, Streams bus,
Celery broker) plus local or shared-volume files and SMTP. Anything else is
reachable only through a driver plugin written against the published
contract.

## What breaks it

| Code or error | When |
| --- | --- |
| [SANDLOOM-C003](../reference/error-codes.md#sandloom-c003) | `raw_client()` / `raw_backend()` in module code |
| [SANDLOOM-C004](../reference/error-codes.md#sandloom-c004) | a backend SDK (`boto3`, `motor`, `pymongo`, `neo4j`, `temporalio`, `kafka`), a legacy global, an infra-internal module or `sandloom.sdk.drivers` imported by a module |
| [SANDLOOM-C005](../reference/error-codes.md#sandloom-c005) | a kernel handle (`.event_bus`, `.infra`, `.service_registry`, `request.app.state.core`) reached in module code |
| [SANDLOOM-C024](../reference/error-codes.md#sandloom-c024) | a raw non-relational query outside `unscoped()` |
| [SANDLOOM-B001](../reference/error-codes.md#sandloom-b001) | a required `CapabilityRequirement` that is unknown, unconfigured or missing a feature |
| [SANDLOOM-B009](../reference/error-codes.md#sandloom-b009) | a write route with several writable sessions and no write store |
| `ProviderContractError` / `FeatureUnsupportedError` / `ExperimentalCapabilityError` | a driver of the wrong contract, a missing feature, an experimental capability without a driver |

??? note "Guarantees, precisely"
    - **Construction (G-INFRA-01, 02).** Backends are constructed only by
      drivers; no implicit default registry.
    - **Contracts and features (G-INFRA-03, 04, 05, 07).** Enforced by the
      registry, validated at build.
    - **Preflight (G-INFRA-06).** A module's requirements are checked at load
      without I/O.
    - **Capabilities (C-INFRA-01 to 11).** Driver plugins, synthesized
      defaults and `INFRA`, independent databases each with its own unit and
      outbox, DI resolution by name, experimental capabilities, the log
      notifier, the key-addressed object store, the operation-based cache,
      diagnostics and the universal accessor.
    - **No cross-database atomicity (G-UOW-07).** Two databases are two
      units; one invariant belongs in one database.

    See the [guarantee ledger](../reference/guarantees.md) and
    [ADR-0005](../adr/0005-effect-model.md).

??? note "How it works"
    The composition root builds the registry from the settings and binds it to
    the core; module-level accessors (`cache()`, `notifier()`, ...) resolve
    through the process's active registry. Outbox and audit writes go through
    the `OutboxStore` / `AuditStore` contracts that the database provider
    exposes, so the kernel's write path never imports a concrete ORM model.
    The paradigm-agnostic kernel imports no SQLAlchemy at runtime; an
    architecture test allowlists only the relational surface.

## Design rationale

- [ADR-0007: kernel layers](../adr/0007-kernel-layers.md) (why the default
  registry fallback was dropped).
- [ADR-0006: accepted risks](../adr/0006-accepted-risks.md) (production
  weakenings as named risks).
- [ADR-0001: framework distribution](../adr/0001-framework-distribution.md)
  (driver plugins as packages).
