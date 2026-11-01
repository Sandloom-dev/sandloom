---
title: Write admission
description: One writer per request - the first unit that writes becomes the request's writer, a second writing unit is refused at its first write.
type: concept
---

# Write admission: one writer per request

A request writes through **one** unit on **one** database, its write store.
The first unit of the request that writes becomes the request's writer; a
write by any other unit of the same request is refused **before the statement
runs**, with [SANDLOOM-R007](../../reference/error-codes.md#sandloom-r007). This is what
makes "the request committed" mean one atomic change: there is no
distributed transaction, so two writing units could commit one and lose the
other.

## What you get by default, and what you declare

| Situation | Default | What you declare |
| --- | --- | --- |
| a route that writes through `db()` or `DbSession` | the module's primary database is the write store | nothing |
| a route that injects one named database (`Database("x")`) and no `DbSession` | `x` is the write store | nothing |
| a route that injects several writable sessions | refused at startup ([SANDLOOM-B009](../../reference/error-codes.md#sandloom-b009)) | `writes("x")` to name the write store |
| a route that must write to a second database too | refused at its first write (SANDLOOM-R007) | `allow_multi_db_write(secondaries=("y",), reason="...")`, a reviewed opt-out with a lower guarantee |
| a child task that writes | allowed if it is the first writer; refused if another unit already wrote | nothing; the child opens its own unit with `open_uow()` |
| a contract command, a consumer, a task, a saga step | its own admission scope: it writes in its own unit | nothing |

The write store also holds the route's durable idempotency record, its audit
rows and its outbox rows, so they commit atomically with the write.

## How a write is recognised

Two hooks consult the request's admission state:

1. **Before a flush**, the pending ORM changes (`new`, `dirty`, `deleted`).
   The hook also runs inside `commit()`, so unflushed changes are covered.
2. **Before every statement reaches the database cursor**, on every engine
   the framework constructs. Every SQL path reaches it: ORM, Core, `text()`
   and `exec_driver_sql()`, including a raw statement under `unscoped()`.

The classification is conservative: a statement is read-only only if it is
provably so. Everything else is a write.

| Statement | Classified as |
| --- | --- |
| one `SELECT` with no top-level `INTO` | read |
| `WITH ...` whose CTEs contain no `INSERT`, `UPDATE`, `DELETE`, `MERGE`, ending in such a `SELECT` | read |
| `SHOW ...` | read |
| `EXPLAIN` without `ANALYZE` | read |
| `SELECT ... INTO ...` | write (creates a table) |
| `EXPLAIN ANALYZE <statement>` | write (executes the statement) |
| `WITH` containing DML in any CTE | write |
| `COPY ... FROM`, `CALL`, `DO`, `SET`, `LOCK`, multi-statement strings, anything unrecognised | write |

Transaction-control statements (`BEGIN`, `COMMIT`, `SAVEPOINT`) are not
writes. The framework's own statements (the row-level-security setting,
outbox marking) are exempt.

## Rules

1. The first unit that writes becomes the request's writer. It must be the
   route's write store.
2. A write by any other unit of the request is refused before the statement
   reaches the cursor or the flush completes. This covers later units, units
   in child tasks, a nested `open_uow()` on the same database and unflushed
   changes.
3. Contract, consumer, task, saga-step and kernel units start a fresh
   admission scope.
4. `allow_multi_db_write(secondaries=, reason=)` admits the listed
   secondaries. A secondary is never the request's writer, so it may be
   written before or after the write store. It is best-effort one-phase
   commit: secondaries commit first, the write store last, whatever the
   order of the endpoint's parameters or their nesting in dependencies; a
   failure in between is a partial commit, counted in
   `sandloom_multi_store_partial_commit_total{route,store}` (each store that
   committed), logged as a `uow.partial_commit` warning and converged by a
   keyed retry. A money effect is never allowed in a secondary
   ([SANDLOOM-B014](../../reference/error-codes.md#sandloom-b014)).
5. An operation-intent route admits sequential units only: a unit may not open
   while another is open ([SANDLOOM-R008](../../reference/error-codes.md#sandloom-r008)).
   See [the lifecycle](lifecycle.md#operation-intent-mode).

```python
from sandloom.sdk.web import Database, DbSession, writes  # sandloom: advanced two databases


@router.post("/rollups", dependencies=[writes("analytics")])
async def rebuild_rollup(db: DbSession, analytics: Database("analytics")) -> None:
    ...   # reads from db, writes to analytics
```

## Out of scope, stated

- Writes performed inside a database function called by a read statement
  (`SELECT my_func()` that writes): the classifier cannot see them.
- Work in other processes.
- The raw DBAPI handles (`raw_connection()`, `driver_connection`): module code
  cannot reach them ([SANDLOOM-C003](../../reference/error-codes.md#sandloom-c003)), so the
  one path that bypasses the cursor hook is closed to modules.

## What breaks it

| Code | What it catches |
| --- | --- |
| [SANDLOOM-R007](../../reference/error-codes.md#sandloom-r007) | a second writer, at its first write |
| [SANDLOOM-R008](../../reference/error-codes.md#sandloom-r008) | overlapping units in an intent route; a unit opened during an external call or a release check |
| [SANDLOOM-B009](../../reference/error-codes.md#sandloom-b009) | several writable sessions without `writes()` or an opt-out |
| [SANDLOOM-B013](../../reference/error-codes.md#sandloom-b013) | an invalid `allow_multi_db_write` (the secondary is the write store, or not injected) |
| [SANDLOOM-B014](../../reference/error-codes.md#sandloom-b014) | a money effect in an opted-out secondary |
| [SANDLOOM-C023](../../reference/error-codes.md#sandloom-c023) | lists every `allow_multi_db_write` opt-out for review |

??? note "Guarantees, precisely"

    **Scope.** No SQL statement that writes, executed on a framework-constructed
    engine in a request's context, runs in a second unit of that request,
    outside the declared modes. The guarantee covers the ORM, Core, `text()`,
    `exec_driver_sql()` through the session's connection under `unscoped()`, a
    child task, a nested unit and an unflushed change, each with a test
    (G-MSTORE-03/04, G-UNIT-03). Startup refusals: G-MSTORE (B009, B013, B014).
    A property test over random sequences of unit opens, writes, flushes and
    child tasks shows no second writer's statement ever executes.

    The ledger with the enforcing tests: [Guarantees & capabilities](../../reference/guarantees.md).

## Design rationale

- [ADR-0010: Public API 1.0](../../adr/0010-public-api-1.0.md), "One writer per request"
- [ADR-0005: the effect model](../../adr/0005-effect-model.md)
