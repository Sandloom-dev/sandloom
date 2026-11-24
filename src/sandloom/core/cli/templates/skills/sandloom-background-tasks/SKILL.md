---
name: sandloom-background-tasks
description: Use when work in this Sandloom app must run later, off the request, or on a schedule: a job after a request returns, a nightly purge, a retry-safe batch, or a task that touches every tenant. It covers `sandloom add task`, `@task`, `@periodic`, `at_least_once`, `open_uow` and `unscoped`.
---

# Background tasks in <app>

A task is a function in the module's `tasks.py`. The framework worker
(`uv run sandloom worker`) runs it; the scheduler (`uv run sandloom beat`) sends
the periodic ones. Never create a broker app yourself.

## Which one

| You need | Do this |
|---|---|
| work after the request returns | `uv run sandloom add task <module> <task>`, then `await <task>.enqueue(payload)` |
| work on a schedule | `uv run sandloom add task <module> <task> --periodic "0 3 * * *"` |
| a nightly job, worked example with tests | recipe `periodic-task` |
| to test a task body | recipe `test-service-or-task` |
| the task in a split deployment | skill `sandloom-split-deploy` |

## Write the task

`sandloom add task` writes the function and the module's task-queue
declaration. A task takes one pydantic model of wire types, or nothing, and its
name is its dotted path, so it has no `name=`.

```python
from sandloom.sdk.data import open_uow
from sandloom.sdk.tasks import periodic

@periodic(cron="0 3 * * *", id="<module>.purge-old-items")
async def purge_old_items() -> None:
    """Every day at 03:00 UTC."""
    async with open_uow():  # one short unit per batch
        await purge_batch()
```

- **The body is `async def`.** No `asyncio.run`. Open a unit with
  `async with open_uow():`; inside it `db()`, `await X(...).publish()` and
  `record_audit` work as in a request. Outside a unit they raise.
- **`cron`** has five fields and runs in UTC. **`id`** is unique across the
  deployment. A periodic function takes no payload.
- **A run can repeat.** A worker can die, a slot can be sent twice. Make every
  run harmless to repeat: delete or update by a condition, upsert, or claim a
  durable inbox key. Work in short units, one `open_uow()` per batch.
- **`@task(at_least_once=True)`** lets the queue redeliver a task that did not
  finish. Use it only for an idempotent task whose effects have a dedupe in the
  database (a UNIQUE operation key); the module also declares the
  task-queue feature `redelivery` (SANDLOOM-C033).
- **Send it** with `await task.enqueue(payload, delay=..., idempotency_key=...)`.
  `await task(payload)` runs the body in your process, which is how a test calls it.

## Tenants

A request binds one tenant; a worker does not. Pick one on purpose:

- **Work for one tenant:** `with bind_tenant(tenant_id):` (`bind_tenant` is a
  tier 1 name of `sandloom.sdk`).
- **Maintenance across all tenants:** `with unscoped():` from
  `sandloom.sdk.tenant`. It is not a tier 2 name, so its import needs
  `# sandloom: advanced every tenant`.

## Rules

1. **Free names.** `task`, `periodic` and `open_uow` are free in the module's
   own `tasks.py` only; in any other file their import needs
   `# sandloom: advanced <reason>` (SANDLOOM-T009).
2. **A child task opens its own `open_uow()`** (SANDLOOM-R013): never reuse the
   parent's session.
3. **Each periodic task needs a beat in every profile that serves its module.**
   In the microservices profile `uv run sandloom add deploy <module>` (or
   `sandloom add task --periodic`, or `sandloom add deploy --refresh`) adds
   `<service>-worker` and `<service>-beat`; `sandloom test` checks it
   (SANDLOOM-T010).
4. **Exactly one beat runs per broker** (a fenced lease): do not add a second
   scheduler by hand.

Finish with skill `sandloom-quality-gates`: `uv run sandloom generate`, then
`uv run sandloom test <module>`.
