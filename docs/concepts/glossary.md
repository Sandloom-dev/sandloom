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
| **Module** | One Python package owning one slice of the business: its tables, routes, events and contracts. Modules never import each other. [Modules](modules.md). |
| **Standard module** | A ready-made module Sandloom publishes as its own package: `identity`, `tenancy` or `billing`. It follows the same rules as your modules. The CLI flag `--no-batteries` and the extra `sandloom[standard]` refer to this set; "battery" is the informal word for a standard module. [Standard modules](../modules/index.md). |
| **Kernel** | The framework runtime: transactions, access checks, outbox, routing. Modules reach it only through the SDK. |
| **SDK** | `sandloom.sdk`, the public Python API. Everything else in the framework is private. [Reference](../reference/sdk/index.md). |
| **Driver API** | `sandloom.driver`, the separate experimental API for driver packages. Modules may not import it. Do not confuse it with `sandloom.core.drivers`, the private built-in drivers. [Reference](../reference/driver-api.md). |
| **Beginner names, advanced names** | The 29 SDK names that cover ordinary modules, and every other public name. An advanced import states its reason in a comment. [Modules](modules.md#the-beginner-tier). |

## How modules talk

| Term | Meaning |
| --- | --- |
| **Contract** | A typed, versioned API one module offers to others, such as `tenancy.membership.v1`. The call is synchronous, in-process or over RPC. [Contracts](contracts.md). |
| **Provider, caller** | The module that implements a contract, and the module that calls it. |
| **Stub** | The generated typed client of a contract, written by `sandloom generate`. Callers import the stub, never the provider. |
| **Command** | A contract method that changes state and is deduplicated by an idempotency key. |
| **Event** | A typed, versioned message on a channel, such as `notes.note.created.v1`, published after commit. Asynchronous and delivered at least once. [Events](events.md). |
| **Channel** | The versioned name of an event. It has one producing module. |
| **View** | A consuming module's own class describing the fields it reads from another module's channel. |
| **Consumer** | An `@on` handler that reacts to an event, once per event id. |
| **Projection** | A read model built from events, for lists that span modules. [Projections](projections.md). |
| **Saga** | A process across modules with compensating steps. [Sagas](sagas.md). |

## Infrastructure

| Term | Meaning |
| --- | --- |
| **Capability** | A kind of infrastructure, named by what it does: `database`, `cache`, `objectstore`, `taskqueue`, `eventbus`, `notification`. [Capabilities](capabilities-and-infrastructure.md). |
| **Provider contract** | The abstract class every driver of a capability implements, such as `NotifierProvider`. It is unrelated to a module [contract](contracts.md). |
| **Driver** | One implementation of a capability, such as `redis` for `cache`. Chosen by configuration, and shipped as its own package, never inside a module. [Write a driver](../guides/write-a-driver.md). |
| **Instance** | A named, configured driver, such as the `default` database and a second `analytics` one. Modules ask for an instance by name. |
| **Backend** | The external system a driver connects to, such as PostgreSQL or Redis. |
| **Feature** | An optional ability a driver advertises, such as `cas` for `cache`. A module can require one. |

## Transactions and requests

| Term | Meaning |
| --- | --- |
| **Unit (of work)** | One database transaction. `db()` returns the session of the running unit; only the unit commits. [Units](units.md). |
| **Outbox, inbox** | The tables that make event publishing atomic with a write, and that deduplicate deliveries. |
| **Route policy** | What a route requires, derived from its code: deny by default, `public(reason=)` to opt out. [Route policy](route-policy.md). |
| **Tenant** | The organization or account whose rows a request may see. `TenantScoped` tables are filtered by it. [Multi-tenancy](multi-tenancy.md). |

## Deployment

| Term | Meaning |
| --- | --- |
| **Monolith, microservices** | One process loading every module, or several processes loading one module each. The module code is the same. [Deployment](deployment.md). |
| **Composition root** | The `create_app` call that assembles an application. |
| **Generated files** | Snapshots, stubs and schemas written by `sandloom generate` and committed. [Generate](../guides/generate.md). |
