---
title: Operations
---

# Operations

Running Sandloom in production: the [runbook](runbook.md) covers secrets, migrations,
backups, dead-lettered events (`sandloom outbox` / `sandloom streams`), background
workers, data retention/PII, the audit log, and observability.

The [operator command reference](operator-reference.md) lists every incident
command, flag and production safeguard. Use `/health/simple` for process
liveness and `/ready` to decide whether traffic may be routed to an instance.

Pairs with the [Deployment guide](../guides/deployment.md) (monolith vs.
microservices topology) and the [Security Model](../reference/security-model.md).
