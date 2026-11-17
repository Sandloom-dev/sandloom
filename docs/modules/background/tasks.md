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

How the host runs the messages is the driver's own option, not a flag: the `worker_pool`
option of the `celery` instance. The default, `prefork`, forks child processes. `solo`
runs the messages in the worker process and `threads` on a thread pool of it. The kernel
runs an `async def` body on one persistent loop per process. A `threads` pool therefore
runs async bodies one at a time.

With `solo` or `threads`, the worker touches its heartbeat file from its own thread. A
task that runs longer than the check window then does not make `sandloom worker check`
fail. Those pools have no children to recycle. The worker refuses `WORKER_MAX_MEMORY_PER_CHILD`
and `WORKER_MAX_TASKS_PER_CHILD` with them, and a `WORKER_CONCURRENCY` above 1 with `solo`.

**macOS:** the Celery driver's default prefork pool does not work there. Billiard spawns
its pool processes on macOS instead of forking them. They never see the task table the
worker registered at boot and cannot run its messages. The worker refuses to start with
`prefork`. Set `worker_pool` to `solo` or `threads` in `INFRA`:

```bash
INFRA='{"taskqueue": {"default": {"driver": "celery", "worker_pool": "solo"}}}'
```

Or run the worker on Linux, in a container.

Start a worker or a scheduler only with `sandloom worker` and `sandloom beat`. The
importable Celery app of the Celery driver is for `celery inspect` and Flower: a
`celery -A ... worker` or `beat` on it stops with exit code 1. It reads its broker
from `INFRA.taskqueue.default.broker`, through `Settings`, when the app is first
used. With no broker it fails and names that option. It has no result backend
unless that instance states `backend`. The environment cannot replace the app
or the broker of a worker or a beat: `APP` and `BROKER_URL` have no effect there.
A `CELERY_BROKER_URL` that differs from `INFRA` is not ignored. It stops startup
with a `ForeignCeleryConfError`. `celery -b` writes `CELERY_BROKER_URL` and
`--result-backend` writes `CELERY_RESULT_BACKEND`. Celery prefers both over the
app's own broker, so the error names the variable when one differs from
`INFRA`. The same holds for
`CELERY_BROKER_READ_URL` and `CELERY_BROKER_WRITE_URL`. The error never shows the
value.

Both apps are checked against `INFRA` on their final `app.conf`: `broker_url`,
`broker_read_url`, `broker_write_url` and `result_backend`, with a config module or a
`celeryconfig.py` included. The importable app refuses a difference when it
is first used. So does the app that publishes your tasks, on every publish. A
`CELERY_*` variable that differs from the instance's `broker` or `backend` stops
it with a `ForeignCeleryConfError`. That is not an outage: nothing was sent.

`INFRA` states the whole connection in the url. So the check refuses a config module
that sets a connection part next to it. Celery lets these win over what the url omits,
and some win over what it states: `broker_transport = "memory"` would lose every task.
The refusal is the same error. It names the key, never the value:

- `broker_transport`, `broker_host`, `broker_port`, `broker_vhost`, `broker_user` and
  `broker_password`;
- `redis_host`, `redis_port`, `redis_db`, `redis_username`, `redis_password`,
  `redis_backend_use_ssl` and `redis_backend_credential_provider`, for the result backend;
- the result backend settings that name a host or a path: `database_url`, `mongodb_backend_settings`, `couchbase_backend_settings`,
  `arangodb_backend_settings`, `cassandra_*`, `s3_*`, `gcs_bucket`, `gcs_project`,
  `gcs_base_path`, `firestore_project`, `dynamodb_endpoint_url`,
  `azureblockblob_base_path`, `cache_backend` and `cache_backend_options`.

`azureblockblob_container_name`, `cosmosdbsql_database_name` and
`cosmosdbsql_collection_name` have a default name in Celery. The default passes and any
other value is refused.

`database_engine_options` may carry only the pool keys `pool_pre_ping`, `pool_recycle`,
`pool_size`, `max_overflow` and `pool_timeout`. The keys `connect_args` and `creator`
reach the database connect call, so they are refused.

The only `broker_failover_strategy` values are `round-robin` and `shuffle`. The key
`broker_login_method` is allowed: it picks the amqp login mechanism and moves no route.

`broker_use_ssl` must be absent, `None` or `False`. A dict there is merged into the
connection after the url. It can change the host or the password, and it can turn off
the certificate check. `True` is refused too. Kombu then skips the TLS default of a
`rediss://` url and connects in plain text, so the password goes out in the clear. TLS
comes from the scheme and from the query of the `INFRA` url.

A `rediss://` url in `broker` or `backend` must state how it verifies the server. Kombu
connects a `rediss://` url without `ssl_cert_reqs` with no certificate check and only
logs a warning. Celery's result backend refuses such a url. The instance is refused at
startup for both. redis-py does not check the hostname by default. So
`ssl_cert_reqs=required` alone accepts any certificate of the trusted CA, for any name.
Write `?ssl_cert_reqs=required&ssl_check_hostname=true` to verify the certificate and
the name. `?ssl_cert_reqs=none` is the explicit way to skip the check, and needs no
hostname option. An `amqps://` broker verifies the certificate chain and not the
hostname, and the driver has no way to turn that check on. A url of several
`;`-separated hosts uses one scheme and each host states the options. No space or empty
chunk may stand around a `;`. Kombu does not strip it, and a failover to such a chunk
fails with the password in the error it logs.

A `sentinel://` url needs the name of its Redis master. Kombu and Celery read it only from
the transport options, so it is an `INFRA` option. The `broker` takes `master_name` and
the `backend` takes `backend_master_name`. A url without its master is refused. So is a
master on a url that is not `sentinel://`. A config module may state the same master in its
transport options, and any other master is refused. `sentinel_kwargs` is not supported: it
holds the host, the credentials and the TLS of the sentinel nodes.

