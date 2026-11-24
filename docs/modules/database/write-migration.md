---
title: Write a migration
description: Add a revision to a module's own migration chain with `sandloom migrate new`, which autogenerates the DDL and adds row-level security for new tenant tables.
type: how-to
---

# Write a migration

**Goal:** a new revision in one module's migration chain that brings the
database in line with the module's models, with row-level security on every
new tenant table.

**Use it** after you add, change or remove a table, a column or an index in
a module's `models/`.

**Do not use it** for the framework's core tables (outbox, audit, inbox,
command log): they have their own chain, applied by `sandloom migrate
upgrade-core`. Never put a module's table in the core chain or a core table
in a module chain. To apply migrations, see
[Run migrations](run-migrations.md). Background:
[Migrations](migrations.md).

## Prerequisites

- The development services running and `.env` pointing at your development
  database.
- The module's database **at the head of the module's chain**: autogenerate
  compares the models with the live schema, so an unapplied revision would
  be generated again. Bring it up first:

  ```bash
  sandloom migrate upgrade-module notes --apply
  ```

## Steps

### 1. Change the models

Edit or add a file in `<module>/models/`. A new per-tenant table inherits
`TenantScoped`. Table names start with the module name. A deliberately
cross-tenant table that the platform reads without a bound tenant sets
`__system_access__ = True`.

### 2. Generate the revision

```bash
sandloom migrate new notes "add archived_at to notes"
```

The module is named by its short name (`notes`) or its dotted package
(`myapp.modules.notes`). The command:

1. runs Alembic autogenerate against the module's **own** chain: the
   module's `migrations/` directory, its own version table
   (`alembic_version_<chain>`), and the database that hosts the module
   (`--database <name>` when the module declares several). It compares only
   the module's tables;
2. adds row-level security for every `TenantScoped` table the revision
   creates. `upgrade()` ends with `enable_tenant_rls` and `downgrade()` starts
   with `disable_tenant_rls`, each with `if_configured=True` (and
   `system_access=True` for a model with `__system_access__ = True`). With
   `if_configured=True` a call acts only where the migration runner knows the
   database enforces tenant isolation, from the `tenant_rls` option of its
   `INFRA` instance. One revision then runs on a database with row-level
   security and on one without;
3. prints the file it created and each table it covered.

The revision is numbered after the chain's last one: `0002_notes` in the file
`0002_notes_add_archived_at_to_notes.py`, as the baseline is `0001_notes`.
`--rev-id <id>` sets an explicit revision id instead.

If two branches each add the next revision of one chain, both write `0002_notes`,
and Alembic keeps only one of the files. `sandloom migrate` and the migration
step of `sandloom test` refuse a chain in which two files declare one revision
id. The message names the file to renumber, for example `renumber
0002_notes_add_x.py to 0003_notes`. Rename that file and edit two fields:
`revision` becomes the new id, `down_revision` the id it now follows.

When the models already match the chain, the command refuses with "no changes
detected" and writes nothing. Change a model first, or write a revision by
hand (see "Other forms").

A new tenant table gets these lines (here as rendered by `sandloom add
projection`, which uses the same template):

```python
--8<-- "docs/snippets/generated/projection/demo/modules/notes/migrations/versions/0002_notes_board_projection.py:22:39"
```

### 3. Review the revision

Autogenerate is a draft, not a decision. Read the file and fix what it
cannot know:

- a renamed column appears as a drop and an add: replace them with
  `op.alter_column(..., new_column_name=...)`;
- a new `NOT NULL` column on a table with rows needs a server default or a
  data step. A Python `default=` is not a database default. Autogenerate
  writes `add_column(..., nullable=False)` with no `server_default`, and that
  fails on a table that has rows. `migrate new` prints a warning for each such
  column. Give the column a `server_default=` in the model, or add it
  nullable, backfill it, then make it `NOT NULL`;
- data migrations (backfills) are yours to write; keep them idempotent;
- a table that is being served needs its DDL in steps (a concurrent index,
  a `NOT VALID` constraint, a batched backfill): see
  [Zero-downtime migrations](zero-downtime-migrations.md). The runner gives
  every statement a `lock_timeout` of `MIGRATION_DDL_LOCK_TIMEOUT_MS` (5 s by
  default), so a revision that waits longer for a lock fails;
- RLS is added only for tables the revision **creates**. A table that
  becomes tenant-scoped later needs `enable_tenant_rls(op, "<table>",
  if_configured=True)` by hand, and `disable_tenant_rls` in `downgrade()`.

A money ledger (a model with the `MoneyLedger` mixin) is refused at import
unless its operation-key column is `NOT NULL` and unique, so the constraint
is in the model and autogenerate renders it.

### 4. Apply it

```bash
sandloom migrate upgrade-module notes --apply
```

Without `--apply` the command prints what it would do.

### Other forms

- `sandloom migrate revision notes -m "<message>" --autogenerate` is the same
  autogenerate **without** the RLS step; it refuses "no changes detected" too.
  Prefer `sandloom migrate new`.
- `sandloom migrate revision notes -m "<message>"` writes an empty revision, for
  a pure data migration. It is the only form that writes a revision with
  nothing in it, and it numbers it like the others.

## When to edit a revision instead

Edit an existing revision, including the baseline `0001_<module>_baseline.py`
that `sandloom add module` renders, **only before its first deployment**: while
it has run on no shared database. Staging counts as shared. Typical case: you
replace the generated example table with your real tables before the module
ships. Rebuild your development database afterwards: downgrade the module, or
drop the database and create it again.

To skip that edit, create the module with `sandloom add module <name> --empty`
and run `sandloom migrate new` for your first model. `--entity <name>` keeps
the example and names it for your domain.

After a revision has run anywhere other than your machine, never edit it:
add a new revision. Other databases already recorded the old one and would
never run your change.

## Verify

```bash
sandloom test notes
```

The tests build tables from the models, but the gates check the chain:
a table or index that no revision creates fails (SANDLOOM-T002), and models that
differ from the chain head fail (SANDLOOM-T006).

## Errors you may hit

- [SANDLOOM-T002](../../reference/error-codes.md#sandloom-t002): a table or index of the
  models is not created by the module's chain.
- [SANDLOOM-T006](../../reference/error-codes.md#sandloom-t006): the models differ from
  the chain head; run `sandloom migrate new`.
- [SANDLOOM-C014](../../reference/error-codes.md#sandloom-c014): a per-tenant table does
  not inherit `TenantScoped`.
- "Target database is not up to date" (only with `--against-database`): the
  module's database is behind the chain head; run
  `sandloom migrate upgrade-module <module> --apply` first. Without the flag,
  `sandloom migrate new` compares against a scratch database with every chain at
  head, on the test server of `.env.test` (+ `SANDLOOM_TEST_*`), and needs no
  database of yours to be migrated.
- "Module not found": the module is not loaded by the composition root
  (`SANDLOOM_COMPOSITION_ROOT`, and `SANDLOOM_MODULES` if set).

## Related

- [Run migrations](run-migrations.md): applying chains in development and
  deployment.
- [Zero-downtime migrations](zero-downtime-migrations.md): revisions for a
  table the app is using.
- [Migrations](migrations.md): core chain, module chains, one
  database per chain.
- [Multi-tenancy](multi-tenancy.md): `TenantScoped` and RLS.
- [Add a module](../create.md).
