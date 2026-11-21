---
title: Zero-downtime migrations
description: Change a PostgreSQL schema while the app serves traffic - expand and contract, concurrent indexes, batched backfills, NOT VALID constraints, and the lock_timeout the migration runner sets.
type: how-to
---

# Zero-downtime migrations

**Goal:** a schema change that the running app and workers keep serving
through, without a queue of requests stuck behind the migration.

**Use it** for a revision that runs against a table the app reads or writes
while the migration job runs. That is every production deployment after the
first.

**Do not use it** for a table nobody reads yet, such as the tables of a new
module: a plain `sandloom migrate new` revision is enough. How a revision is
written and applied: [Write a migration](write-migration.md) and
[Run migrations](run-migrations.md). Background:
[Migrations](migrations.md).

## Why a waiting `ALTER TABLE` takes the table down

Most `ALTER TABLE` forms need an `ACCESS EXCLUSIVE` lock. If a transaction
that touched the table is still open, the `ALTER` waits for it. Every query
that arrives after the `ALTER` queues behind the `ALTER`, not behind the old
transaction. A short statement that waits for a long transaction therefore
blocks all traffic on that table for the length of the wait.

The migration runner bounds that wait with a PostgreSQL `lock_timeout`. The
statement gives up, the revision fails and rolls back, and the table was
blocked for at most the timeout. You retry when the long transaction is gone.

## The timeouts the runner sets

On PostgreSQL, the chain runner sets two session settings on its connection.
The runner is `run_migrations` in the `env.py` of every chain, the core
chain's included.

| Env var | Default | Sets |
| --- | --- | --- |
| `MIGRATION_DDL_LOCK_TIMEOUT_MS` | `5000` | `lock_timeout`: how long a statement of the run waits for a lock before it fails |
| `MIGRATION_STATEMENT_TIMEOUT_MS` | `0` | `statement_timeout`: how long one statement may run; `0` is no limit |

