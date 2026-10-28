---
title: Run migrations
description: Apply the core chain and every module's chain with `sandloom migrate`, in development and as a deployment one-shot.
type: how-to
---

# Run migrations

**Goal:** bring every database of a deployment to the head of its migration
chains: the framework's core chain on every database that hosts a module,
and each module's own chain on the database that hosts that module.

**Use it** in development after pulling changes, and in every deployment
before the app, workers and beat start.

**Do not use it** to author a revision (see
[Write a migration](write-migration.md)) or to move a module's existing data
to another database (see [Extract a module's data](#extract-a-modules-data)
below). Background: [Migrations](../concepts/migrations.md).

## Prerequisites

- The environment the app runs with: `DATABASE_URL`, `CACHE_URL`,
  `BROKER_URL` and any named databases. `sandloom migrate` boots the composition
  root (`SANDLOOM_COMPOSITION_ROOT`; an app made by `sandloom new` sets
  `<app>.asgi`) to learn which modules are loaded and where their tables
  live. `SANDLOOM_MODULES`, when set, limits it to those modules.

## Steps

### 1. See the plan

```bash
sandloom migrate plan         # each loaded module's chain, and its database(s)
sandloom migrate databases    # the configured database instances (URLs masked)
```

Both are read-only.

### 2. Apply everything

```bash
sandloom migrate upgrade-all --apply
```

In order, it:

1. applies the **core chain** (outbox, audit log, consumer inbox, contract
   command log, HTTP idempotency log) to **every database that hosts a
   module**, because each database owns its own transactional outbox
   (`default` always hosts one);
2. applies each loaded **module's chain** to the one database that hosts its
   tables.

Without `--apply` it is a dry run. The whole run holds a per-database
advisory lock, so concurrent runs (several replicas, several services)
serialise instead of racing. `--lock-timeout SECONDS` bounds the wait
(default `MIGRATION_LOCK_TIMEOUT_SECONDS`, 300); while waiting, the lock
holder's pid, application name and query start are logged. It exits
non-zero on any failure, which the deployment one-shot relies on.

### 3. Targeted commands

```bash
sandloom migrate upgrade-module notes --apply                 # one module's chain
sandloom migrate upgrade-module notes --rev <revision> --apply
sandloom migrate upgrade-core --database analytics --apply   # core chain, one database
sandloom migrate upgrade-core --all-databases --apply         # core chain, every database
sandloom migrate upgrade-core --rev <revision> --apply
```

A module is named by its short name (`notes`, `identity`) or its dotted
package (`myapp.modules.notes`, `sandloom_identity`). `upgrade-all` always goes
to `head`; use `upgrade-module` or `upgrade-core` for a specific revision.

## In deployment

Run migrations as a one-shot job that finishes before `app`, `worker` and
`beat` start. In compose (`depends_on: condition:
service_completed_successfully`):

```yaml
migrate:
  image: myapp
  command: ["sandloom", "migrate", "upgrade-all", "--apply"]
```

- **Monolith:** one `migrate` job.
- **Microservices:** one `migrate-<service>` job per service database, each
  with that service's `SANDLOOM_MODULES` and `DATABASE_URL`. The advisory lock
  makes overlapping jobs on a shared database safe.

Never run `sandloom projection rebuild` in the migrate job: a rebuild reads its
sources through their contracts and needs the source services up. See
[Deployment](deployment.md).

## Development and repair only: ensure-outbox

```bash
sandloom migrate ensure-outbox --all-databases --apply
```

It creates the shared core tables (`outbox_messages`, `audit_log`,
`consumer_inbox`, `contract_command_log`, `http_idempotency_log`) in a
database with `CREATE ... IF NOT EXISTS`. It has no version tracking and no
drift detection: an existing but outdated table is left as it is. It is
refused in production unless `--force` is given, which you do only to repair
a database the chain cannot reach. Production uses `upgrade-core`.

## Extract a module's data

Moving a module that already holds data from a shared database to its own
database is an offline, maintenance-window step:

```bash
sandloom migrate extract-module notes --from default --to notes_db            # dry run
sandloom migrate extract-module notes --from default --to notes_db --apply --i-have-stopped-writes
sandloom migrate extract-module notes --from default --to notes_db --verify
sandloom migrate extract-module notes --from default --to notes_db --drop-source
```

`--unfence` rolls back (makes the source tables writable again). Follow the
`sandloom-extract-module` procedure and [Deployment](deployment.md) for the full
cut-over.

## Verify

```bash
sandloom migrate plan
sandloom doctor          # read-only deployment preflight
```

`/ready` answers `503` while the app is degraded, for example when a
required table is missing.

## Errors you may hit

- "Module not found": the module is not loaded by the composition root
  (check `SANDLOOM_COMPOSITION_ROOT` and `SANDLOOM_MODULES`).
- A lock timeout: another migration runner holds the database's advisory
  lock; the log names its pid and query.
- "Refusing 'ensure-outbox --apply' in production": use `upgrade-core`.
- [SANDLOOM-T002](../reference/error-codes.md#sandloom-t002) in tests: a table is not
  created by its owning chain, so production would miss it.

## Related

- [Write a migration](write-migration.md).
- [Migrations](../concepts/migrations.md): core chain, module chains, one
  owner per table.
- [Deployment](deployment.md).
- [CLI reference](../reference/cli.md).
- [Operations runbook](../operations/runbook.md): backups before a
  migration.
