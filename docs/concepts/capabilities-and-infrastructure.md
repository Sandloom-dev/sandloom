---
title: Infrastructure
description: How Sandloom reaches databases, caches, queues, files and email through interfaces and drivers, what is included and how configuration chooses a backend.
type: concept
---

# Infrastructure

Your modules use a cache, a database or email without knowing which product is behind
them. Each kind of infrastructure has an **interface**, and a **driver** connects that
interface to a concrete backend. Configuration chooses the driver, so the same module
code runs against any configured backend and in any deployment profile.

- An **interface** is what a module may call, such as `cache().get(key)`. In code and in
  error messages it is called a **capability**.
- A **driver** implements an interface for one backend, such as `redis` for the cache.
- An **instance** is a named, configured driver, such as the `default` database and a
  second `analytics` one.

## What is included

| Interface | What it is for | Drivers |
| --- | --- | --- |
| [Database](../infrastructure/database.md) | your tables, transactions, the outbox and audit | [`sqlalchemy`](../infrastructure/database.md#sqlalchemy) |
| [Cache](../infrastructure/cache.md) | fast shared data and counters | [`redis`](../infrastructure/cache.md#redis) |
| [Event bus](../infrastructure/event-bus.md) | delivering events between modules | [`streams`](../infrastructure/event-bus.md#streams), [`redis`](../infrastructure/event-bus.md#redis), [`memory`](../infrastructure/event-bus.md#memory) |
| [Task queue](../infrastructure/task-queue.md) | background work | [`celery`](../infrastructure/task-queue.md#celery) |
| [Object store](../infrastructure/object-store.md) | files | [`local`](../infrastructure/object-store.md#local) |
| [Notifications](../infrastructure/notifications.md) | email and other messages | [`log`](../infrastructure/notifications.md#log), [`smtp`](../infrastructure/notifications.md#smtp) |

Each page shows what a module calls, and each driver's settings and limits.
[Configure backends](../infrastructure/configure.md) shows how to choose them.

## What you get by default / what you declare

| You get by default | You declare |
| --- | --- |
| A `default` instance of every capability, built from the flat settings (`DATABASE_URL`, `CACHE_URL`, `BROKER_URL`, `STORAGE_*`, `EVENT_BUS_BACKEND`) | extra instances or driver overrides in the `INFRA` setting |
| `db()` / `DbSession`: the session of the running unit on the module's database | `Annotated[AsyncSession, Database("analytics")]` for a named database |
| Accessors that return operations, such as `cache()` and `notifier()` | the instance name, when not `default` |
| A startup preflight of the module's needs, without I/O | `define_module(..., capabilities=[CapabilityRequirement(...)])` for anything beyond the defaults |
| Refusal of backend SDKs, raw clients and backend constructors in modules | nothing |

## Operations, not handles

An interface offers operations, never the underlying client. What only one engine can do
(Redis `eval`, Celery `send_task`, a filesystem path) stays behind a driver-only
`raw_client()` / `raw_backend()`, which business modules may not call.

Drivers are the only code that constructs a backend, and each is built lazily, so nothing
opens at import. To connect a new backend, [write a driver](../guides/write-a-driver.md);
for a new kind of infrastructure, [add your own capability](../infrastructure/own-capability.md).

## Declare what you need

```python
from sandloom.sdk import define_module
from sandloom.sdk.module import CapabilityRequirement

register = define_module(
    __name__,
    capabilities=[
        CapabilityRequirement("cache", features=frozenset({"cas"})),
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

## What breaks it

| Code or error | When |
| --- | --- |
| [SANDLOOM-C003](../reference/error-codes.md#sandloom-c003) | `raw_client()` / `raw_backend()` in module code |
| [SANDLOOM-C004](../reference/error-codes.md#sandloom-c004) | a backend SDK (`boto3`, `motor`, `pymongo`, `neo4j`, `temporalio`, `kafka`), a legacy global, an infra-internal module or `sandloom.driverkit`, or a driver package's module, imported by a module |
| [SANDLOOM-C005](../reference/error-codes.md#sandloom-c005) | a kernel handle (`.event_bus`, `.infra`, `.service_registry`, `request.app.state.core`) reached in module code |
| [SANDLOOM-C024](../reference/error-codes.md#sandloom-c024) | a raw non-relational query outside `unscoped()` |
| [SANDLOOM-B001](../reference/error-codes.md#sandloom-b001) | a required `CapabilityRequirement` that is unknown, unconfigured or missing a feature |
| [SANDLOOM-B009](../reference/error-codes.md#sandloom-b009) | a write route with several writable sessions and no write store |
| [SANDLOOM-B021](../reference/error-codes.md#sandloom-b021), [SANDLOOM-B022](../reference/error-codes.md#sandloom-b022) | a selected driver with two providers, or one that cannot be used |
| [SANDLOOM-B023](../reference/error-codes.md#sandloom-b023) | an experimental capability in production without the accepted risk |
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
- [ADR-0011: the driver API](../adr/0011-driver-api.md)
  (finding drivers by name, two doors for one contract).
