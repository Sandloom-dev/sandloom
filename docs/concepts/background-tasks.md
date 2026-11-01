---
title: Background tasks
description: Celery tasks in a module, run_async with open_uow() units, enqueue deduplication, the worker and the beat lease.
type: concept
---

# Background tasks

A **background task** is work that runs outside a request, in the Celery
worker: a scheduled cleanup, a slow export, a retryable call to an outside
service. A module declares its tasks with `@shared_task` (`sandloom.sdk.tasks`,
typed) in its own `tasks.py` (`sandloom add task <module> <name>` writes one);
the framework's worker boots the application and imports the tasks of every
loaded module. A task body is synchronous, so it runs its
async work with `run_async(coro)`, and inside that coroutine every database
change happens in an explicit unit, `async with open_uow():`. A task has no
request around it: no session check, no automatic idempotency, no write
admission across blocks. Its retries are made safe at the **effect** level,
with an enqueue key, the durable inbox or a unique operation key.

## What you get by default / what you declare

| You get by default | You declare |
| --- | --- |
| The worker imports `<module>/tasks.py` of every loaded module | `@shared_task` functions in `tasks.py` (never a Celery app) |
| One persistent event loop per worker process, with engine pools bound to it | `run_async(coro)` in the task body (never `asyncio.run`) |
| Commit on success, rollback on any exception, effects dispatched after COMMIT, per block | `async with open_uow() as session:` around each unit of work |
| No stored results; the worker prefetches one message | `ignore_result=False` only when you read a result |
| At-most-once acknowledgement (the message is acked when the task starts) | `acks_late=True, reject_on_worker_lost=True` for an idempotent task |
| Deduplicated enqueue retries when a key is given | `taskqueue().enqueue(task, payload, idempotency_key=...)` |
| A beat scheduler that runs only while it holds a fenced lease | `setup_periodic_tasks(sender, **kwargs)` in `tasks.py` for schedules |

## How to use it

### A task

```python
# <module>/tasks.py
from uuid import UUID

from sandloom.sdk.data import open_uow
from sandloom.sdk.effects import run_async
from sandloom.sdk.tasks import shared_task
from sandloom.sdk.tenant import bind_tenant

from . import services


@shared_task(acks_late=True, reject_on_worker_lost=True)   # idempotent: at least once
def close_stale_items(tenant_id: str) -> None:
    run_async(_close_stale_items(UUID(tenant_id)))


async def _close_stale_items(tenant_id: UUID) -> None:
    with bind_tenant(tenant_id):
        async with open_uow():
            await services.close_stale()          # uses db(); publishes, audits
```

- **`run_async`, never `asyncio.run`.** The worker keeps one loop per
  process (fresh after fork, engines disposed on it at shutdown), and engine
  pools are bound to that loop. `run_async` refuses to run inside a running
  loop; in async code, `await` the coroutine instead.
- **One `open_uow()` block per unit.** Inside it, `db()` is that unit's
  session, and `.publish()`, `record_audit(...)` and `on_commit(...)` work as
  in a request: the block commits when it exits normally and rolls back on
  an exception. Several blocks are several independent transactions; a
  failure in the second does not undo the first. `open_uow("analytics")`
  opens a unit on a named database.
- **Tenancy.** A task has no bound tenant. Bind one with `bind_tenant(id)`
  for `TenantScoped` work; deliberate cross-tenant work goes inside
  `with unscoped():`.
- **Pass ids, not objects.** Arguments are serialised by the broker; pass
  strings and numbers, and load the rest inside the unit.

### Enqueue

From code that holds a unit (a route, a consumer), enqueue through the task
queue capability:

```python
from sandloom.sdk.infra import taskqueue
from sandloom.sdk.effects import operation_key

await taskqueue().enqueue(
    "notes.close_stale_items",
    {"tenant_id": str(tenant_id)},
    idempotency_key=operation_key("close-stale", tenant_id, day),
)
```

`enqueue(task, payload, *, delay_seconds=None, idempotency_key=None)` returns
the job id. With a key:

- a successful repeat within `dedupe_ttl_seconds` (driver option, default
  86400, maximum 604800) returns the original job id and publishes nothing;
- the same key with another task, payload or delay raises
  `TaskEnqueueConflict` (409);
- a concurrent send, or one whose broker acknowledgement was lost, raises
  `TaskEnqueueUncertain` (503) and keeps the key pending. Pending records have
  no automatic expiry: a lost acknowledgement cannot safely be treated as a
  failed send. Reconcile it first (the "Uncertain task enqueues" runbook in
  `docs/operations/operator-reference.md`);
- a dedupe store outage, or a cache without compare-and-set, raises
  `TaskEnqueueUnavailable` (503) and publishes nothing.

