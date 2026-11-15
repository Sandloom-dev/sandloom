---
title: Background tasks
description: '@task and @periodic in a module, open_uow() units, Task.enqueue and its deduplication, the worker and the beat lease.'
type: concept
---

# Background tasks

A **background task** is work that runs outside a request, in the framework's
worker: a scheduled cleanup, a slow export, a retryable call to an outside
service. A module declares its tasks with `@task` and `@periodic`
(`sandloom.sdk.tasks`, typed) in its own `tasks.py` (`sandloom add task <module>
<name>` writes one); the framework's worker boots the application and imports
the tasks of every loaded module. A task is a module-level function, usually
`async def`, that takes one pydantic model or nothing. Every database change
in its body happens in an explicit unit, `async with open_uow():`. A task has no
request around it: no session check, no automatic idempotency, no write
admission across blocks. Its retries are made safe at the **effect** level,
with an enqueue key, the durable inbox or a unique operation key.

## What you get by default / what you declare

| You get by default | You declare |
| --- | --- |
| The worker imports `<module>/tasks.py` of every loaded module | `@task` / `@periodic` functions in `tasks.py` (never a broker app) |
| One persistent event loop per worker process, with engine pools bound to it | an `async def` body (never `asyncio.run`) |
| Commit on success, rollback on any exception, effects dispatched after COMMIT, per block | `async with open_uow() as session:` around each unit of work |
| No stored results; the worker prefetches one message | nothing: a task returns nothing |
| At-most-once acknowledgement (the message is acked when the task starts) | `@task(at_least_once=True)` for an idempotent task (the driver feature `redelivery`) |
| Deduplicated enqueue retries when a key is given | `await task.enqueue(payload, idempotency_key=...)` |
| A scheduler that runs only while it holds a fenced lease | `@periodic(cron=..., id=...)` in `tasks.py` for schedules |
| Startup checks that the task queue's driver can serve the module | `capabilities=["taskqueue"]` in `define_module`, with the feature `periodic` for schedules (`sandloom add task` writes it) |

## How to use it

### A task

```python
# <module>/tasks.py
from uuid import UUID

from pydantic import BaseModel

from sandloom.sdk.data import open_uow
from sandloom.sdk.tasks import task
from sandloom.sdk.tenant import bind_tenant

from . import services


class CloseStaleItems(BaseModel):
    tenant_id: UUID


@task(at_least_once=True)                       # idempotent: runs again if the worker dies
async def close_stale_items(payload: CloseStaleItems) -> None:
    with bind_tenant(payload.tenant_id):
        async with open_uow():
            await services.close_stale()          # uses db(); publishes, audits
```

- **A task is a function as written.** `await close_stale_items(payload)` runs
  the body in your process (a sync `def` is called without `await`). The worker
  runs an `async def` body on its one persistent loop (fresh after fork,
  engines disposed on it at shutdown), so never `asyncio.run` inside a task. A
  sync body runs directly in the worker process, outside any running loop.
- **The name is the dotted path** of the function
  (`<package>.tasks.close_stale_items`); there is no `name=`, and two tasks
  with one name are refused at load.
- **One `open_uow()` block per unit.** Inside it, `db()` is that unit's
  session, and `.publish()`, `record_audit(...)` and `on_commit(...)` work as
  in a request: the block commits when it exits normally and rolls back on
  an exception. Several blocks are several independent transactions; a
  failure in the second does not undo the first. `open_uow("analytics")`
  opens a unit on a named database.
- **Tenancy.** A task has no bound tenant. Bind one with `bind_tenant(id)`
  for `TenantScoped` work; deliberate cross-tenant work goes inside
  `with unscoped():`.
- **Pass ids, not objects.** The payload is validated and serialised to JSON
  before anything is sent. Its fields are wire types (ids, strings, numbers);
  the task loads the rest inside the unit.

### Enqueue

From code that holds a unit (a route, a consumer), enqueue the task:

```python
from datetime import timedelta

from sandloom.sdk.effects import operation_key

from .tasks import CloseStaleItems, close_stale_items

await close_stale_items.enqueue(
    CloseStaleItems(tenant_id=tenant_id),
    idempotency_key=operation_key("close-stale", tenant_id, day),
)
```

