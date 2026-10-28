---
title: "Tutorial: your first feature"
description: Build a two-module application with generators - a table, routes, an event consumed exactly once, a contract call and tests - using only the beginner names.
type: tutorial
---

# Tutorial: your first feature

You build `tasklist`, a small multi-tenant application with two modules:

- **tasks** stores task items and publishes `tasks.item.created.v1` when one is
  created;
- **stats** counts the items of each tenant by consuming that event exactly
  once, and provides a quota contract that tasks calls before creating an
  item.

Every step is a generator command plus a few lines of domain code. All the
code on this page is included from the finished application in the
repository, `examples/tutorial/`, so you can compare your files
with it at any point.

**You need:** Python 3.12 or later, [uv](https://docs.astral.sh/uv/), Docker,
and a checkout of the Sandloom repository (this page assumes `~/sandloom`). It takes
about twenty minutes.

## 1. Create the application

```bash
cd ~/sandloom
uv sync                                         # the framework and its pinned tools
uv run sandloom new tasklist --dir ~/work --no-batteries --module tasks
cd ~/work/tasklist
uv sync
docker compose -f deploy/docker-compose.dev.yml up -d --wait   # Postgres and Redis
```

`sandloom new` writes a complete application: `tasklist/asgi.py` (the
composition root), `pyproject.toml` with the test plugin activated, a
`deploy/` directory, an `AGENTS.md` for coding agents, and a first module,
`tasklist/modules/tasks/`. The module's whole entry point is one line:

```python title="tasklist/modules/tasks/__init__.py"
--8<-- "examples/tutorial/tasklist/modules/tasks/__init__.py"
```

**What Sandloom did for you:** the application loads every subpackage of
`tasklist/modules` that defines `register`, so a new module edits nothing
else. `define_module` discovers the module's routes, events and tables.

## 2. Read the generated module

The table. `TenantScoped` gives it a `tenant_id` column, filled from the
caller's tenant and filtered on every query:

```python title="tasklist/modules/tasks/models/item.py"
--8<-- "examples/tutorial/tasklist/modules/tasks/models/item.py"
```

The event the module publishes. The channel starts with the module's name, so
this class is the channel's **producer**:

```python title="tasklist/modules/tasks/events.py"
--8<-- "examples/tutorial/tasklist/modules/tasks/events.py"
```

The use cases. `db()` is the session of the running unit of work; the code
flushes and never commits:

```python title="tasklist/modules/tasks/services.py"
--8<-- "examples/tutorial/tasklist/modules/tasks/services.py"
```

**What Sandloom did for you:**

- `module_router()` (in `api.py`) authenticates every route, binds the tenant
  and mounts the routes under `/api/v1/tasks`.
- The write commits before the response is sent, with the event in the same
  transaction (the outbox): the event is published if and only if the item
  is stored.
- A retried `POST` with the same `Idempotency-Key` is not executed twice.
- Before the write commits, the caller's session is checked again.

Nothing of this is declared in the code. See
[Route policy and sessions](../concepts/route-policy.md) and
[Units of Work](../concepts/units.md).

Run the module's tests and the gates:

```bash
uv run sandloom test tasks
```

## 3. Add a second module

```bash
uv run sandloom add module stats
```

The generator writes `tasklist/modules/stats/` with an example table. Stats
needs one counter row per tenant instead, so replace the example model with
this one:

```python title="tasklist/modules/stats/models/counter.py"
--8<-- "examples/tutorial/tasklist/modules/stats/models/counter.py:model"
```

Nothing has been deployed yet, so edit the generated baseline revision to
create this table instead of the example one. Keep the row-level-security
calls the generator wrote. The unique key on `tenant_id` already serves the
tenant lookups, so the table gets no separate `tenant_id` index:

```python title="tasklist/modules/stats/migrations/versions/0001_stats_baseline.py"
--8<-- "examples/tutorial/tasklist/modules/stats/migrations/versions/0001_stats_baseline.py:upgrade"
```

Once a revision has been applied anywhere, never edit it: add a new one with
`uv run sandloom migrate new stats "<message>"` ([Write a migration](../guides/write-migration.md)).

The use cases. The select needs no tenant filter: the bound tenant filters
`TenantScoped` rows automatically.

```python title="tasklist/modules/stats/services.py"
--8<-- "examples/tutorial/tasklist/modules/stats/services.py:services"
```

Delete the generated `events.py` of stats (it produces no event) and give it
one read route:

```python title="tasklist/modules/stats/api.py"
--8<-- "examples/tutorial/tasklist/modules/stats/api.py:route"
```

## 4. Consume the event exactly once

```bash
uv run sandloom add consumer stats tasks.item.created.v1
```

The generator reads the producer's schema and writes a **view** of the event
(the stats module's own class for a channel it does not own) and an empty
handler. Make the handler count the item:

```python title="tasklist/modules/stats/consumers.py"
--8<-- "examples/tutorial/tasklist/modules/stats/consumers.py:consumer"
```

**What Sandloom did for you:**

- the handler runs in its own unit of work, with the event's `tenant_id`
  bound, so `services.record_item()` writes the right tenant's row;
- the consumer is durable: a redelivered event (same envelope id) is applied
  once;
- stats never imports tasks: the view is checked against the producer's
  schema instead ([Events](../concepts/events.md)).

Test it through the real consumer runtime. `deliver(..., times=2)` delivers
the same event twice:

```python title="tasklist/modules/stats/tests/test_stats.py"
--8<-- "examples/tutorial/tasklist/modules/stats/tests/test_stats.py:tests"
```

```bash
uv run sandloom test stats
```

## 5. Provide a contract

```bash
uv run sandloom add contract stats quota
```

The generator writes the provider class in `stats/contracts.py`, its DTO in
`stats/schemas.py`, then runs `sandloom generate`, which writes a typed stub
(and the DTO's copy) in `tasklist/shared/contracts/`. Replace the example
methods with one read (and drop the example DTO):

```python title="tasklist/modules/stats/contracts.py"
--8<-- "examples/tutorial/tasklist/modules/stats/contracts.py:contract"
```

`uv run sandloom generate stats` regenerates the stub from the provider, after
checking the change is compatible with the committed one (`sandloom test`
only checks):

```python title="tasklist/shared/contracts/stats_quota_v1.py"
--8<-- "examples/tutorial/tasklist/shared/contracts/stats_quota_v1.py"
```

Callers import this stub, never the stats module.

## 6. Call the contract

In the tasks module, refer to the contract at module level and call it before
creating an item:

```python title="tasklist/modules/tasks/api.py"
--8<-- "examples/tutorial/tasklist/modules/tasks/api.py:quota"

--8<-- "examples/tutorial/tasklist/modules/tasks/api.py:routes"
```

**What Sandloom did for you:**

- `Contract(STATS_QUOTA_V1)` is typed: your editor and mypy know
  `may_create_item()` returns a `bool`;
- the module now declares that it consumes `stats.quota.v1`; a deployment
  without the provider fails at startup instead of creating items unchecked;
- in a monolith the call runs in-process; when stats runs as its own service
  the same line calls it over internal RPC
  ([Contracts](../concepts/contracts.md)).

Test both answers of the quota with a test double:

```python title="tasklist/modules/tasks/tests/test_tasks.py"
--8<-- "examples/tutorial/tasklist/modules/tasks/tests/test_tasks.py:tests"
```

## 7. Run every gate

```bash
uv run sandloom generate
uv run sandloom test --full
```

`sandloom generate` writes the contract stub, the event schemas and the module
snapshots after a compatibility check (`sandloom add` already ran it after each
generator); `sandloom test --full` runs the whole suite in parallel, ruff,
format, mypy, `sandloom check`, and checks those files without writing them.
Commit the generated files with your code.

Run the application:

```bash
uv run sandloom migrate upgrade-all --apply
uv run uvicorn tasklist.asgi:app --reload       # http://localhost:8000/docs
```

## If you are stuck

- Every error carries a code: `uv run sandloom explain <code>` says what happened
  and how to fix it. All codes: [error catalogue](../reference/error-codes.md).
- `uv run sandloom docs <name>` prints a name's signature and documentation.
- Compare your files with `examples/tutorial/`.

## Next steps

- [Concepts](../concepts/index.md): what the framework did in each step, and
  its exact guarantees.
- [How-to guides](../guides/index.md): one task each, such as a saga, a
  projection or a migration.
