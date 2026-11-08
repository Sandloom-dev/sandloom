---
title: Move a module's data to its own database
description: Copy one module's rows, undelivered events and dedupe records from the shared database to its service's database with sandloom migrate extract-module, in a maintenance window, then cut over.
type: how-to
job: When I split a module that already holds production data into its own service, I want its rows moved to the service's database with nothing lost or sent twice, so the split costs no data and the other modules keep serving.
purpose: The operator plans, fences, copies, verifies and cuts over one module's data with sandloom migrate extract-module, and knows how to resume, roll back and clean up.
audience: [operator, agent]
level: operating
---

# Move a module's data to its own database

**Goal:** one module's existing rows live in the database of its own
service, and the shared database no longer accepts writes to them.
[Split into services](split-into-services.md) moves the module's code to the
service by configuration; `sandloom migrate extract-module` moves its data.

**Use it** when a module that already holds data in the shared (monolith)
database gets its own service and database.

**Do not use it** for a new deployment with no data: the service's
`migrate-<service>` one-shot creates its empty tables. It does not move data
while the module keeps writing. The move is offline: the module's writers
stop for a maintenance window while the other modules keep serving.

## Prerequisites

- Run every command with the monolith's configuration. Its `SANDLOOM_MODULES`
  must load the module, or the command stops with `Module not found: …`.
- `--from` and `--to` name two different PostgreSQL databases, each as a
  configured instance name (`default`) or a URL. The target database exists.
- The module's chain is at head on the source
  (`sandloom migrate upgrade-module <module> --apply`).
- No foreign key leads from the module's tables to another module's tables.
- When a module table has row-level security, the role on both sides is a
  superuser or has `BYPASSRLS`. Otherwise the copy would see one tenant's
  rows, and the preflight refuses.

## Steps

### 1. Read the plan

The default is a dry run that writes nothing:

```bash
sandloom migrate extract-module tenancy --from default --to "$TENANCY_DATABASE_URL"
```

On a monolith holding one organization and three members, it printed:

```text
extract-module sandloom_tenancy [extract] DRY-RUN (pass --apply to execute)
  source: postgresql+psycopg2://***@127.0.0.1:25901/mono
  target: postgresql+psycopg2://***@127.0.0.1:25901/tenancy
  source fenced (read-only): no
  target manifest complete: no
  tables (FK order):
    tenancy_organizations              source=1        target=-        pending
    tenancy_invitations                source=0        target=-        pending
    tenancy_member_versions            source=0        target=-        pending
    tenancy_members                    source=3        target=-        pending
  outbox: pending=0 dead=0 (module channels, to hand off); already handed off=0
  consumer_inbox claims to copy: 0
  audit_log rows to copy: 0
  contract_command_log records to copy: 0
  saga instances to copy: 0
  http_idempotency_log records to copy: 0
  action: fence the source (module tables read-only, module outbox channels refuse inserts)
  action: apply the core chain + the module chain to the target
  action: copy + verify tables: ['tenancy_organizations', 'tenancy_invitations', 'tenancy_member_versions', 'tenancy_members']
  action: set sequences, copy consumer_inbox/audit/command-log rows, hand off undelivered outbox rows, write the completion manifest
  streams group owners: none recorded for this module/database
```

A `PROBLEM:` line is a preflight refusal, and the command exits 1. Fix the
cause it names before the window starts.

### 2. Stop every writer of the module

Stop every process that loads the module:

- the app replicas;
- its worker and beat;
- the processes that run its consumers.

Until the cutover, nothing may write the module's tables or publish its
events. The rest of the monolith may keep running.

### 3. Extract

Run the plan's command with `--apply --i-have-stopped-writes`:

```bash
sandloom migrate extract-module tenancy --from default --to "$TENANCY_DATABASE_URL" \
    --apply --i-have-stopped-writes
```

Without `--i-have-stopped-writes` the command refuses:

```text
extract-module refused/failed: refusing to extract without --i-have-stopped-writes: stop every process that writes the module (app, workers, beat, consumers) first; the fence then makes the source read-only
```

