---
name: sandloom-module-rules
description: Use when you write or review the parts of a Sandloom module that no check enforces: a table reached through another tenant row, an effect that can run twice, event order and versions, a contract command, a list route, a password or role change, a side effect outside the database, or a task. Each rule has an Incorrect and a Correct example.
---

# Module rules no check enforces in <app>

Nothing fails when you get one of these wrong, so read the rule for the work at
hand before you write it. Each entry links to a file with an Incorrect and a
Correct example. A rule a check does enforce names its code, and
`uv run sandloom explain <code>` gives the fix.

## High impact

- [A contract command is not atomic with your unit, so make it a saga step](rules/contract-command-is-a-saga-step.md) (contracts, sagas, consistency)
  A contract command has committed in the provider when it returns and stays applied if your unit rolls back: run it as a saga step with a compensation.
- [An effect outside a durable consumer needs its own dedupe](rules/effect-needs-dedupe.md) (effects, idempotency, consumers)
  An effect (a grant, a charge, a row) applied outside a durable consumer carries its own dedupe in the same unit: a UNIQUE `operation_key(...)` on its row, or `durable_inbox(...).claim(db(), id)`.
- [A breaking event change is a new .v2 class, published beside v1](rules/event-breaking-change-is-v2.md) (events, versioning)
  A breaking change is a new `.v2` class next to `.v1`, published with `publish_event_versions` until no `.v1` reader is left.
- [Events arrive more than once and out of order](rules/event-order-by-version.md) (events, consumers, ordering)
  Delivery is at-least-once and unordered: carry a per-aggregate `version` in the event and apply an event only when its version is higher than the stored one.
- [A password, role or membership change revokes the user's tokens](rules/revoke-tokens-on-security-change.md) (security, auth, sessions)
  After a password, role or membership change, call `revoke_user_tokens_on_commit(..., require_immediate=True)` as the last step that can fail, and `record_audit` the change.
- [A side effect outside the database waits for the commit](rules/side-effect-on-commit.md) (effects, transactions)
  Register an email, a cache write or any call outside the database with `await on_commit(callback)`: it runs after the commit and never when the unit rolls back.
- [A row owned through another tenant row is tenant data](rules/tenant-row-through-parent.md) (tenancy, models, rls)
  A table whose rows belong to a tenant row (a note's comments, an order's lines) inherits `TenantScoped`, even with no `organization_id` column.

## Medium impact

- [A list route caps its limit](rules/cap-list-limit.md) (api, pagination)
  Declare a list route's `limit` with `Query(ge=1, le=<cap>)`: with no upper bound one request reads a whole table.
- [A task body is async, opens its own unit and can run twice](rules/task-body-async-repeatable.md) (tasks, units, idempotency)
  A task body is an `async def` that works in `async with open_uow():`, never `asyncio.run`, and a repeated run does no harm.

Finish with skill `sandloom-quality-gates`.
