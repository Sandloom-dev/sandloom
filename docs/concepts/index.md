---
title: Concepts
description: The mental model of Sandloom - modules, units, route policy, events, contracts, projections, guarantees - with the errors that guard each one.
type: concept
---

# Concepts

These pages explain what Sandloom does for you and what you must declare. Most
pages start with a "default / declare" table for module authors, list the
error codes that guard the concept, and end with the exact guarantees in a
collapsed box. The [glossary](glossary.md) defines every term in one line. The design decisions behind them are in the
[decision records](../adr/index.md); every error code is in the
[error catalogue](../reference/error-codes.md).

## Start here

| Concept | What it covers |
| --- | --- |
| [Modules, levels and discovery](modules.md) | atomic, isolated modules; what a module may import; the 29 beginner names; L1 / L2 / L3; how `define_module` discovers declarations |
| [Units of Work and `db()`](units.md) | one transaction per unit, `db()`, lazy request units, ownership and child tasks, `open_uow()`, the outbox, `on_commit` |
| [Route policy and sessions](route-policy.md) | deny by default, `public(reason=)`, the live session check, the commit-time re-check, risk at its source, idempotency modes B / S / X |
| [Events](events.md) | typed `Event` classes, producers and views, `@on` consumers, at-least-once delivery, compatibility |
| [Contracts](contracts.md) | `@contract` / `@command` providers, `Contract(REF)` callers, generated stubs, optional contracts |

## The transaction model

| Concept | What it covers |
| --- | --- |
| [Write admission](write-admission.md) | one writer per request, the write store, `writes()`, the multi-database opt-out |
| [Transaction and replay lifecycle](lifecycle.md) | the matrix of every route kind and unit; the operation-intent mode |
| [Guarantees and labels](guarantees.md) | atomic, no loss, no duplicate, visibility; labels derived from the mechanism |

## Data and access

| Concept | What it covers |
| --- | --- |
| [Multi-tenancy](multi-tenancy.md) | fail-closed tenant scoping, the tenant binding, row-level security |
| [Authorization and billing subjects](authorization.md) | the authorization algebra, three-valued decisions, `of=Account.user()` / `org()` / `bound_tenant()` |
| [Projections](projections.md) | cross-module read models built from events; the convergence model |
| [Audit](audit.md) | `record_audit` and the automatic audit row of sensitive writes |

## Processes and platform

| Concept | What it covers |
| --- | --- |
| [Sagas](sagas.md) | cross-module processes with compensation |
| [Background tasks](background-tasks.md) | Celery tasks, `run_async`, enqueue dedupe, beat |
| [Capabilities and infrastructure](capabilities-and-infrastructure.md) | databases, caches, queues, object stores, the bus, drivers |
| [Migrations model](migrations.md) | the core chain and per-module chains |
| [Deployment topologies](deployment.md) | monolith and microservices by configuration |