The run fences the source first. Triggers make the module's tables
read-only and refuse new outbox rows on its channels. A write to a fenced
table fails:

```text
ERROR:  sandloom: tenancy_organizations is read-only: module sandloom_tenancy is fenced for extraction to another database (sandloom migrate extract-module)
```

Then it migrates the target to head and copies, in this order:

| What | Which rows | After the copy |
| --- | --- | --- |
| Module tables and their sequences | every table mapped under `<package>.models`, and every table named `<module>_…` | each table verified by row count and checksum, recorded in a manifest in the target |
| Undelivered events (`outbox_messages`) | pending and dead rows on the module's channels | same ids in the target; marked delivered in the source, which never sends them again |
| Dedupe records (`consumer_inbox`, `contract_command_log`, `http_idempotency_log`) | the module's consumer claims, command results and stored responses | a redelivered event or a retried call after the move is still answered once |
| Saga state (`saga_instances`, `saga_steps`) | the module's sagas | the service continues each saga from where it stopped |
| Audit rows (`audit_log`) | actions in the module's namespace | the source keeps its copy |
| Streams consumer-group owners | groups the module owns on the source database | re-pointed to the target database |

Every step is idempotent. After a crash or a refusal, fix the cause and run
the same command again: it skips the tables its manifest already holds. A
target table with rows the tool did not write is refused, never
overwritten.

A process that still writes the module makes the fence time out after
`--lock-timeout-ms` (5000 by default), with
`could not fence the source within 5000 ms: … the module still has writers`.
Stop that process and run the same command again.

The group owner is the database the services connect to. When they reach
the databases under other host names than the URLs you pass, add
`--source-identity-url` and `--target-identity-url` with the URLs the
services use.

### 4. Cut over

1. Start the module's service on its own `DATABASE_URL`, with the same
   `CACHE_URL`, event bus and JWT settings as the other services.
2. Remove the module from the monolith's `SANDLOOM_MODULES`.
3. Deploy the bindings and gateway routes that
   [`sandloom add deploy`](split-into-services.md) wrote for the service.

A monolith that still loads the module cannot consume its events after the
group transfer: claiming a moved group raises `GroupOwnershipError`.

## Verify

Compare the target with the manifest at any time; it writes nothing:

```bash
sandloom migrate extract-module tenancy --from default --to "$TENANCY_DATABASE_URL" --verify
```

It prints the same report with `[verify]` and exits 0. A row count or
checksum that differs exits 1 with `verification failed: …`.

## Roll back before the cutover

`--unfence --apply` drops the fence and makes the source writable again.
After a completed extraction the command refuses, because the new service may
already hold writes the source lacks:

```text
PROBLEM: the extraction completed: the new service may already have taken writes that the source does not have. Unfencing makes the stale source writable again — pass --force only if the target never served traffic
```

Add `--force` only when the service never served traffic.

## Clean up the source

Once the service has run cleanly, drop the module's tables from the shared
database. Read the plan first, then apply it:

```bash
sandloom migrate extract-module tenancy --from default --to "$TENANCY_DATABASE_URL" --drop-source
sandloom migrate extract-module tenancy --from default --to "$TENANCY_DATABASE_URL" \
    --drop-source --apply --i-have-stopped-writes
```

The cleanup requires the completed manifest and a fenced source that still
matches it, and refuses while undelivered module events remain in the
source. Then it drops the module's tables with `alembic_version_<module>`
and deletes the module's inbox claims, command records and saga instances.

The audit rows, the handed-off outbox rows and the fence stay in the shared
database.

## Next steps

- One window per module: online (zero-downtime) extraction is not
  implemented.
- `tests/integration/with_batteries/test_module_extraction.py` covers the fence,
  resume, refusals, drop-source, unfence and the group transfer. The full
  scenario, a monolith split into three services with its data, runs with
  `SANDLOOM_EXTRACTION_E2E=1 uv run pytest tests/extraction --no-cov`.
- Every flag: `uv run sandloom migrate extract-module --help`. The operator
  runbook with the same steps: [`deploy/README.md`](https://github.com/Sandloom-dev/sandloom/blob/main/deploy/README.md).