`enqueue(payload, *, delay=None, idempotency_key=None)` returns the job id.
`delay` is a `timedelta` and needs a task queue driver with the feature `delay`
(`Unsupported` otherwise, raised before anything is reserved). A task without
a payload is enqueued without one. A module enqueues only its own tasks:
work in another module goes through events or contracts, never through that
module's task name. With a key:

- a successful repeat within `dedupe_ttl_seconds` (driver option, default
  86400, maximum 604800) returns the original job id and publishes nothing;
- the same key with another task, payload or delay raises
  `TaskEnqueueConflict` (409);
- a concurrent send, or one whose broker acknowledgement was lost, raises
  `TaskEnqueueUncertain` (503) and keeps the key pending. Pending records have
  no automatic expiry: a lost acknowledgement cannot safely be treated as a
  failed send. Reconcile it first (the "Uncertain task enqueues" runbook in
  `docs/operate/operator-reference.md`);
- a dedupe store outage, or a cache without compare-and-set, raises
  `TaskEnqueueUnavailable` (503) and publishes nothing.

The reservation lives in the shared `idempotency` `atomic_store` instance
(`INFRA.atomic_store.idempotency`, else the `security` one). It is scoped to
the queue instance, the broker and the bound tenant. Use a persistent,
non-evicting Redis. Without a key no dedupe store is used.

An enqueue is not part of the unit's transaction. If the task must run only
when the change commits, enqueue it from `on_commit(...)`, or publish an
event and enqueue from its consumer.

### Effect-level dedupe