- They are written with `SET` right after the runner takes its advisory lock.
  Waiting for another migrator is a different setting,
  `MIGRATION_LOCK_TIMEOUT_SECONDS` (see
  [Run migrations](run-migrations.md#2-apply-everything)). Raising one of the
  settings does nothing for the other.
- A value of `0` is written too. It switches off a `lock_timeout` or
  `statement_timeout` that the server, the database or the role sets by
  default.
- The statement timeout is off by default because a backfill or an index build
  is long on purpose. Set it when you prefer a failed run to a statement that
  holds locks for hours.
- A SQLite database runs no chain, so nothing is set there
  ([SANDLOOM-B028](../../reference/error-codes.md#sandloom-b028)). The runtime
  pools keep their own settings (`DB_STATEMENT_TIMEOUT_MS`,
  `CONTRACT_DB_LOCK_TIMEOUT_MS`).
- `lock_timeout` covers row locks as well as table locks. A backfill batch
  that waits on a locked row for longer than the timeout also fails.

When the lock timeout fires, the run stops with an error that names
`MIGRATION_DDL_LOCK_TIMEOUT_MS` and how to lift it. The statement timeout does
the same for `MIGRATION_STATEMENT_TIMEOUT_MS` when it is above `0`. Any other
database error reaches you unchanged.

```text
a migration statement waited longer than MIGRATION_DDL_LOCK_TIMEOUT_MS=5000 for a table lock: ...
```

### Lift the lock timeout for one revision

A revision that must wait, for instance because you stopped the app first,
lifts the timeout for its own transaction:

```python
--8<-- "docs/snippets/migrations/lift_lock_timeout.py:revision"
```

`SET LOCAL` lasts until the transaction ends. One run of a chain applies all
its pending revisions in one transaction. The lift therefore also covers the
revisions after this one in the same run. Keep a revision that lifts it on its
own. Inside `op.get_context().autocommit_block()` there is no transaction:
use `SET lock_timeout = 0`, which holds for the rest of the connection.

To lift it for a whole run, set `MIGRATION_DDL_LOCK_TIMEOUT_MS=0`.

## Expand and contract

Deploy a schema change in steps that each work with the code before and after
it. The migration job runs before the new app version starts. For a moment the
old code runs against the new schema.

1. **Expand.** A revision adds what the new code needs and removes nothing:
   a nullable column, a new table, a new index. The old code ignores it.
2. **Migrate the data.** Fill the new column (see
   [Backfill in batches](#backfill-in-batches)). Deploy code that writes both
   the old and the new column and reads the new one.
3. **Contract.** Once no running code reads the old column, a later revision
   drops it, or sets the constraint that the old code would have violated.

A rename is an expand (add the new column), a backfill and a contract (drop
the old one). Never use `op.alter_column(..., new_column_name=...)` on a table
that is being served: the old code fails at once.

Safe in the expand step:

- `op.add_column` for a nullable column, or one with a constant default. On
  PostgreSQL 11 and later a constant default does not rewrite the table.
- `op.create_table`, and `op.create_index` as shown below.

Split into steps, because they take a long lock or rewrite the table:

- a `NOT NULL` column without a default, added to a table with rows;
- a change of a column's type;
- a check, foreign key or unique constraint added in one step.

## Create an index without blocking writes

A plain `CREATE INDEX` blocks writes to the table while it builds.
`CREATE INDEX CONCURRENTLY` does not, but PostgreSQL refuses it inside a
transaction. Alembic runs a chain in a transaction, so step out of it with
`autocommit_block()`:

```python
--8<-- "docs/snippets/migrations/concurrent_index.py:revision"
```

`autocommit_block()` commits the revisions applied so far in the run, runs
the block outside a transaction and opens a new one. The framework's own
revisions do the same: `0006_identity_users_search_trgm.py` and
`0007_identity_drop_redundant_indexes.py` in `sandloom_identity`. Keep the
block to the statements that need it, and give it a revision of its own.

A concurrent build that fails leaves an `INVALID` index under its name. A lock
timeout, a cancel or a duplicate in a unique index all do this.
`if_not_exists=True` then skips the index on the retry. Drop the invalid index
first with `DROP INDEX CONCURRENTLY IF EXISTS ix_notes_owner_id`.

Declare the index in the model as well. The chain must still match the models
(SANDLOOM-T006), and every model index must exist in a migration
(`test_model_indexes_migrated.py`).

## Backfill in batches

An `UPDATE` over a whole table is one statement in the chain's transaction. It
locks every row it touched until the run ends and writes a lot of WAL at once.
Write a backfill so that:

- each batch is its own short transaction: run it inside
  `autocommit_block()`, where every statement commits;
- it is idempotent: it selects only rows that are not done. A run that stops
  halfway continues where it stopped, and a second run does nothing;
- the batch is bounded with `LIMIT`, so one statement holds few locks and
  fits `MIGRATION_DDL_LOCK_TIMEOUT_MS`.

```python
--8<-- "docs/snippets/migrations/backfill_in_batches.py:revision"
```

For a table with many millions of rows, run the backfill as a job of its own
and keep the revision to the DDL. A migration job that runs for an hour delays
every deployment behind it.

## Add a constraint in two steps

`ADD CONSTRAINT ... CHECK` and `ADD FOREIGN KEY` scan the table while they
hold a strong lock. Add the constraint as `NOT VALID` first. It applies to new
and changed rows at once and skips the old ones. Then validate it, which takes
a lock that blocks neither reads nor writes:

```python
--8<-- "docs/snippets/migrations/constraint_not_valid.py:revision"
```

The validating revision comes later, once the existing rows satisfy the
constraint:

```python
--8<-- "docs/snippets/migrations/constraint_validate.py:revision"
```

Put the `VALIDATE` in its own revision. It fails if an old row breaks the
constraint, and the `NOT VALID` step has already shipped. On PostgreSQL 12 and
later, `ALTER COLUMN ... SET NOT NULL` skips its scan when a validated
`CHECK (column IS NOT NULL)` constraint exists. The same two steps make a
column `NOT NULL` without a long lock.

## Downgrade policy

`sandloom migrate` has no downgrade command. It applies chains forward:
`upgrade-all`, `upgrade-module` and `upgrade-core`. Treat a revision that has
run in production as final:

- to undo it, write a new revision that reverses it;
- write `downgrade()` for the expand steps anyway: development and the
  migration tests (`migrate_module` in `sandloom.core.testing.migrations`)
  walk a chain down and up;
- a contract step (a dropped column or table) loses data that no `downgrade()`
  restores. Take a [backup](../../operate/operator-reference.md#backups-restore)
  before it, and keep it apart from the expand revision so you can stop
  between them.

Never edit a revision that has run on a shared database
([When to edit a revision instead](write-migration.md#when-to-edit-a-revision-instead)).

## Verify

```bash
sandloom test notes             # models match the chain (SANDLOOM-T006)
sandloom migrate plan           # what would run
```

On a copy of production data, run the revision with a long transaction open
on the table (`BEGIN; SELECT 1 FROM notes LIMIT 1;` in another `psql`
session). The `ALTER` fails after `MIGRATION_DDL_LOCK_TIMEOUT_MS`.

## Errors you may hit

- "a migration statement waited longer than `MIGRATION_DDL_LOCK_TIMEOUT_MS`":
  a transaction holds a lock the revision needs. End it and retry, raise the
  setting for this run, or lift it in the revision.
- "a migration statement ran longer than `MIGRATION_STATEMENT_TIMEOUT_MS`":
  raise the setting or set it to `0`.
- `cannot run inside a transaction block`: a `CONCURRENTLY` statement outside
  `autocommit_block()`.

## Related

- [Write a migration](write-migration.md).
- [Run migrations](run-migrations.md).
- [Migrations](migrations.md).
- [Configuration reference](../../reference/configuration.md).
