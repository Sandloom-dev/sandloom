---
title: How-to guides
description: Task-by-task guides - one per generator, plus migrations, testing, deployment and operations.
type: how-to
---

# How-to guides

Each guide does one task, step by step: the generator first, then the code
it writes, then your domain edits, then how to verify. To learn the
framework from scratch, start with the
[tutorial](../getting-started/tutorial.md). To understand why things work the
way they do, see the [concepts](../concepts/index.md).

## Build a module

| Guide | What you do |
| --- | --- |
| [Add a module](add-module.md) | `sandloom add module`: a package with a table, routes, an event, a migration and tests. |
| [Add an event](add-event.md) | `sandloom add event`: a typed event your module publishes. |
| [Add a consumer](add-consumer.md) | `sandloom add consumer`: react to another module's event, once per event. |
| [Add a contract](add-contract.md) | `sandloom add contract`: a synchronous cross-module API, its typed stub and its callers. |
| [Add a saga](add-saga.md) | `sandloom add saga`: a multi-step process across modules with compensation. |
| [Add a projection](add-projection.md) | `sandloom add projection`: a read model kept up to date from events. |
| [Make a write idempotent](idempotent-write.md) | Status replay by default, `replay_safe`, `no_idempotency`, explicit guards, money writes. |
| [Change an event's payload](event-versioning.md) | Add an optional field, or roll a channel from `.v1` to `.v2`. |
| [Testing modules](testing.md) | Write module tests with the fixtures of the testing plugin. |

## Extend

| Guide | What you do |
| --- | --- |
| [Customize a standard module](customize-standard-module.md) | Change how identity, tenancy or billing behave without forking. |
| [Write a driver](write-a-driver.md) | Connect a new backend to a capability and select it by configuration. |

## Generated files and effects

- [Generate contracts and snapshots](generate.md): write and check generated declarations.
- [Deduplicate effects](effect-deduplication.md): HTTP, consumers, commands, tasks and providers.
- [Revoke sessions](session-revocation.md): transactional changes and operator action.

## Data and migrations

| Guide | What you do |
| --- | --- |
| [Write a migration](write-migration.md) | `sandloom migrate new`: a revision with row-level security for new tenant tables. |
| [Run migrations](migrations.md) | `sandloom migrate upgrade-all` and the targeted commands, in development and deployment. |

## Deploy and operate

| Guide | What you do |
| --- | --- |
| [Deployment](deployment.md) | Deploy as a monolith or split into services by configuration. |
| [Split an app into services](split-into-services.md) | `sandloom add deploy`: an `sandloom new` app's microservices profile, gateway, bindings and keys, with no code change. |
| [Service RPC](service-rpc.md) | Call contracts across processes. |
| [Loader configuration](loader-configuration.md) | Control which modules a process loads. |
| [SSO setup](sso-setup.md) | Configure Google, Microsoft or GitHub sign-in. |
| [Admin UI for operators](admin-operations.md) | Run the platform-owner console. |
| [Admin UI for developers](admin.md) | Extend the platform-owner console. |
| [Operations](../operations/index.md) | Operating a deployment: overview. |
| [Operations runbook](../operations/runbook.md) | Health, backups, secrets, incidents. |

## Upgrade

| Guide | What you do |
| --- | --- |
| [Upgrade an application](upgrading.md) | Review package versions, generated contracts, migrations and rollback. |
| [Upgrading to 1.0](upgrading-1.0.md) | Move a module to the `sandloom.sdk` names: every old name and its new home. |
