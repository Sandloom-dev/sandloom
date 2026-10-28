---
title: Add a module
description: Create a business module with `sandloom add module`, then replace the example table, route and event with your domain.
type: how-to
---

# Add a module

**Goal:** a new business module (a package under `<app>/modules/`) with a
table, an authenticated route, an event, a migration and tests, loaded by the
app with no wiring.

**Use it** for a new bounded part of your domain that owns its own tables,
such as `billing_notes`, `stats` or `board`.

**Do not use it** to split code inside one module (add a file to the existing
package instead), or to share code between modules. Modules never import each
other: they talk through [events](../concepts/events.md) and
[contracts](../concepts/contracts.md). See
[Modules](../concepts/modules.md).

## Prerequisites

- An application created with `sandloom new` (it has an `architecture_rules.yml`
  at its root).
- The development services running:
  `docker compose -f deploy/docker-compose.dev.yml up -d --wait`.

## Steps

### 1. Run the generator

From the application root:

```bash
sandloom add module board
```

It writes new files inside `<app>/modules/board/` only, and refuses to run
if the package already exists. It edits no other file: the app loads every
subpackage of `<app>/modules` whose `__init__.py` defines `register`, so the
new module is part of the app at once. `SANDLOOM_MODULES` still selects a subset
in a given process.

### 2. Read what it wrote

The module declaration. `define_module` discovers everything else from the
conventional submodules (`models`, `api`, `events`, `consumers`,
`contracts`, `sagas`, `projections`, `tasks`):

```python
--8<-- "docs/snippets/generated/module/demo/modules/board/__init__.py"
```

An example table. `TenantScoped` adds the `tenant_id` column and filters
every query by the bound tenant. Table names start with the module name.
`created_at: Mapped[CreatedAt]` defaults to `clock_timestamp()`, not `now()`:
`now()` is the transaction's start, so rows created in one transaction (a
test's `client` fixture shares one) would tie, and a "newest first" list would
come back in any order. `default=new_id` gives time-ordered UUIDv7 ids. The
list orders by `created_at`, then `id`
([Timestamps, ids and list order](../concepts/modules.md#timestamps-ids-and-list-order)):

```python
--8<-- "docs/snippets/generated/module/demo/modules/board/models/item.py"
```

The event the module publishes. Its channel starts with the module name, so
this class is the channel's producer:

```python
--8<-- "docs/snippets/generated/module/demo/modules/board/events.py"
```

The use cases. `db()` is the session of the running unit of work; the event
is written to the outbox in the same transaction as the row:

```python
--8<-- "docs/snippets/generated/module/demo/modules/board/services.py"
```

The routes. `module_router()` mounts them under `/api/v1/board`,
authenticates every route and binds the tenant (the module has a
`TenantScoped` table). Nothing else is declared: the `POST` gets the write
defaults (automatic idempotency, the commit-time session check). The list
route caps its `limit` with `Query(le=...)`: one request never loads an
unbounded table, and `TenantScoped` keeps it to the caller's tenant:

```python
--8<-- "docs/snippets/generated/module/demo/modules/board/api.py"
```

The baseline migration of the module's own chain, rendered from the model
(no database is needed). It enables row-level security for the tenant
table:

```python
--8<-- "docs/snippets/generated/module/demo/modules/board/migrations/versions/0001_board_baseline.py"
```

The tests, which run against the real app and a real database:

```python
--8<-- "docs/snippets/generated/module/demo/modules/board/tests/test_board.py"
```

The generator also writes `schemas.py` (pydantic request and response
models), `models/__init__.py` (imports every `models/*.py`),
`migrations/env.py` and `migrations/script.py.mako`.

### 3. Replace the example with your domain

1. Rename or replace `BoardItem` in `models/`. Add one file per table;
   keep the `board_` prefix and reference other modules' rows by UUID, never
   by a foreign key.
2. If the module has not been deployed anywhere yet, edit the baseline
   migration so it creates your real tables. After the first deployment,
   never edit a revision that ran: add a new one
   ([Write a migration](write-migration.md)).
3. Adapt `schemas.py`, `services.py` and `api.py`. Keep SQL in `services.py`
   and `models/`, endpoints in `api.py`, and pure pydantic in `schemas.py`.
4. Rename the event or add more with
   [`sandloom add event`](add-event.md).

## Verify

```bash
sandloom test board
```

It runs the module's tests first, then the fast suite and the static gates
(ruff, format, mypy, `sandloom check`, the route and contract snapshots).
It never writes the module's generated files, such as
`routes.snapshot.json`: `sandloom add` ran `sandloom generate`, which wrote
them; after a change run `sandloom generate`, review them in your diff and
commit them. A stale one fails with "generated files are stale: run
`sandloom generate`".

Then apply the migration to your development database:

```bash
sandloom migrate upgrade-all --apply
```

## Errors you may hit

- [SANDLOOM-C001](../reference/error-codes.md#sandloom-c001): the module imports
  another module; use an event or a contract.
- [SANDLOOM-B001](../reference/error-codes.md#sandloom-b001): the module failed to
  load; the message carries the original traceback.
- [SANDLOOM-B006](../reference/error-codes.md#sandloom-b006): an object `define_module`
  collects lives outside the conventional submodules.
- [SANDLOOM-B008](../reference/error-codes.md#sandloom-b008): a route is neither
  authenticated nor `public(reason=...)`, or is both.
- [SANDLOOM-T001](../reference/error-codes.md#sandloom-t001): the route snapshot is
  stale (run `sandloom generate`), or a route lost a protection (review it,
  then approve it with `sandloom routes snapshot --write`).
- [SANDLOOM-T002](../reference/error-codes.md#sandloom-t002): a table exists in the
  models but no revision of the chain creates it.

## Related

- [Modules](../concepts/modules.md): the isolation rule and discovery.
- [Units of Work and `db()`](../concepts/units.md).
- [Route policy](../concepts/route-policy.md): the defaults every route gets.
- [Testing modules](testing.md).
- [Tutorial](../getting-started/tutorial.md): a module built step by step.