`broker_transport_options` and `result_backend_transport_options` accept only keys that
tune a connection without moving it, and the keys depend on the scheme of the url. Some
transports lay these options over the url. An `amqp://` broker takes its host, user,
password and `ssl` from them, and an SQS broker takes its region and queue map. A key
outside the list of its scheme is refused, and so is a value that is not a mapping. Every
scheme stays supported. The allowed keys:

- every scheme: `visibility_timeout`, `polling_interval`, `retry_policy`, `max_retries`,
  `interval_start`, `interval_step` and `interval_max`;
- `redis://`, `rediss://` and `sentinel://`: `global_keyprefix`, `sep`, `priority_steps`,
  `queue_order_strategy`, `socket_timeout`, `socket_connect_timeout`, `socket_keepalive`,
  `socket_keepalive_options`, `max_connections`, `health_check_interval`,
  `retry_on_timeout`, `client_name`, `fanout_prefix`, `fanout_patterns`, `ack_emulation`
  and the `unacked_*` keys; `sentinel://` adds `min_other_sentinels`;
- `amqp://`, `amqps://` and `pyamqp://`: `confirm_publish`, `read_timeout`,
  `write_timeout`, `socket_settings`, `channel_max` and `frame_max`;
- `sqs://`: `wait_time_seconds`, `region` (an AWS region name), `queue_name_prefix` and <!-- prose-ok: P6 a list of option names -->
  `predefined_queues`. Each queue carries only its `url`, `backoff_policy` and
  `backoff_tasks`. The credentials and the region of a queue come from the `INFRA` url
  and `region`. The queue `url` is `https://<host>/<account>/<name>` with an AWS host:
  `sqs.<region>.amazonaws.com` (`.amazonaws.com.cn` in China), the FIPS
  `sqs-fips.<region>.amazonaws.com` (not in China) or a VPC endpoint
  `vpce-<id>.sqs.<region>.vpce.amazonaws.com`;
- `result_backend_transport_options` of any backend: `retry_policy`,
  `result_chord_ordered` and `global_keyprefix`.

An `sqs://` url with a host uses plain HTTP in kombu unless the transport option
`is_secure` is true. `INFRA` states it for such a url, so the connection is HTTPS. The
conf may repeat `is_secure = True` and never turn it off. This driver cannot reach a
local emulator on plain HTTP. Without a host in the url, kombu already uses HTTPS.

A `predefined_queues` url is pinned to the region of the `INFRA` broker url. When that
url has an AWS host, the queue urls and the `region` option of the conf must name the
region of that host. When it has none, the `region` option of the conf is the pin. With
neither, kombu takes the region from the environment. The check does not read it, so a
queue of any region passes.

The `INFRA` broker url of an `sqs://` instance takes these host forms: the three
above (`sqs.<region>.amazonaws.com`, the FIPS and the VPC endpoint names, each
naming its region), a host with no AWS domain (an emulator or a proxy, no region to
pin) or no host. A host under `amazonaws.com`, `amazonaws.com.cn` or `api.aws` of any
other form, and any host with a trailing dot, is refused at startup, because the pin
could not read its region: the legacy `<region>.queue.amazonaws.com`, the dual-stack
`sqs.<region>.api.aws`, `sqs.<region>.amazonaws.com.` and
`sqs-fips.<region>.amazonaws.com.cn` (the China regions have no FIPS endpoint). A queue
`url` of any form only names a queue: the `INFRA` host picks the endpoint that carries
the traffic.

The `INFRA` option `sqs_account_id` pins the account. It has 12 digits and any other
scheme refuses it. A queue url of another account is then refused. Left out, any account
passes, and the queue policy of that account is the only guard.

`design/infra-drivers/celery-conf-audit.md` lists every key with its decision and the line
of the library that reads it.

Only the `default` instance's app becomes `celery.current_app`, the app an unbound
`@shared_task` `.delay()` publishes through, and only when it is the process-wide app. A
named instance (`aux`) never does: its broker would capture the tasks of every unbound
call in the thread. This covers the process that publishes. A worker that hosts a named
instance still makes that instance's app current, as Celery does for the one app of a
worker process.

Run exactly one `beat` per broker. A second one stands by. The kernel resolves
the lease (the `lease` instance `INFRA.lease.beat`; `BEAT_LEASE_TTL_SECONDS`,
`BEAT_LEASE_NAME`), scoped per service. The driver's scheduler sends periodic
tasks only while the process holds it. Each cron slot is sent once while the
lease store answers. A slot is an instant in UTC, so the repeated hour of a
daylight saving change runs twice. The scheduler claims a run by its periodic
`id` on one lease name per id, and the claim ends shortly before the next slot.
After a takeover, a scheduler does not repeat the slot the last one sent. A send
still blocked when the old lease ran out is not repeated either.

If the store does not answer, the slot goes out anyway: `periodic` promises at
least one run, and a run may repeat. A refused claim sends nothing and logs an
error. A scheduler killed after its claim and before its send loses that slot.
Any send that may have reached the broker keeps its
claim. See `deploy/README.md` ("Beat singleton lease").

A tool that inspects the Celery driver's queues (`celery inspect`, Flower) uses
the driver's importable app, `sandloom_celery.worker.app` (built from
`taskqueue.default.broker` of `INFRA`, with no application booted): `celery -A sandloom_celery.worker
inspect active`. It is the driver's own interface, in the `sandloom-celery`
package (`pip install 'sandloom[celery]'`).

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
