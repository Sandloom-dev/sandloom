---
title: Runbook
description: What to do in production, by situation - dead-lettered events, parked sagas, restores, key leaks, slow logins - with a link to the exact procedure.
---

# Runbook

Find the situation, then follow the procedure it links to in the
[operator reference](operator-reference.md).

Operator commands (`sandloom migrate`, `outbox`, `streams`, `projection`, `doctor`)
boot your application, so set `SANDLOOM_COMPOSITION_ROOT=<app>.asgi` for them, as the
generated profile does. Run them with `--apply` only after reading the dry run.

## Something is wrong

| Situation | Start with | Procedure |
| --- | --- | --- |
| an alert fired | its row in the alert table | [Alerts](operator-reference.md#alerts) |
| a process won't start or isn't ready | `sandloom doctor` | [Preflight](operator-reference.md#preflight-and-effective-configuration) |
| events stopped arriving, or some were dead-lettered | `sandloom outbox list-dead`, `sandloom streams consumers` | [Events](operator-reference.md#events-outbox-relay-and-dead-letters) |
| a saga is stuck | `sandloom sagas list` | [A parked saga](operator-reference.md#runbook-a-parked-saga) |
| a task may or may not have been enqueued | `TaskEnqueueUncertain`: don't retry with another key | [Uncertain enqueues](operator-reference.md#uncertain-task-enqueues) |
| a database or a key leaked | freeze deploys, `sandloom security audit-export` | [Security incident](operator-reference.md#security-incident-runbook-database-leak-credential-leak-key-compromise) |
| data was lost | stop writers, restore in order, `sandloom backup reconcile-security` | [Restoring](operator-reference.md#restoring-the-order-and-the-traffic-gate) |
| Redis is slow or unavailable | the `CACHE_*` limits and each caller's failure policy | [Redis cache](operator-reference.md#redis-cache-timeouts-pool-revocation-cache) |
| login is slow or CPU spikes at login | `PASSWORD_HASH_PROFILE` | [Password hashing](operator-reference.md#password-hashing-work-factor-capacity-planning) |
| latency spikes with no slow queries | the `GC_*` settings and metrics | [Garbage collector](operator-reference.md#garbage-collector-tuning) |

## Routine work

| Task | Procedure |
| --- | --- |
| store or rotate secrets and keys | [Secrets management](operator-reference.md#secrets-management) |
| apply migrations | [Database migrations](operator-reference.md#database-migrations) |
| plan and verify backups | [Backups](operator-reference.md#backups-restore) |
| set retention and handle erasure requests | [Data retention and PII](operator-reference.md#data-retention-pii) |
| answer "who did this?" | [Audit log](operator-reference.md#audit-log) |
| connect logs, metrics, traces and dashboards | [Observability](operator-reference.md#observability) |
| upgrade asyncpg, SQLAlchemy or aiohttp | [Database drivers](operator-reference.md#upgrading-asyncpg-sqlalchemy), [aiohttp](operator-reference.md#upgrading-aiohttp) |
| upgrade Sandloom | [Upgrade an application](../guides/upgrading.md) |
