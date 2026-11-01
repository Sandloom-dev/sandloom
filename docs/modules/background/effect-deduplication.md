---
title: Deduplicate effects
description: Choose the correct deduplication boundary for HTTP retries, events, commands, tasks and external providers.
---

# Deduplicate effects

Choose the key for one logical operation and keep it stable across retries.
A fresh random key on every attempt creates a new operation.

| Boundary | Mechanism | What to do |
| --- | --- | --- |
| Authenticated HTTP write | Automatic durable idempotency guard | Reuse the same `Idempotency-Key`; understand status replay in the [HTTP guide](../http/idempotent-write.md) |
| Event consumer | `@on(..., durable=True)` (the default) | Let the consumer's unit claim the envelope ID; duplicate delivery must not repeat committed effects |
| Contract command | `@command(idempotency_key=..., idempotency_scope=...)` | Declare the operation-key argument and the account scope; callers reuse both |
| Task or direct command effect | A durable inbox claim or a unique operation key in the effect's transaction | Atomically claim and write; task redelivery alone is not deduplication |
| External payment or notification | Provider idempotency key plus durable reconciliation | Reuse the provider key after timeouts; resolve ambiguous outcomes before compensating |

The SDK exposes `durable_inbox` and `operation_key` through
[`sandloom.sdk.effects`](../../reference/sdk/effects.md), and the command declaration
through [`sandloom.sdk.contracts`](../../reference/sdk/contracts.md). These are
advanced tools: choose a scope that cannot collide between unrelated tenants,
accounts or operation types.

## Keep the claim and the effect together

A consumer's database claim and its database writes commit in the same unit.
A rollback leaves the delivery available for retry. A claim committed before
an unrelated effect can lose work; an effect committed before its claim can
repeat work. Do not add a second dedupe store alongside the framework's model.

For a task, write a regression that delivers the same logical operation twice
and checks the stored balance, ledger row or other final effect. For consumers,
use `deliver(event, times=2)` with the same envelope identity, and also check the
rollback-and-retry path. The [testing reference](../../reference/testing.md) explains
the real consumer runtime and helpers.

## Boundaries and retention

At-least-once delivery does not become global exactly-once execution. Different
envelope IDs are different deliveries. Outbox retention, inbox retention and
command-log TTL must cover the retry horizon; startup checks refuse inconsistent
windows. An external provider does not participate in the database transaction.
Use an [operation intent](../http/route-policy.md) or a
[saga](../between/sagas.md) when the process crosses those boundaries.