The reservation lives in the shared `SECURITY_CACHE_INSTANCE`, scoped to the
queue instance, the broker and the bound tenant; use a persistent,
non-evicting Redis. Without a key no dedupe store is used.

An enqueue is not part of the unit's transaction. If the task must run only
when the change commits, enqueue it from `on_commit(...)`, or publish an
event and enqueue from its consumer.

### Effect-level dedupe

The broker may redeliver a message (a worker crash with `acks_late`, a lost
acknowledgement), and enqueue dedupe expires. Protect every irreversible
task effect with a dedupe record that commits in the same transaction as the
effect:

- `if not await durable_inbox("<module>@<task>").claim(db(), key): return`
  inside the block (`sandloom.sdk.effects`), or
- a `UNIQUE` operation-key column on the effect's own row, or
- a command contract called with a derived `operation_key(...)`: its command
  log dedupes at the provider.

### The worker and beat

The worker is a framework entry point:

```bash
celery -A sandloom.core.worker worker
celery -A sandloom.core.worker beat
```

It boots the composition root named by `SANDLOOM_COMPOSITION_ROOT` (default
`sandloom.main`; an app generated by `sandloom new` uses `<app>.asgi`), takes its
Celery app from the task queue capability instance named by
`SANDLOOM_WORKER_TASKQUEUE` (default `default`), and imports `<module>.tasks` of
every **loaded** module. Concurrency defaults from the CPU quota
(`WORKER_CONCURRENCY` overrides); health checks use
`python -m sandloom.core.worker healthcheck`.

Run exactly one `beat` per broker. A second one stands by: beat schedules
only while it holds a fenced cache lease (`BEAT_LEASE_*`), scoped per
service. See `deploy/README.md` ("Beat singleton
lease").

**A module's first periodic task needs a beat.** A schedule
(`setup_periodic_tasks` in `tasks.py`) runs only where a beat loads the
module, and exactly one may. In the microservices profile each service has
its own `<service>-worker` and `<service>-beat`: `sandloom add task <m> <name>
--periodic "<cron>"` adds the pair for a deployed module, and
`sandloom add deploy <m> --service <s>` adds it when it deploys one with
periodic tasks. `sandloom test`'s static `schema & deploy` step fails a profile
whose module has no beat, or two (SANDLOOM-T010).

```python
from typing import Any

from sandloom.sdk.tasks import cron


def setup_periodic_tasks(sender: Any, **kwargs: Any) -> None:
    sender.add_periodic_task(cron("0 3 * * *"), purge.s(), name="notes.purge")
```

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
| [SANDLOOM-R002](../reference/error-codes.md#sandloom-r002) | `db()`, `.publish()` or `record_audit` outside an `open_uow()` block |
| [SANDLOOM-R001](../reference/error-codes.md#sandloom-r001) | a `TenantScoped` query with no tenant bound |
| [SANDLOOM-C004](../reference/error-codes.md#sandloom-c004) | a Celery app or another backend client constructed in a module |
| [SANDLOOM-C021](../reference/error-codes.md#sandloom-c021) | a function-local import in `tasks.py` |
| `TaskEnqueueConflict` / `TaskEnqueueUncertain` / `TaskEnqueueUnavailable` | the enqueue cases above |

??? note "Guarantees, precisely"
    - **Enqueue (G-TASK-01 to 03).** With a key, one real message per
      successful retry within the replay window, parallel retries included;
      conflicts are 409; the dedupe fails closed (503) and an ambiguous send
      stays pending, never republished, until reconciled. This is
      **replay-only** for the send, not exactly-once execution.
    - **Execution (G-TASK-04, G-TASK-05, C-WORKER-01 to 06).** One persistent
      loop per worker process; no stored results; prefetch one;
      `acks_late` is a per-task opt-in. A task runs at most once by default
      and at least once with `acks_late`; exactly-once **effect** comes only
      from a dedupe record committed with the effect.
    - **Units.** Each `open_uow()` block has the commit semantics of every
      unit (G-UOW-01, G-UOW-04): `before_commit`, COMMIT, outbox dispatch,
      `on_commit`. There is no request re-check and no write admission across
      blocks.

    See [ADR-0005](../adr/0005-effect-model.md), the
    [guarantee ledger](../reference/guarantees.md) and the
    [transaction and replay lifecycle](lifecycle.md).

## Design rationale

- [ADR-0005: the effect model](../adr/0005-effect-model.md) (enqueue dedupe
  as one dedupe record).
- [ADR-0010: Public API 1.0](../adr/0010-public-api-1.0.md) §4 (units and
  their owners).

Related: [Units of work](units.md), [Capabilities and infrastructure](capabilities-and-infrastructure.md),
[Deployment](deployment.md).