The broker may redeliver a message (a worker crash under `at_least_once`, a lost
acknowledgement), and enqueue dedupe expires. A task protects every
irreversible effect with a dedupe record that commits in the effect's
transaction. [Deduplicate effects](effect-deduplication.md#claim-an-effect-yourself)
lists its forms.

### The worker and beat

The worker is a framework command:

```bash
sandloom worker [--queues Q,Q] [--concurrency N]
sandloom beat
sandloom worker check [--timeout SECONDS]   # the container healthcheck
```

It boots the composition root named by `SANDLOOM_COMPOSITION_ROOT` (default
`sandloom.main`; an app generated by `sandloom new` uses `<app>.asgi`). It takes
its worker host from the task queue instance named by `SANDLOOM_WORKER_TASKQUEUE`
(default `default`; the bundled `celery` driver). The task table comes from
`<module>.tasks` of every **loaded** module.

The kernel runs no message loop of its own. The driver's host consumes the
queues and runs each message through the kernel: an `async def` body on one
persistent loop per process, a sync body directly. Concurrency defaults from
the CPU quota (`WORKER_CONCURRENCY` overrides). `sandloom worker check` asks the
host whether a worker is alive, without booting the application. It still needs the
full, valid settings of the worker, `JWT_SECRET` included.

Start a worker or a scheduler only with `sandloom worker` and `sandloom beat`. The
importable Celery app of the Celery driver is for `celery inspect` and Flower: a
`celery -A ... worker` or `beat` on it stops with exit code 1. The environment
(`APP`, `BROKER_URL`, `CELERY_BROKER_URL` and the like) cannot replace the app or
the broker of the instance a worker or a beat runs.

Run exactly one `beat` per broker. A second one stands by. The kernel resolves
the lease (the `lease` instance `INFRA.lease.beat`; `BEAT_LEASE_TTL_SECONDS`,
`BEAT_LEASE_NAME`), scoped per service. The driver's scheduler sends periodic
tasks only while the process holds it. See `deploy/README.md` ("Beat singleton
lease").

A tool that inspects the Celery driver's queues (`celery inspect`, Flower) uses
the driver's importable app, `sandloom.core.drivers.celery_worker.app` (built
from `BROKER_URL`, with no application booted): `celery -A
sandloom.core.drivers.celery_worker inspect active`. It is the driver's own
interface and moves to `sandloom_celery.app` with the package.

**A module's first periodic task needs a beat.** A `@periodic`
task runs only where a beat loads the
module, and exactly one may. In the microservices profile each service has
its own `<service>-worker` and `<service>-beat`: `sandloom add task <m> <name>
--periodic "<cron>"` adds the pair for a deployed module, and
`sandloom add deploy <m> --service <s>` adds it when it deploys one with
periodic tasks. `sandloom test`'s static `schema & deploy` step fails a profile
whose module has no beat, or two (SANDLOOM-T010).

```python
from sandloom.sdk.tasks import periodic


@periodic(cron="0 3 * * *", id="notes.purge")
async def purge() -> None:
    ...
```

`cron` is five fields, evaluated in UTC; `id` is unique in the deployment
(a duplicate is refused at load). Each run may repeat or overlap another, so
make the body harmless to run twice. A periodic task is also a function as
written: `await purge()` runs it inline, and `purge.max_interval` is the
longest gap between two of its slots.

**A module with tasks declares the task queue.** The module states what it
needs, and the deployment picks the driver. `tasks.py` needs the `taskqueue`
capability. A `@periodic` task also needs the feature `periodic`: a driver that runs
the module's periodic jobs at their slots while the deployment runs. An
`at_least_once` task needs `redelivery`. A driver that can only enqueue has
neither; today only `celery` does. In
`define_module`, write
`capabilities=[CapabilityRequirement("taskqueue", features=frozenset({"periodic"}))]`,
with `CapabilityRequirement` from `sandloom.sdk.module`.

The kernel derives the same need from `tasks.py` at load, so startup checks
it either way; `sandloom check` asks for the declaration
([SANDLOOM-C033](../../reference/error-codes.md#sandloom-c033)). A
declaration decides whether the need is required: pass `required=False` when
the module works without its schedule (identity's token cleanup does). A
selected module whose required capability is not met refuses startup, also
when the module is optional.

The worker, `sandloom migrate` and the operational commands (`sandloom outbox`,
`sandloom streams`, `sandloom projection`) resolve the application through the same
composition root, so with `SANDLOOM_COMPOSITION_ROOT=<app>.asgi` each acts on the
app's module set, never silently on the framework's default.

### Startup work

Work that must run when a process starts goes through
`core.add_startup_task(fn)` in the module's `setup(core)`; pass
`critical=True` when the process must not serve traffic if it fails (the
failure then aborts startup instead of degrading silently).

## What breaks it

| Error | When |
| --- | --- |
| [SANDLOOM-R002](../../reference/error-codes.md#sandloom-r002) | `db()`, `.publish()` or `record_audit` outside an `open_uow()` block |
| [SANDLOOM-R001](../../reference/error-codes.md#sandloom-r001) | a `TenantScoped` query with no tenant bound |
| [SANDLOOM-C004](../../reference/error-codes.md#sandloom-c004) | a broker app or another backend client constructed in a module |
| [SANDLOOM-C021](../../reference/error-codes.md#sandloom-c021) | a function-local import in `tasks.py` |
| [SANDLOOM-C033](../../reference/error-codes.md#sandloom-c033) | `tasks.py` without `taskqueue` in `capabilities=`, or a schedule without `periodic` |
| `TaskEnqueueConflict` / `TaskEnqueueUncertain` / `TaskEnqueueUnavailable` | the enqueue cases above |

??? note "Guarantees, precisely"
    - **Enqueue (G-TASK-01 to 03).** With a key, one real message per
      successful retry within the replay window, parallel retries included;
      conflicts are 409; the dedupe fails closed (503) and an ambiguous send
      stays pending, never republished, until reconciled. This is
      **replay-only** for the send, not exactly-once execution.
    - **Execution (G-TASK-04, G-TASK-05, C-WORKER-01 to 06).** One persistent
      loop per worker process; no stored results; prefetch one;
      `at_least_once` is a per-task opt-in. A task runs at most once by default
      and at least once with `at_least_once=True`; exactly-once **effect** comes only
      from a dedupe record committed with the effect.
    - **Units.** Each `open_uow()` block has the commit semantics of every
      unit (G-UOW-01, G-UOW-04): `before_commit`, COMMIT, outbox dispatch,
      `on_commit`. There is no request re-check and no write admission across
      blocks.

    See [ADR-0005](../../adr/0005-effect-model.md), the
    [guarantee ledger](../../reference/guarantees.md) and the
    [transaction and replay lifecycle](../../advanced/lifecycle.md).

## Design rationale

- [ADR-0005: the effect model](../../adr/0005-effect-model.md) (enqueue dedupe
  as one dedupe record).
- [ADR-0010: Public API 1.0](../../adr/0010-public-api-1.0.md) §4 (units and
  their owners).

Related: [Units of work](../database/units.md), [Capabilities and infrastructure](../../infrastructure/index.md),
[Deployment](../../operate/topologies.md).
