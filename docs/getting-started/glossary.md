---
title: Glossary
description: The words Sandloom uses - module, standard module, contract, capability, driver, instance, unit and more - each with one meaning and a link to its page.
type: concept
---

# Glossary

Each term has one meaning across this documentation.

## Application structure

| Term | Meaning |
| --- | --- |
| **Application** | Your project from `sandloom new`: its modules, configuration and deployment files. |
| **Module** | One Python package owning one slice of the business: its tables, routes, events and contracts. Modules never import each other. [Modules](../modules/index.md). |
| **Standard module** | A ready-made module Sandloom publishes as its own package: `identity`, `tenancy` or `billing`. It follows the same rules as your modules. The CLI flag `--no-batteries` and the extra `sandloom[standard]` refer to this set; "battery" is the informal word for a standard module. [Standard modules](../modules/standard/index.md). |
| **Kernel** | The framework runtime: transactions, access checks, outbox, routing. Modules reach it only through the SDK. |
| **SDK** | `sandloom.sdk`, the public Python API for modules. Apart from the driver kit below, everything else in the framework is private. [Reference](../reference/sdk/index.md). |
| **Driver kit** | `sandloom.driverkit`, the separate API for driver packages. Modules may not import it. [Reference](../infrastructure/driverkit.md). |
| **Beginner names, advanced names** | The 29 SDK names that cover ordinary modules, and every other public name. An advanced import states its reason in a comment. [Modules](../modules/guarantees/module-internals.md#the-beginner-tier). |

## How modules talk

| Term | Meaning |
| --- | --- |
| **Contract** | A typed, versioned API one module offers to others, such as `tenancy.membership.v1`. The call is synchronous, in-process or over RPC. [Contracts](../modules/between/contracts.md). |
| **Provider, caller** | The module that implements a contract, and the module that calls it. |
| **Stub** | The generated typed client of a contract, written by `sandloom generate`. Callers import the stub, never the provider. |
| **Command** | A contract method that changes state and is deduplicated by an idempotency key. |
| **Event** | A typed, versioned message on a channel, such as `notes.note.created.v1`, published after commit. Asynchronous and delivered at least once. [Events](../modules/between/events.md). |
| **Channel** | The versioned name of an event. It has one producing module. |
| **View** | A consuming module's own class describing the fields it reads from another module's channel. |
| **Consumer** | An `@on` handler that reacts to an event, once per event id. |
| **Projection** | A read model built from events, for lists that span modules. [Projections](../modules/between/projections.md). |
| **Saga** | A process across modules with compensating steps. [Sagas](../modules/between/sagas.md). |

## Infrastructure

| Term | Meaning |
| --- | --- |
| **Capability** | A kind of infrastructure, also called an interface in these docs, named by what it does: `database`, `cache`, `objectstore`, `taskqueue`, `eventbus`, `notification`. [Infrastructure](../infrastructure/index.md). |
| **Provider contract** | The abstract class every driver of a capability implements, such as `NotifierProvider`. Modules see its operations as a Protocol, such as `Notifier`. It is unrelated to a module [contract](../modules/between/contracts.md). |
| **Capability reference** | A typed key to a capability, `CapabilityRef[VectorIndex]("vector_index.v1")`, for one that a package adds. |
| **Driver** | One implementation of a capability, such as `redis` for `cache`. Chosen by configuration, and published as its own package, found by name, never inside a module. [Write a driver](../infrastructure/write-a-driver.md). |
| **Instance** | A named, configured driver, such as the `default` database and a second `analytics` one. Modules ask for an instance by name. |
| **Backend** | The external system a driver connects to, such as PostgreSQL or Redis. |
| **Feature** | An optional ability a driver advertises, such as `cas` for `cache`. A module can require one. |

## Transactions and requests

| Term | Meaning |
| --- | --- |
| **Unit (of work)** | One database transaction. `db()` returns the session of the running unit; only the unit commits. [Units](../modules/database/units.md). |
| **Outbox, inbox** | The tables that make event publishing atomic with a write, and that deduplicate deliveries. |
| **Route policy** | What a route requires, derived from its code: deny by default, `public(reason=)` to opt out. [Route policy](../modules/http/route-policy.md). |
| **Tenant** | The organization or account whose rows a request may see. `TenantScoped` tables are filtered by it. [Multi-tenancy](../modules/database/multi-tenancy.md). |

## Deployment

| Term | Meaning |
| --- | --- |
| **Monolith, microservices** | One process loading every module, or several processes loading one module each. The module code is the same. [Deployment](../operate/topologies.md). |
| **Composition root** | The `create_app` call that assembles an application. |
| **Generated files** | Snapshots, stubs and schemas written by `sandloom generate` and committed. [Generate](../modules/tooling/generate.md). |
