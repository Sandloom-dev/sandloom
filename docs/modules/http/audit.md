---
title: Audit
description: Recording security-sensitive actions with record_audit in the unit of work, the automatic audit row of sensitive writes, and why denials are logged instead.
type: concept
---

# Audit

The audit log records **committed** security-sensitive actions: who did what
to which resource. An audit row is written in the same unit as the change, so
it exists if and only if the change committed.

## What you get by default, and what you declare

| You write | Sandloom does for you |
| --- | --- |
| nothing, on a sensitive write (`admin`, `money`, `security` risk) | one automatic audit row `sandloom.sensitive_write`: actor, route template, method, path parameters, risks and their sources, status; never the body ([Route policy](route-policy.md)) |
| `record_audit("tenancy.member.role_changed", actor_id=..., ...)` | an `audit_log` row in the current unit, committed with the change |
| nothing, on a denial | a structured deny log and the `access_denied_*` metric (the request rolls back, so no audit row) |

```python
from sandloom.sdk import record_audit

record_audit(
    "tenancy.member.role_changed",
    actor_id=user.user_id,
    resource_type="membership",
    resource_id=str(membership_id),
    data={"from": "member", "to": "admin"},
)
```

`record_audit(action, *, actor_id=None, actor_type="user",
resource_type=None, resource_id=None, data=None, required=None,
database=None)` is synchronous: it queues the row on the current unit
(`required=None` means required). Exact signature:
[`sandloom.sdk.effects.record_audit`](../../reference/sdk/effects.md#record_audit).

## Semantics

- The row is written to `audit_log` **in the current unit**: it commits if
  and only if the business change commits. A rolled-back request leaves no
  audit row.
- Keep **personal data out of `data`**: it is a security trail, not an
  analytics store. `data` passes through the log redactor before it is
  stored.
- **Required by default.** A missing unit, a database without an audit store
  or a non-UUID `actor_id` raises `AuditUnavailableError` instead of dropping
  the record. `required=False` is the explicit best-effort form (those cases
  are skipped and logged); in module code it needs a
  `# sandloom: best-effort <reason>` marker on the call
  ([SANDLOOM-C007](../../reference/error-codes.md#sandloom-c007)).
- **The database.** The audit row goes to the route's write store, the same
  database as the write ([Write admission](../guarantees/write-admission.md)). In a unit
  opened on a named database, `database="billing"` names it explicitly; an
  ambiguous choice raises rather than auditing in the wrong database.
- In tests under `client` or `db_uow` the audit row lands in the test
  transaction ([Testing reference](../../reference/testing.md)).

## Audit and deny logging

Audit is for committed actions. An authorization denial raises and rolls back
its unit, so it is recorded by the structured deny log and the
`access_denied_*` metrics instead ([Authorization](authorization.md)). The
operator exports the audit trail with `sandloom security` (see the
[operator reference](../../operate/operator-reference.md#security-incident-runbook-database-leak-credential-leak-key-compromise)).

## What breaks it

| Code | What it catches |
| --- | --- |
| [SANDLOOM-C007](../../reference/error-codes.md#sandloom-c007) | `record_audit(required=False)` without a marker |
| [SANDLOOM-R002](../../reference/error-codes.md#sandloom-r002) | an effect outside any unit |

??? note "Guarantees, precisely"

    - The audit row commits with the change, in the write store (G-AUDIT-01,
      G-AUDIT-02).
    - One automatic, mandatory audit row per sensitive write; none for a read
      or a refused request (G-SENS-04).

    The ledger with the enforcing tests: [Guarantees & capabilities](../../reference/guarantees.md).

## Design rationale

- [ADR-0005: the effect model](../../adr/0005-effect-model.md)
- [ADR-0009: route policy](../../adr/0009-route-policy-and-session-freshness.md), "What risk triggers"
